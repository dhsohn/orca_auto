"""Incremental ORCA list queries over canonical queue/location/state sources."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from orca_auto.core import activity_index as index
from orca_auto.core import activity_invalidation as journal
from orca_auto.core.activity import ActivityListRequest, ActivityRecord, sort_key
from orca_auto.core.indexing import JobLocationRecord
from orca_auto.core.indexing import store as locations
from orca_auto.core.queue import persistence as queue
from orca_auto.core.queue.store import queue_lock
from orca_auto.core.utils.lock import file_lock
from orca_auto.orca.queue import adapter
from orca_auto.orca.run_snapshot import RunSnapshot, collect_run_snapshots

from . import _orca


def _sync_sources(connection: sqlite3.Connection, root: Path) -> None:
    # Never hold a SQLite transaction while acquiring a canonical source lock.
    if index.metadata(connection, "queue") != index.fingerprint(queue.queue_path(root)):
        with queue_lock(root):
            entries = queue.load_entries(root)
            index.sync_source(
                connection,
                root,
                "queue",
                queue.QUEUE_FILE_NAME,
                [queue.entry_to_dict(entry) for entry in entries],
            )
    filename = locations.JOB_LOCATION_INDEX_FILE_NAME
    if index.metadata(connection, "location") != index.fingerprint(root / filename):
        with file_lock(root / locations.JOB_LOCATION_INDEX_LOCK_NAME):
            records = locations._load_records(root)
            index.sync_source(
                connection, root, "location", filename, [asdict(record) for record in records]
            )


def _sources_current(connection: sqlite3.Connection, root: Path) -> bool:
    return index.metadata(connection, "queue") == index.fingerprint(
        queue.queue_path(root)
    ) and index.metadata(connection, "location") == index.fingerprint(
        root / locations.JOB_LOCATION_INDEX_FILE_NAME
    )


def _group_snapshots(
    connection: sqlite3.Connection,
    root: Path,
    tokens: set[str],
) -> tuple[list[sqlite3.Row], list[Any], list[RunSnapshot]]:
    previous: set[tuple[str, str]] | None = None
    entries: list[Any] = []
    snapshots: list[RunSnapshot] = []
    while True:
        sources = index.related_sources(connection, tokens)
        identities = {(row["kind"], row["id"]) for row in sources}
        if identities == previous:
            return sources, entries, snapshots
        previous = identities
        sources.sort(key=lambda row: row["position"])
        entries = [
            queue.entry_from_dict(json.loads(row["body"]))
            for row in sources
            if row["kind"] == "queue"
        ]
        entries = [entry for entry in entries if adapter.is_orca_queue_entry(entry)]
        records = [
            JobLocationRecord(**json.loads(row["body"]))
            for row in sources
            if row["kind"] == "location"
        ]
        snapshots = collect_run_snapshots(
            root,
            discover_unindexed=False,
            location_records=records,
            known_dirs=(
                Path(adapter.queue_entry_reaction_dir(entry))
                for entry in entries
                if adapter.queue_entry_reaction_dir(entry)
            ),
            synchronize=True,
        )
        # A new state run id can connect an older queue path to an organized
        # location. Expand through both old cached and new snapshot identities.
        for snapshot in snapshots:
            if snapshot.run_id:
                tokens.add("r:" + snapshot.run_id)
            tokens.add(index.path_token(str(snapshot.reaction_dir)))


def _snapshot_payload(snapshot: RunSnapshot) -> dict[str, Any]:
    body = asdict(snapshot)
    body["reaction_dir"] = str(snapshot.reaction_dir)
    return body


def _decode_snapshot(body: dict[str, Any] | None) -> RunSnapshot | None:
    if body is None:
        return None
    body = dict(body)
    body["reaction_dir"] = Path(body["reaction_dir"])
    return RunSnapshot(**body)


def _store_activity(
    connection: sqlite3.Connection, kind: str, key: str, record: ActivityRecord
) -> None:
    updated, submitted, activity_id = sort_key(record)
    connection.execute(
        "INSERT OR REPLACE INTO activities VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kind,
            key,
            record.status,
            updated.isoformat(),
            submitted.isoformat(),
            activity_id,
            json.dumps(record.to_dict()),
            bool(record.metadata.get("publication_blocked_reason")),
        ),
    )


def _store_group(
    connection: sqlite3.Connection,
    root: Path,
    sources: list[sqlite3.Row],
    entries: list[Any],
    snapshots: list[RunSnapshot],
) -> None:
    for row in sources:
        if row["kind"] == "snapshot":
            index.replace_source(connection, "snapshot", row["id"], 0, None)
        elif row["kind"] == "queue":
            connection.execute("DELETE FROM activities WHERE kind='queue' AND id=?", (row["id"],))
            connection.execute("DELETE FROM watches WHERE kind='queue' AND id=?", (row["id"],))
    for position, snapshot in enumerate(snapshots):
        index.replace_source(
            connection,
            "snapshot",
            str(snapshot.reaction_dir.resolve()),
            position,
            _snapshot_payload(snapshot),
        )
    rows = _orca.materialized_records(entries, snapshots, root)
    for kind, key, record in rows:
        _store_activity(connection, kind, key, record)
    by_run, by_dir = _orca.snapshot_indexes(snapshots)
    for entry in entries:
        if adapter.queue_entry_status(entry) != "running":
            continue
        matched = _orca.snapshot_matches_entry(adapter, entry, by_run, by_dir)
        payload: dict[str, Any] = {
            "entry": queue.entry_to_dict(entry),
            "snapshot": _snapshot_payload(matched) if matched else None,
        }
        connection.execute(
            "INSERT OR REPLACE INTO watches VALUES ('queue', ?, ?)",
            (entry.queue_id, json.dumps(payload)),
        )
    represented = {
        matched_snapshot.key
        for entry in entries
        if (matched_snapshot := _orca.snapshot_matches_entry(adapter, entry, by_run, by_dir))
        is not None
        and _orca.queue_represents_snapshot(adapter, entry, matched_snapshot)
    }
    superseded = _orca.superseded_snapshot_dirs(adapter, entries)
    for snapshot in snapshots:
        if snapshot.key in represented:
            continue
        suppressed = str(snapshot.reaction_dir.resolve()) in superseded
        if snapshot.status in {"running", "retrying"} or suppressed:
            payload = {"snapshot": _snapshot_payload(snapshot), "superseded": suppressed}
            connection.execute(
                "INSERT OR REPLACE INTO watches VALUES ('snapshot', ?, ?)",
                (str(snapshot.reaction_dir.resolve()), json.dumps(payload)),
            )


def _refresh_locks(connection: sqlite3.Connection, root: Path) -> None:
    # Only OS lock state can change without a canonical state/source ticket.
    # Retain the already-matched generation context so polling one live rerun
    # does not traverse every historical generation sharing its directory.
    for row in connection.execute("SELECT kind,id,body FROM watches").fetchall():
        payload = json.loads(row["body"])
        snapshot = _decode_snapshot(payload["snapshot"])
        if row["kind"] == "queue":
            record = _orca.queue_record(
                adapter, queue.entry_from_dict(payload["entry"]), snapshot, allowed_root=root
            )
        else:
            assert snapshot is not None
            if payload["superseded"] and _orca._snapshot_is_superseded(
                snapshot, {str(snapshot.reaction_dir.resolve())}
            ):
                connection.execute(
                    "DELETE FROM activities WHERE kind=? AND id=?", (row["kind"], row["id"])
                )
                continue
            record = _orca.snapshot_record(snapshot, allowed_root=root)
        _store_activity(connection, row["kind"], row["id"], record)


def _refresh(connection: sqlite3.Connection, root: Path) -> None:
    for _attempt in range(5):
        _sync_sources(connection, root)
        revision = index.metadata(connection, "revision")
        tickets = journal.capture(root)
        tokens = {row[0] for row in connection.execute("SELECT token FROM dirty")}
        tokens.update(index.path_token(path) for path in tickets)
        sources, entries, snapshots = _group_snapshots(connection, root, tokens)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if index.metadata(connection, "revision") != revision or not _sources_current(
                connection, root
            ):
                continue
            _store_group(connection, root, sources, entries, snapshots)
            _refresh_locks(connection, root)
            connection.execute("DELETE FROM dirty")
        journal.acknowledge(root, tickets)
        # A writer arriving during materialization leaves its newer ticket or
        # source revision intact. Retry instead of returning an obsolete page.
        if (
            not journal.capture(root)
            and _sources_current(connection, root)
            and index.metadata(connection, "revision") == revision
        ):
            return
    raise index.ActivityIndexError("Activity sources kept changing during query; retry queue list")


def _select(connection: sqlite3.Connection, request: ActivityListRequest) -> list[ActivityRecord]:
    matches = (not request.engines or "orca" in request.engines) and (
        not request.kinds or "job" in request.kinds
    )
    selected = {}
    # A separate bounded range per status avoids SQLite sorting every matching
    # historical row for a multi-status IN predicate. The caller merges top K.
    for status in (request.statuses or ("",)) if matches else ():
        where = " WHERE status=?" if status else ""
        values: list[Any] = [status] if status else []
        sql = (
            "SELECT kind,id,body FROM activities"
            + where
            + " ORDER BY updated DESC, submitted DESC, activity_id DESC"
        )
        if request.limit > 0:
            sql += " LIMIT ?"
            values.append(request.limit)
        selected.update(
            {(row["kind"], row["id"]): row["body"] for row in connection.execute(sql, values)}
        )
    # Global summaries must survive filters and limits. These indexed lookups
    # add only live jobs/blockers, not the rest of the historical catalog.
    for query in (
        "SELECT kind,id,body FROM activities WHERE status IN ('running','retrying','cancel_requested')",
        "SELECT kind,id,body FROM activities WHERE blocked=1",
    ):
        for row in connection.execute(query):
            selected[(row["kind"], row["id"])] = row["body"]
    records = []
    for body in selected.values():
        payload = json.loads(body)
        payload["aliases"] = tuple(payload["aliases"])
        records.append(ActivityRecord(**payload))
    return records


def query_records(root: Path, request: ActivityListRequest) -> list[ActivityRecord]:
    if not root.is_dir():
        return []
    root = root.resolve()
    try:
        with file_lock(root / ".activity-query.lock"):
            # Establish the invalidation rendezvous before reading ANY state.
            (root / journal.DIR_NAME).mkdir(exist_ok=True)
            with closing(index.connect(root)) as connection:
                index.initialize(connection, root)
                (root / index.DB_NAME).chmod(0o600)
                if request.refresh:
                    with connection:
                        for table in ("sources", "links", "dirty", "activities", "watches"):
                            connection.execute(f"DELETE FROM {table}")
                        connection.execute("DELETE FROM meta WHERE key IN ('queue','location')")
                        connection.execute(
                            "UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'"
                        )
                for _attempt in range(5):
                    _refresh(connection, root)
                    with connection:
                        connection.execute("BEGIN")
                        records = _select(connection, request)
                        if (
                            not connection.execute("SELECT 1 FROM dirty LIMIT 1").fetchone()
                            and _sources_current(connection, root)
                            and not journal.capture(root)
                        ):
                            return records
                raise index.ActivityIndexError(
                    "Activity sources kept changing during selection; retry queue list"
                )
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise index.ActivityIndexError(
            f"Cannot refresh activity projection at {root / index.DB_NAME}: {exc}. "
            "Remove this disposable database and retry queue list to rebuild it."
        ) from exc
