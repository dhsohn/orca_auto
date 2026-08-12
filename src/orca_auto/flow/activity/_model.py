from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orca_auto.core.engine_catalog import activity_engine_entries
from orca_auto.core.utils import normalize_text, parse_iso_utc

from ..engine_options import WorkflowEngineOptions


@dataclass(frozen=True)
class ActivitySourceRequest:
    workflow_root: str | Path | None = None
    crest_config: str | None = None
    xtb_config: str | None = None
    orca_config: str | None = None
    shared_config: str | None = None


@dataclass(frozen=True)
class ActivityListRequest:
    sources: ActivitySourceRequest
    refresh: bool = False
    limit: int = 0


@dataclass(frozen=True)
class ActivityCancelRequest:
    target: str
    sources: ActivitySourceRequest
    engine_options: WorkflowEngineOptions


@dataclass(frozen=True)
class ResolvedActivitySources:
    workflow_root: str | None
    crest_config: str | None
    xtb_config: str | None
    orca_config: str | None
    engine_configs: dict[str, str | None] = field(default_factory=dict)

    def config_for_engine(self, engine: str) -> str | None:
        engine_id = normalize_text(engine).lower()
        if engine_id in self.engine_configs:
            return self.engine_configs[engine_id]
        return getattr(self, f"{engine_id}_config", None)

    @property
    def shared_config(self) -> str | None:
        for entry in activity_engine_entries():
            config = self.config_for_engine(entry.engine_id)
            text = normalize_text(config)
            if text:
                return text
        return None


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
