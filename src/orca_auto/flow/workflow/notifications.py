from __future__ import annotations

import logging
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
)
from orca_auto.core.statuses import STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED
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

from ._phases import phase_snapshot
from .stage_summary import crest_stage_detail

LOGGER = logging.getLogger(__name__)


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
    state = {}
    metadata["phase_notifications"] = state
    return state


def _raw_stages_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    stages: dict[str, dict[str, Any]] = {}
    for stage in payload.get("stages", []):
        if not isinstance(stage, dict):
            continue
        stage_id = _normalize_text(stage.get("stage_id"))
        if stage_id and stage_id not in stages:
            stages[stage_id] = stage
    return stages


def _count_output_artifacts(stage: dict[str, Any]) -> int:
    return len(
        [item for item in _coerce_sequence(stage.get("output_artifacts")) if isinstance(item, dict)]
    )


def _crest_conformer_count(stage: dict[str, Any]) -> int | None:
    """Conformers CREST actually retained, or ``None`` when unreadable.

    ``output_artifacts`` counts the named ensemble FILES that survived
    validation, which is capped at four by the engine's candidate list — it is
    not a conformer count. The ensemble file itself carries the real number.
    """
    return crest_stage_detail(stage)[1]


def _xtb_candidate_count(stage: dict[str, Any]) -> int:
    metadata = _coerce_mapping(stage.get("metadata"))
    attempts = [
        item for item in _coerce_sequence(metadata.get("xtb_attempts")) if isinstance(item, dict)
    ]
    if attempts:
        latest = attempts[-1]
        return _safe_int(latest.get("candidate_count"), default=0)
    return _count_output_artifacts(stage)


def _phase_label(phase_engine: str) -> str:
    return {"crest": "CREST", "xtb": "xTB"}.get(phase_engine, phase_engine.upper())


def _phase_stage_row(
    snapshot_row: dict[str, str],
    raw_stage: dict[str, Any],
    *,
    phase_engine: str,
) -> _PhaseStageRow:
    stage_label = (
        _normalize_text(snapshot_row.get("label"))
        or _normalize_text(snapshot_row.get("stage_id"))
        or "stage"
    )
    status = _normalize_text(snapshot_row.get("status")).lower() or "unknown"
    result = _normalize_text(snapshot_row.get("result")).lower() or STATUS_FAILED
    if phase_engine == "crest":
        conformers = _crest_conformer_count(raw_stage)
        return _PhaseStageRow(
            stage_label=stage_label,
            result=result,
            metrics=(
                ("Status", status),
                ("Conformers", "-" if conformers is None else conformers),
            ),
        )
    if phase_engine == "xtb":
        handoff_status = (
            _normalize_text(snapshot_row.get("reaction_handoff_status")).lower() or "none"
        )
        return _PhaseStageRow(
            stage_label=stage_label,
            result=result,
            metrics=(
                ("Status", status),
                ("Handoff", handoff_status),
                ("Candidates", _xtb_candidate_count(raw_stage)),
            ),
        )
    return _PhaseStageRow(
        stage_label=stage_label,
        result=result,
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


def _result_count(snapshot: dict[str, Any], result: str) -> int:
    return _safe_int(_coerce_mapping(snapshot.get("result_counts")).get(result), default=0)


def _overview_fields(
    *,
    payload: dict[str, Any],
    phase_engine: str,
    snapshot: dict[str, Any],
) -> list[Any]:
    workflow_id = _normalize_text(payload.get("workflow_id")) or "-"
    template_name = _normalize_text(payload.get("template_name")) or "-"
    fields = [
        field_row("Workflow", code(workflow_id), inline=True),
        field_row("Template", code(template_name), inline=True),
        field_row("Outcome", code(_normalize_text(snapshot.get("outcome")) or "-"), inline=True),
        field_row(
            "Stages",
            code(_safe_int(snapshot.get("stage_count"), default=0)),
            raw(" | completed="),
            code(_result_count(snapshot, STATUS_COMPLETED)),
            raw(", failed="),
            code(_result_count(snapshot, STATUS_FAILED)),
            raw(", cancelled="),
            code(_result_count(snapshot, STATUS_CANCELLED)),
        ),
    ]
    if phase_engine == "xtb":
        ready_count = sum(
            1
            for row in snapshot.get("stage_statuses", [])
            if _normalize_text(row.get("reaction_handoff_status")).lower() == "ready"
        )
        fields.append(field_row("Ready for ORCA", code(ready_count), inline=True))
    return fields


def _summary_severity(snapshot: dict[str, Any]) -> Any:
    outcome = _normalize_text(snapshot.get("outcome")).lower()
    return {
        STATUS_COMPLETED: "success",
        STATUS_FAILED: "error",
        "mixed": "warning",
        STATUS_CANCELLED: "warning",
    }.get(outcome, "info")


def _build_phase_summary_message(
    *,
    payload: dict[str, Any],
    phase_engine: str,
    snapshot: dict[str, Any],
    extra_lines: list[str] | None,
) -> Message:
    title = f"{_phase_label(phase_engine)} phase summary"
    overview = group(
        *_overview_fields(payload=payload, phase_engine=phase_engine, snapshot=snapshot)
    )
    groups: list[Group] = [overview]

    notes = _notes_group(extra_lines)
    if notes is not None:
        groups.append(notes)

    raw_stages = _raw_stages_by_id(payload)
    stage_rows = [
        _phase_stage_row(
            row,
            raw_stages.get(_normalize_text(row.get("stage_id")), {}),
            phase_engine=phase_engine,
        )
        for row in snapshot.get("stage_statuses", [])
    ]
    for index, row in enumerate(stage_rows):
        heading = (bold("Stage details"),) if index == 0 else ()
        groups.append(group(*_stage_row_lines(row), heading=heading))

    return Message(
        title=title,
        severity=_summary_severity(snapshot),
        groups=tuple(groups),
        author="orca_auto",
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
    extra_lines: list[str] | None = None,
) -> bool:
    normalized_engine = _normalize_text(phase_engine).lower()
    if normalized_engine not in {"crest", "xtb"}:
        return False

    snapshot = phase_snapshot(payload.get("stages", []), engine=normalized_engine)
    stage_count = _safe_int(snapshot.get("stage_count"), default=0)
    if not stage_count or not snapshot.get("finished"):
        return False

    notification_state = _phase_notification_state(payload)
    state_key = f"{normalized_engine}_summary"
    if _phase_summary_already_sent(notification_state, state_key=state_key):
        return False

    message = _build_phase_summary_message(
        payload=payload,
        phase_engine=normalized_engine,
        snapshot=snapshot,
        extra_lines=extra_lines,
    )
    try:
        channel = build_channel_from_config_path(config_path)
        if not channel.enabled:
            return False
        result = channel.send(message)
    except Exception as exc:  # noqa: BLE001 - optional notification boundary
        LOGGER.warning(
            "workflow_phase_notification_failed: workflow_id=%s engine=%s error_type=%s",
            _normalize_text(payload.get("workflow_id")) or "-",
            normalized_engine,
            type(exc).__name__,
        )
        return False
    if not result.sent:
        if not result.skipped:
            LOGGER.warning(
                "workflow_phase_notification_failed: workflow_id=%s engine=%s error=%s",
                _normalize_text(payload.get("workflow_id")) or "-",
                normalized_engine,
                result.error or "unknown_error",
            )
        return False

    _mark_phase_summary_sent(
        notification_state,
        state_key=state_key,
        stage_count=stage_count,
    )
    return True


__all__ = ["maybe_notify_workflow_phase_summary"]
