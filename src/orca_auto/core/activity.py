from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import STATUS_CANCEL_REQUESTED, STATUS_RETRYING, STATUS_RUNNING
from orca_auto.core.utils import normalize_text, parse_iso_utc

ACTIVE_SIMULATION_STATUSES = frozenset({STATUS_RUNNING, STATUS_RETRYING, STATUS_CANCEL_REQUESTED})


@dataclass(frozen=True)
class ActivitySourceRequest:
    orca_config: str | None = None
    shared_config: str | None = None


@dataclass(frozen=True)
class ActivityListRequest:
    sources: ActivitySourceRequest
    refresh: bool = False
    limit: int = 0
    indexed: bool = False
    statuses: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActivityCancelRequest:
    target: str
    sources: ActivitySourceRequest


@dataclass(frozen=True)
class ResolvedActivitySources:
    orca_config: str | None


@dataclass(frozen=True)
class ActivityRecord:
    activity_id: str
    kind: str
    engine: str
    status: str
    label: str
    source: str
    submitted_at: str
    updated_at: str
    cancel_target: str
    aliases: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity_id": self.activity_id,
            "kind": self.kind,
            "engine": self.engine,
            "status": self.status,
            "label": self.label,
            "source": self.source,
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
            "cancel_target": self.cancel_target,
            "aliases": list(self.aliases),
            "metadata": dict(self.metadata),
        }


def sort_key(record: ActivityRecord) -> tuple[datetime, datetime, str]:
    return (
        parse_iso_utc(record.updated_at) or datetime.min.replace(tzinfo=UTC),
        parse_iso_utc(record.submitted_at) or datetime.min.replace(tzinfo=UTC),
        record.activity_id,
    )


@dataclass(frozen=True)
class ActivityListing:
    """One page of activities plus the catalog-wide summaries a list shows.

    ``records`` is already status-filtered, newest first and cut to the
    requested limit; nobody downstream filters or slices again. ``blockers``
    and ``active_count`` describe the whole catalog regardless of the page.
    """

    records: tuple[ActivityRecord, ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()
    active_count: int = 0


def blocker_payload(record: ActivityRecord) -> dict[str, Any] | None:
    metadata = record.metadata
    reason = normalize_text(metadata.get("publication_blocked_reason"))
    if not reason:
        return None
    return {
        "queue_id": metadata.get("queue_id", record.activity_id),
        "allowed_root": metadata.get("allowed_root", ""),
        "scope": metadata.get("publication_blocked_scope", ""),
        "reason": reason,
        "next_action": metadata.get("publication_blocked_action", ""),
    }


def is_active_simulation(record: ActivityRecord) -> bool:
    return (
        normalize_text(record.kind).lower() == "job"
        and normalize_text(record.status).lower() in ACTIVE_SIMULATION_STATUSES
    )


def listing_from_records(
    records: Iterable[ActivityRecord],
    *,
    statuses: Sequence[str] = (),
    limit: int = 0,
) -> ActivityListing:
    """Filter, order and page an in-memory catalog exactly once."""
    ordered = sorted(records, key=sort_key, reverse=True)
    wanted = {normalize_text(status).lower() for status in statuses if normalize_text(status)}
    page = [
        record
        for record in ordered
        if not wanted or normalize_text(record.status).lower() in wanted
    ]
    if limit > 0:
        page = page[:limit]
    return ActivityListing(
        records=tuple(page),
        blockers=tuple(
            payload for record in ordered if (payload := blocker_payload(record)) is not None
        ),
        active_count=sum(1 for record in ordered if is_active_simulation(record)),
    )


def unique_texts(values: list[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return tuple(ordered)


def mapping_text(mapping: dict[str, Any], key: str) -> str:
    return normalize_text(mapping.get(key))


def path_aliases(path_text: str, *, root: Path | None = None) -> tuple[str, ...]:
    text = normalize_text(path_text)
    if not text:
        return ()
    try:
        path = Path(text).expanduser().resolve()
    except OSError:
        return (text,)

    aliases = [str(path), path.name]
    if root is not None:
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = None
        if relative is not None:
            aliases.extend([str(relative), relative.as_posix()])
    return unique_texts(aliases)


def timestamp_metadata(
    *,
    enqueued_at: Any = "",
    started_at: Any = "",
    finished_at: Any = "",
    elapsed_started_at: Any = "",
) -> dict[str, str]:
    enqueued_at_text = normalize_text(enqueued_at)
    started_at_text = normalize_text(started_at)
    finished_at_text = normalize_text(finished_at)
    elapsed_started_at_text = (
        normalize_text(elapsed_started_at) or started_at_text or enqueued_at_text
    )
    metadata: dict[str, str] = {}
    if enqueued_at_text:
        metadata["enqueued_at"] = enqueued_at_text
    if started_at_text:
        metadata["started_at"] = started_at_text
    if finished_at_text:
        metadata["finished_at"] = finished_at_text
    if elapsed_started_at_text:
        metadata["elapsed_started_at"] = elapsed_started_at_text
    return metadata
