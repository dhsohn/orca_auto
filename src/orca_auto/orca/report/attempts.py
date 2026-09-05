"""Attempt-chain rows and table rendering shared by all job reports."""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..statuses import AnalyzerStatus


@dataclass(frozen=True)
class AttemptReportRow:
    index: int
    label: str
    direction: str
    analyzer_status: str
    analyzer_reason: str
    duration_text: str
    detail: str
    terminal_actions: tuple[str, ...]


def analyzer_status_text(value: Any) -> str:
    """Canonical analyzer status text for both durable strings and live enums."""
    return value.value if isinstance(value, AnalyzerStatus) else str(value or "")


def attempt_dicts(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        return []
    return [attempt for attempt in attempts if isinstance(attempt, dict)]


def attempt_actions(attempt: Mapping[str, Any]) -> tuple[str, ...]:
    actions = attempt.get("patch_actions")
    if not isinstance(actions, list):
        return ()
    return tuple(str(action) for action in actions)


def attempt_role(creating_actions: Sequence[str]) -> tuple[str, str]:
    """(label, direction) for the attempt created by ``creating_actions``."""
    if any(action.startswith("resume_") for action in creating_actions):
        return "resume", "forward"
    return "attempt", "forward"


def attempt_report_rows(
    attempts: Sequence[Mapping[str, Any]], initial_label: str
) -> tuple[AttemptReportRow, ...]:
    """Rows for job types without a per-attempt detail column (Opt, SP)."""
    rows: list[AttemptReportRow] = []
    for position, attempt in enumerate(attempts):
        if position == 0:
            label = initial_label
        else:
            label, _direction = attempt_role(attempt_actions(attempts[position - 1]))
        rows.append(
            AttemptReportRow(
                index=int(attempt.get("index", position + 1) or (position + 1)),
                label=label,
                direction="forward",
                analyzer_status=analyzer_status_text(attempt.get("analyzer_status")),
                analyzer_reason=str(attempt.get("analyzer_reason") or ""),
                duration_text=duration_text(attempt.get("started_at"), attempt.get("ended_at")),
                detail="",
                terminal_actions=attempt_actions(attempt) if position == len(attempts) - 1 else (),
            )
        )
    return tuple(rows)


def final_out_path(state: Mapping[str, Any]) -> Path | None:
    """Output file of the run's recorded final result.

    A recorded final ``last_out_path`` is authoritative: when it is absent on
    disk the run has no trustworthy output, and an earlier attempt's numbers
    must never stand in for it. The attempt scan remains only for records
    that never captured a final result path.
    """
    final_result = state.get("final_result")
    if isinstance(final_result, Mapping):
        last_out = str(final_result.get("last_out_path") or "").strip()
        if last_out:
            path = Path(last_out)
            return path if path.exists() else None
    attempts = state.get("attempts")
    attempts = attempts if isinstance(attempts, list) else []
    for attempt in reversed(attempts):
        if not isinstance(attempt, Mapping):
            continue
        out_raw = str(attempt.get("out_path") or "").strip()
        if out_raw and Path(out_raw).exists():
            return Path(out_raw)
    return None


def parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def duration_text(start: Any, end: Any) -> str:
    started = parse_iso(start)
    ended = parse_iso(end)
    if started is None or ended is None:
        return ""
    seconds = (ended - started).total_seconds()
    if seconds < 0:
        return ""
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def attempts_table_html(rows: Sequence[AttemptReportRow], detail_header: str) -> str:
    body = []
    for row in rows:
        if row.analyzer_status == "completed":
            status_class = "ok"
        elif row.analyzer_status == "ts_not_found":
            status_class = "warn"
        else:
            status_class = "bad"
        duration = html.escape(row.duration_text) if row.duration_text else "&#8211;"
        detail = html.escape(row.detail) if row.detail else "&#8211;"
        body.append(
            "<tr>"
            f"<td>{row.index}</td>"
            f"<td>{html.escape(row.label)}</td>"
            f"<td>{detail}</td>"
            f'<td class="{status_class}">{html.escape(row.analyzer_status)}'
            f'<div class="sub">{html.escape(row.analyzer_reason)}</div></td>'
            f"<td>{duration}</td>"
            "</tr>"
        )
    return (
        f"<table><thead><tr><th>#</th><th>Recipe</th><th>{html.escape(detail_header)}</th>"
        "<th>Analyzer</th><th>Wall time</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def terminal_actions_html(rows: Sequence[AttemptReportRow]) -> str:
    terminal = rows[-1].terminal_actions if rows else ()
    if not terminal:
        return ""
    return (
        f'<p class="muted">Final rewrite actions: <code>{html.escape(", ".join(terminal))}'
        "</code></p>"
    )
