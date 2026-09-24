"""Attempt-chain rows and table rendering shared by all job reports."""

from __future__ import annotations

import html
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from ..statuses import AnalyzerStatus
from .render import metric_card

T = TypeVar("T")


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


def with_details(
    rows: Sequence[AttemptReportRow], details: Sequence[str]
) -> tuple[AttemptReportRow, ...]:
    """``rows`` with their per-attempt detail column filled from ``details``."""
    return tuple(replace(row, detail=detail) for row, detail in zip(rows, details, strict=True))


def attempt_out_path(attempt: Mapping[str, Any]) -> Path | None:
    """The attempt's recorded output path when it still exists on disk."""
    out_raw = str(attempt.get("out_path") or "").strip()
    if not out_raw:
        return None
    out_path = Path(out_raw)
    return out_path if out_path.exists() else None


def parse_attempt_output(attempt: Mapping[str, Any], parse: Callable[[Path], T]) -> T | None:
    """``parse`` of the attempt's output; ``None`` when it is absent or unreadable."""
    out_path = attempt_out_path(attempt)
    if out_path is None:
        return None
    try:
        return parse(out_path)
    except OSError:
        return None


def latest_attempt_with_content(
    attempts: Sequence[Mapping[str, Any]],
    parse: Callable[[Path], T],
    has_content: Callable[[T], bool],
) -> T | None:
    """Latest attempt output whose parse ``has_content``; else the latest readable parse.

    An execution that died before its driver started (or a trailing Freq-only
    attempt) parses to an empty shell; skipping such shells keeps the report
    consistent with the per-attempt detail column instead of masking an earlier
    attempt's data. ``None`` only when no attempt output can be read.
    """
    fallback: T | None = None
    for attempt in reversed(attempts):
        parsed = parse_attempt_output(attempt, parse)
        if parsed is None:
            continue
        if has_content(parsed):
            return parsed
        if fallback is None:
            fallback = parsed
    return fallback


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
        if row.analyzer_status == AnalyzerStatus.COMPLETED.value:
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


def attempts_metric_card(rows: Sequence[AttemptReportRow], total_duration_text: str) -> str:
    """The "Attempts" metric card every job report ends its card row with."""
    return metric_card(
        "Attempts",
        str(len(rows)),
        total_duration_text and f"total wall time {total_duration_text}",
    )
