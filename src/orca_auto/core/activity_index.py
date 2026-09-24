"""Rebuildable activity projection; never used to authorize a queue mutation.

Canonical source writers update their mirror AFTER committing JSON. A missed
update (including a crash) is detected by the canonical file identity. State
publication uses the separate pre-write journal in activity_invalidation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import stat
from collections.abc import Iterable, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

DB_NAME = ".activity.sqlite3"
# Bump whenever the mirrored layout changes; an older projection is dropped and
# rebuilt from the canonical sources instead of being read with new rules.
# 2: location rows are keyed by job_id (position is only an ordering column).
SCHEMA_VERSION = "2"
LOGGER = logging.getLogger(__name__)
_TABLES = ("meta", "sources", "watches", "links", "dirty", "activities")


class ActivityIndexError(RuntimeError):
    """An activity query could not obtain a current, complete projection."""


def fingerprint(path: Path) -> str:
    try:
        info = path.stat()
    except FileNotFoundError:
        return "missing"
    return ":".join(
        str(value)
        for value in (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    )


def connect(root: Path) -> sqlite3.Connection:
    path = root / DB_NAME
    try:
        details = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ActivityIndexError(
                f"Activity database must be a single-link regular file: {path}"
            )
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _create_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (
            kind TEXT, id TEXT, position INTEGER, body TEXT NOT NULL,
            PRIMARY KEY (kind, id));
        CREATE TABLE IF NOT EXISTS watches (
            kind TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY (kind, id));
        CREATE TABLE IF NOT EXISTS links (
            kind TEXT, id TEXT, token TEXT, PRIMARY KEY (kind, id, token));
        CREATE INDEX IF NOT EXISTS links_token ON links(token);
        CREATE TABLE IF NOT EXISTS dirty (token TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS activities (
            kind TEXT, id TEXT, status TEXT, updated TEXT, submitted TEXT,
            activity_id TEXT, body TEXT NOT NULL, blocked INTEGER,
            PRIMARY KEY (kind, id));
        CREATE INDEX IF NOT EXISTS activity_order
            ON activities(updated DESC, submitted DESC, activity_id DESC);
        CREATE INDEX IF NOT EXISTS activity_status
            ON activities(status, updated DESC, submitted DESC, activity_id DESC);
        CREATE INDEX IF NOT EXISTS activity_blocked ON activities(blocked) WHERE blocked=1;
        """
    )


def initialize(connection: sqlite3.Connection, root: Path) -> None:
    _create_tables(connection)
    expected = {"schema": SCHEMA_VERSION, "root": str(root.resolve())}
    stored = dict(connection.execute("SELECT key, value FROM meta WHERE key IN ('schema','root')"))
    if stored.get("root", expected["root"]) != expected["root"]:
        raise ActivityIndexError("Activity projection belongs to another root")
    if stored.get("schema", expected["schema"]) != expected["schema"]:
        # A projection written under another layout is disposable: drop it and
        # let this reader rebuild deterministically from the canonical sources.
        with connection:
            for table in _TABLES:
                connection.execute(f"DROP TABLE IF EXISTS {table}")
        _create_tables(connection)
    with connection:
        connection.executemany("INSERT OR IGNORE INTO meta VALUES (?, ?)", expected.items())
        connection.execute("INSERT OR IGNORE INTO meta VALUES ('revision', '0')")


def metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row[0])


def path_token(path: str) -> str:
    path = path.strip()
    return "p:" + str(Path(path).expanduser().resolve()) if path else ""


def source_tokens(kind: str, body: dict[str, Any]) -> set[str]:
    from .utils.coercion import normalize_text

    if kind == "queue":
        value = body.get("metadata", {})
        paths = [value.get("reaction_dir") or value.get("job_dir") or ""]
        run_id = value.get("run_id", "")
    elif kind == "location":
        paths = [body.get("original_run_dir", ""), body.get("latest_known_path", "")]
        run_id = ""
    else:
        paths = [body["reaction_dir"]]
        run_id = body.get("run_id", "")
    run_id = normalize_text(run_id)
    return {token for path in paths if (token := path_token(normalize_text(path)))} | (
        {"r:" + run_id} if run_id else set()
    )


def mark_dirty(connection: sqlite3.Connection, tokens: Iterable[str]) -> None:
    connection.executemany("INSERT OR IGNORE INTO dirty VALUES (?)", ((t,) for t in tokens))


