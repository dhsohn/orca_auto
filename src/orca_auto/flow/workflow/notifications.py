from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orca_auto.core.config import TelegramConfig
from orca_auto.core.notifications import (
    build_telegram_transport,
    load_telegram_config_from_file,
    split_telegram_message,
)
from orca_auto.core.notifications import (
    escape_html as _escape_html,
)
from orca_auto.core.notifications import (
    html_code as _metric_code,
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


def _load_telegram_config(config_path: str | None) -> TelegramConfig:
    return load_telegram_config_from_file(config_path)


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


def _format_phase_stage_row(row: _PhaseStageRow) -> str:
    lines = [
        f"<b>Stage</b>: {_escape_html(row.stage_label)}  <b>Result</b>: {_metric_code(row.result)}"
    ]
    if row.metrics:
        lines.append(
            "  ".join(f"<b>{label}</b>: {_metric_code(value)}" for label, value in row.metrics)
        )
    return "\n".join(lines)


def _extra_lines_section(extra_lines: list[str] | None) -> str | None:
    rows: list[str] = []
    for raw_line in extra_lines or []:
        line = _normalize_text(raw_line)
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            normalized_key = _normalize_text(key)
            normalized_value = _normalize_text(value) or "-"
            if normalized_key:
                rows.append(
                    f"<b>{_escape_html(normalized_key)}</b>: {_metric_code(normalized_value)}"
                )
                continue
        rows.append(_escape_html(line))
    if not rows:
        return None
    return "<b>Notes</b>\n" + "\n".join(rows)


def _format_phase_summary_message(
    *,
    payload: dict[str, Any],
    phase_engine: str,
    stages: list[dict[str, Any]],
    counts: dict[str, int],
    stage_buckets: dict[int, str],
    extra_lines: list[str] | None,
) -> str:
    phase = _phase_label(phase_engine)
    workflow_id = _normalize_text(payload.get("workflow_id")) or "-"
    template_name = _normalize_text(payload.get("template_name")) or "-"
    outcome = _phase_outcome(counts)

    overview = [
        f"<b>orca_auto Flow {phase} Phase Summary</b>",
        f"<b>Workflow</b>: {_metric_code(workflow_id)}",
        f"<b>Template</b>: {_metric_code(template_name)}",
        f"<b>Outcome</b>: {_metric_code(outcome)}",
        (
            f"<b>Stages</b>: {_metric_code(len(stages))}  "
            f"completed={_metric_code(counts['completed'])}  "
            f"failed={_metric_code(counts['failed'])}  "
            f"cancelled={_metric_code(counts['cancelled'])}"
        ),
    ]
    if phase_engine == "xtb":
        ready_count = sum(
            1
            for stage in stages
            if _normalize_text(_stage_metadata(stage).get("reaction_handoff_status")).lower()
            == "ready"
        )
        overview.append(f"<b>Ready for ORCA</b>: {_metric_code(ready_count)}")

    stage_rows = [
        _phase_stage_row(
            stage,
            phase_engine=phase_engine,
            bucket=stage_buckets.get(id(stage), "failed"),
        )
        for stage in stages
    ]

    sections: list[str] = ["\n".join(overview)]
    notes = _extra_lines_section(extra_lines)
    if notes is not None:
        sections.append(notes)
    if stage_rows:
        sections.append(
            "<b>Stage details</b>\n"
            + "\n\n".join(_format_phase_stage_row(row) for row in stage_rows)
        )
    return "\n\n".join(sections)


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


def _send_phase_summary_message(telegram: TelegramConfig, message: str) -> bool:
    chunks = split_telegram_message(message)
    if not chunks:
        return False

    transport = build_telegram_transport(telegram)
    for chunk in chunks:
        result = transport.send_text(chunk, parse_mode="HTML")
        if result.sent or result.skipped:
            continue
        fallback_result = transport.send_text(chunk, parse_mode=None)
        if not (fallback_result.sent or fallback_result.skipped):
            return False
    return True


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

    telegram = _load_telegram_config(config_path)
    if not telegram.enabled:
        return False

    message = _format_phase_summary_message(
        payload=payload,
        phase_engine=summary.engine,
        stages=summary.stages,
        counts=summary.counts,
        stage_buckets=summary.stage_buckets,
        extra_lines=extra_lines,
    )
    if not _send_phase_summary_message(telegram, message):
        return False

    _mark_phase_summary_sent(
        notification_state,
        state_key=summary.state_key,
        stage_count=len(summary.stages),
    )
    return True


__all__ = ["maybe_notify_workflow_phase_summary"]
