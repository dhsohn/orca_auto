from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orca_auto.core.messaging import (
    Group,
    Line,
    Message,
    bold,
    build_channel_from_config_path,
    code,
    field_row,
    group,
    line,
    raw,
    text,
    title_heading,
)
from orca_auto.core.utils import (
    coerce_list as _coerce_sequence,
)
from orca_auto.core.utils import (
    mapping_or_empty as _coerce_mapping,
)
from orca_auto.core.utils import (
    normalize_text as _normalize_text,
)
from orca_auto.core.utils import (
    now_utc_iso,
)
from orca_auto.core.utils import (
    safe_int as _safe_int,
)

from .status import workflow_status_is_terminal

_ACTIVE_STATUSES = frozenset(
    {"planned", "queued", "running", "submitted", "cancel_requested", "retrying"}
)


@dataclass(frozen=True)
class _PhaseSummary:
    engine: str
    state_key: str
    stages: list[dict[str, Any]]
    counts: dict[str, int]
    stage_buckets: dict[int, str]


@dataclass(frozen=True)
class _PhaseStageRow:
    stage_label: str
    result: str
    metrics: tuple[tuple[str, Any], ...]


def _phase_notification_state(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        payload["metadata"] = {}
        metadata = payload["metadata"]
    state = metadata.get("phase_notifications")
    if isinstance(state, dict):
        return state
    metadata["phase_notifications"] = {}
    return metadata["phase_notifications"]


def _stage_engine(stage: dict[str, Any]) -> str:
    return _normalize_text(_coerce_mapping(stage.get("task")).get("engine")).lower()


def _stage_metadata(stage: dict[str, Any]) -> dict[str, Any]:
    return _coerce_mapping(stage.get("metadata"))


def _stage_task_payload(stage: dict[str, Any]) -> dict[str, Any]:
    task = _coerce_mapping(stage.get("task"))
    return _coerce_mapping(task.get("payload"))


def _stage_is_terminal(
    stage: dict[str, Any],
    *,
    stage_failure_is_recoverable_fn: Callable[[dict[str, Any]], bool] | None,
) -> bool:
    status = _normalize_text(stage.get("status")).lower()
    task_status = _normalize_text(_coerce_mapping(stage.get("task")).get("status")).lower()
    if status in _ACTIVE_STATUSES or task_status in _ACTIVE_STATUSES:
        return False
    if stage_failure_is_recoverable_fn is not None and stage_failure_is_recoverable_fn(stage):
        return True
    return workflow_status_is_terminal(status) or workflow_status_is_terminal(task_status)


def _terminal_phase_stages(
    payload: dict[str, Any],
    *,
    phase_engine: str,
    stage_failure_is_recoverable_fn: Callable[[dict[str, Any]], bool] | None,
) -> list[dict[str, Any]] | None:
    stages = [
        stage
        for stage in payload.get("stages", [])
        if isinstance(stage, dict) and _stage_engine(stage) == phase_engine
    ]
    if not stages:
        return None
    if not all(
        _stage_is_terminal(stage, stage_failure_is_recoverable_fn=stage_failure_is_recoverable_fn)
        for stage in stages
    ):
        return None
    return stages


def _stage_result_bucket(
    stage: dict[str, Any],
    *,
    phase_engine: str,
    stage_failure_is_recoverable_fn: Callable[[dict[str, Any]], bool] | None,
) -> str:
    status = _normalize_text(stage.get("status")).lower()
    metadata = _stage_metadata(stage)
    if stage_failure_is_recoverable_fn is not None and stage_failure_is_recoverable_fn(stage):
        return "completed"
    if (
        phase_engine == "xtb"
        and _normalize_text(metadata.get("reaction_handoff_status")).lower() == "ready"
    ):
        return "completed"
    if status == "completed":
        return "completed"
    if status == "cancelled":
        return "cancelled"
    return "failed"


def _count_output_artifacts(stage: dict[str, Any]) -> int:
    return len(
        [item for item in _coerce_sequence(stage.get("output_artifacts")) if isinstance(item, dict)]
    )


def _xtb_candidate_count(stage: dict[str, Any]) -> int:
    metadata = _stage_metadata(stage)
    attempts = [
        item for item in _coerce_sequence(metadata.get("xtb_attempts")) if isinstance(item, dict)
    ]
    if attempts:
        latest = attempts[-1]
        return _safe_int(latest.get("candidate_count"), default=0)
    return _count_output_artifacts(stage)


def _phase_label(phase_engine: str) -> str:
    return {"crest": "CREST", "xtb": "xTB"}.get(phase_engine, phase_engine.upper())


def _phase_outcome(counts: dict[str, int]) -> str:
    failed = counts.get("failed", 0)
    cancelled = counts.get("cancelled", 0)
    completed = counts.get("completed", 0)
    if failed and completed:
        return "mixed"
    if failed:
        return "failed"
    if cancelled and completed:
        return "mixed"
    if cancelled:
        return "cancelled"
    if completed:
        return "completed"
    return "unknown"


def _phase_stage_row(
    stage: dict[str, Any],
    *,
    phase_engine: str,
    bucket: str,
) -> _PhaseStageRow:
    stage_id = _normalize_text(stage.get("stage_id")) or "stage"
    status = _normalize_text(stage.get("status")).lower() or "unknown"
    task_payload = _stage_task_payload(stage)
    metadata = _stage_metadata(stage)
    if phase_engine == "crest":
        role = _normalize_text(task_payload.get("input_role")) or stage_id
        return _PhaseStageRow(
            stage_label=role,
            result=bucket,
            metrics=(
                ("Status", status),
                ("Retained conformers", _count_output_artifacts(stage)),
            ),
        )
    if phase_engine == "xtb":
        reaction_key = _normalize_text(task_payload.get("reaction_key")) or stage_id
        handoff_status = _normalize_text(metadata.get("reaction_handoff_status")).lower() or "none"
        return _PhaseStageRow(
            stage_label=reaction_key,
            result=bucket,
            metrics=(
                ("Status", status),
                ("Handoff", handoff_status),
                ("Candidates", _xtb_candidate_count(stage)),
            ),
        )
    return _PhaseStageRow(
        stage_label=stage_id,
        result=bucket,
        metrics=(("Status", status),),
    )


def _stage_row_lines(row: _PhaseStageRow) -> list[Line]:
    lines = [
        line(
            bold("Stage"),
            raw(": "),
            text(row.stage_label),
            raw("  "),
            bold("Result"),
            raw(": "),
            code(row.result),
        )
    ]
    if row.metrics:
        spans = []
        for index, (label, value) in enumerate(row.metrics):
            if index:
                spans.append(raw("  "))
            spans.extend((bold(label), raw(": "), code(value)))
        lines.append(line(*spans))
    return lines


def _notes_group(extra_lines: list[str] | None) -> Group | None:
    items: list[Any] = []
    for raw_line in extra_lines or []:
        entry = _normalize_text(raw_line)
        if not entry:
            continue
        if ":" in entry:
            key, value = entry.split(":", 1)
            normalized_key = _normalize_text(key)
            normalized_value = _normalize_text(value) or "-"
            if normalized_key:
                items.append(field_row(normalized_key, code(normalized_value)))
                continue
        items.append(line(text(entry)))
    if not items:
        return None
    return group(*items, heading=(bold("Notes"),))


def _overview_fields(
    *,
    payload: dict[str, Any],
    phase_engine: str,
    stages: list[dict[str, Any]],
    counts: dict[str, int],
) -> list[Any]:
    workflow_id = _normalize_text(payload.get("workflow_id")) or "-"
    template_name = _normalize_text(payload.get("template_name")) or "-"
    fields = [
        field_row("Workflow", code(workflow_id)),
        field_row("Template", code(template_name)),
        field_row("Outcome", code(_phase_outcome(counts))),
        field_row(
            "Stages",
            code(len(stages)),
            raw("  completed="),
            code(counts["completed"]),
            raw("  failed="),
            code(counts["failed"]),
            raw("  cancelled="),
            code(counts["cancelled"]),
        ),
    ]
    if phase_engine == "xtb":
        ready_count = sum(
            1
            for stage in stages
            if _normalize_text(_stage_metadata(stage).get("reaction_handoff_status")).lower()
            == "ready"
        )
        fields.append(field_row("Ready for ORCA", code(ready_count)))
    return fields


def _build_phase_summary_message(
    *,
    payload: dict[str, Any],
    phase_engine: str,
    stages: list[dict[str, Any]],
    counts: dict[str, int],
    stage_buckets: dict[int, str],
    extra_lines: list[str] | None,
) -> Message:
    title = f"orca_auto Flow {_phase_label(phase_engine)} Phase Summary"
    overview = group(
        *_overview_fields(payload=payload, phase_engine=phase_engine, stages=stages, counts=counts),
        heading=title_heading(title),
    )
    groups: list[Group] = [overview]

    notes = _notes_group(extra_lines)
    if notes is not None:
        groups.append(notes)

    stage_rows = [
        _phase_stage_row(
            stage, phase_engine=phase_engine, bucket=stage_buckets.get(id(stage), "failed")
        )
        for stage in stages
    ]
    for index, row in enumerate(stage_rows):
        heading = (bold("Stage details"),) if index == 0 else ()
        groups.append(group(*_stage_row_lines(row), heading=heading))

    return Message(title=title, severity=_summary_severity(counts), groups=tuple(groups))


def _summary_severity(counts: dict[str, int]) -> Any:
    outcome = _phase_outcome(counts)
    return {
        "completed": "success",
        "failed": "error",
        "mixed": "warning",
        "cancelled": "warning",
    }.get(outcome, "info")


def _phase_summary_counts(
    stages: list[dict[str, Any]],
    *,
    phase_engine: str,
    stage_failure_is_recoverable_fn: Callable[[dict[str, Any]], bool] | None,
) -> tuple[dict[str, int], dict[int, str]]:
    counts = {"completed": 0, "failed": 0, "cancelled": 0}
    stage_buckets: dict[int, str] = {}
    for stage in stages:
        bucket = _stage_result_bucket(
            stage,
            phase_engine=phase_engine,
            stage_failure_is_recoverable_fn=stage_failure_is_recoverable_fn,
        )
        counts[bucket] += 1
        stage_buckets[id(stage)] = bucket
    return counts, stage_buckets


def _phase_summary(
    payload: dict[str, Any],
    *,
    phase_engine: str,
    stage_failure_is_recoverable_fn: Callable[[dict[str, Any]], bool] | None,
) -> _PhaseSummary | None:
    stages = _terminal_phase_stages(
        payload,
        phase_engine=phase_engine,
        stage_failure_is_recoverable_fn=stage_failure_is_recoverable_fn,
    )
    if not stages:
        return None
    counts, stage_buckets = _phase_summary_counts(
        stages,
        phase_engine=phase_engine,
        stage_failure_is_recoverable_fn=stage_failure_is_recoverable_fn,
    )
    return _PhaseSummary(
        engine=phase_engine,
        state_key=f"{phase_engine}_summary",
        stages=stages,
        counts=counts,
        stage_buckets=stage_buckets,
    )


def _phase_summary_already_sent(
    notification_state: dict[str, Any],
    *,
    state_key: str,
) -> bool:
    previous_state = _coerce_mapping(notification_state.get(state_key))
    return bool(previous_state.get("sent_at"))


def _mark_phase_summary_sent(
    notification_state: dict[str, Any],
    *,
    state_key: str,
    stage_count: int,
) -> None:
    notification_state[state_key] = {
        "sent_at": now_utc_iso(),
        "stage_count": stage_count,
    }


def maybe_notify_workflow_phase_summary(
    *,
    payload: dict[str, Any],
    config_path: str | None,
    phase_engine: str,
    stage_failure_is_recoverable_fn: Callable[[dict[str, Any]], bool] | None = None,
    extra_lines: list[str] | None = None,
) -> bool:
    normalized_engine = _normalize_text(phase_engine).lower()
    if normalized_engine not in {"crest", "xtb"}:
        return False

    summary = _phase_summary(
        payload,
        phase_engine=normalized_engine,
        stage_failure_is_recoverable_fn=stage_failure_is_recoverable_fn,
    )
    if summary is None:
        return False

    notification_state = _phase_notification_state(payload)
    if _phase_summary_already_sent(notification_state, state_key=summary.state_key):
        return False

    channel = build_channel_from_config_path(config_path)
    if not channel.enabled:
        return False

    message = _build_phase_summary_message(
        payload=payload,
        phase_engine=summary.engine,
        stages=summary.stages,
        counts=summary.counts,
        stage_buckets=summary.stage_buckets,
        extra_lines=extra_lines,
    )
    if not channel.send(message).sent:
        return False

    _mark_phase_summary_sent(
        notification_state,
        state_key=summary.state_key,
        stage_count=len(summary.stages),
    )
    return True


__all__ = ["maybe_notify_workflow_phase_summary"]