def replace_source(
    connection: sqlite3.Connection,
    kind: str,
    key: str,
    position: int,
    body: dict[str, Any] | None,
) -> None:
    connection.execute("DELETE FROM links WHERE kind=? AND id=?", (kind, key))
    if body is None:
        connection.execute("DELETE FROM sources WHERE kind=? AND id=?", (kind, key))
        connection.execute("DELETE FROM activities WHERE kind=? AND id=?", (kind, key))
        connection.execute("DELETE FROM watches WHERE kind=? AND id=?", (kind, key))
        return
    connection.execute(
        "INSERT OR REPLACE INTO sources(kind,id,position,body) VALUES (?, ?, ?, ?)",
        (kind, key, position, json.dumps(body, sort_keys=True)),
    )
    connection.executemany(
        "INSERT INTO links VALUES (?, ?, ?)",
        ((kind, key, token) for token in source_tokens(kind, body)),
    )


def source_key(kind: str, body: dict[str, Any], position: int, seen: set[str]) -> str:
    """The stable identity of one canonical row inside the projection.

    Queue rows carry a unique queue id and location rows a job id that the
    index store keeps unique per upsert, so removing or appending a row leaves
    every other key unchanged. Other kinds have no identity beyond their slot.
    A duplicated identity (a hand-edited file) is suffixed with its slot so the
    mirror stays deterministic instead of overwriting one row with the other.
    """
    if kind == "queue":
        key = str(body["queue_id"])
    elif kind == "location":
        key = str(body.get("job_id", ""))
    else:
        key = str(position)
    if key in seen:
        key = f"{key}#{position}"
    seen.add(key)
    return key


def sync_source(
    connection: sqlite3.Connection,
    root: Path,
    kind: str,
    filename: str,
    records: Sequence[dict[str, Any]],
) -> None:

    old = {
        str(row["id"]): row
        for row in connection.execute("SELECT * FROM sources WHERE kind=?", (kind,))
    }
    seen: set[str] = set()
    with connection:
        for position, body in enumerate(records):
            key = source_key(kind, body, position, seen)
            previous = old.pop(key, None)
            encoded = json.dumps(body, sort_keys=True)
            if previous is not None and previous["body"] == encoded:
                # Only the slot moved (a row before it was pruned): the ordering
                # column follows, but nothing this row materialized has changed.
                if previous["position"] != position:
                    connection.execute(
                        "UPDATE sources SET position=? WHERE kind=? AND id=?",
                        (position, kind, key),
                    )
                continue
            tokens = source_tokens(kind, body)
            if previous is not None:
                tokens |= source_tokens(kind, json.loads(previous["body"]))
            # Queue rows without a path still need materialization.
            tokens.add(f"{kind}:{key}")
            mark_dirty(connection, tokens)
            replace_source(connection, kind, key, position, body)
        for key, previous in old.items():
            mark_dirty(connection, source_tokens(kind, json.loads(previous["body"])))
            replace_source(connection, kind, key, 0, None)
        connection.execute(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)", (kind, fingerprint(root / filename))
        )
        connection.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")


def published_source(
    root: Path, kind: str, filename: str, records: Sequence[dict[str, Any]]
) -> None:
    try:
        if not (root / DB_NAME).is_file():
            return
        with closing(connect(root)) as connection:
            # Initialization/rebuild belongs exclusively to the query reader.
            if metadata(connection, "root") != str(root.resolve()):
                return
            if metadata(connection, "schema") != SCHEMA_VERSION:
                return
            sync_source(connection, root, kind, filename, records)
    except (sqlite3.Error, OSError, ActivityIndexError) as exc:
        # The canonical commit already succeeded. Its changed fingerprint makes
        # the next reader repair or report an error; never claim a queue failure.
        LOGGER.warning("Activity projection update deferred: %s", exc)


def related_sources(connection: sqlite3.Connection, tokens: set[str]) -> list[sqlite3.Row]:
    found: dict[tuple[str, str], sqlite3.Row] = {}
    remaining = set(tokens)
    visited: set[str] = set()
    while remaining:
        batch = sorted(remaining)[:400]
        remaining.difference_update(batch)
        visited.update(batch)
        marks = ",".join("?" for _ in batch)
        rows = list(
            connection.execute(
                f"SELECT DISTINCT s.* FROM sources s JOIN links l USING(kind,id) WHERE l.token IN ({marks})",
                batch,
            )
        )
        for token in batch:
            kind, key = token.split(":", 1)
            if kind in {"queue", "location", "snapshot"}:
                rows.extend(
                    connection.execute("SELECT * FROM sources WHERE kind=? AND id=?", (kind, key))
                )
        for row in rows:
            identity = (str(row["kind"]), str(row["id"]))
            if identity in found:
                continue
            found[identity] = row
            remaining.update(source_tokens(identity[0], json.loads(row["body"])) - visited)
    return list(found.values())
