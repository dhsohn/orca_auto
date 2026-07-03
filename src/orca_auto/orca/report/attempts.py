"""Attempt-chain rows and table rendering shared by all job reports."""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any


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
    if any(action.startswith("scants_fallback_to_optts") for action in creating_actions):
        return "OptTS fallback (scan maximum)", "forward"
    if any(action.startswith("relaxed_scan_optts_chain") for action in creating_actions):
        return "OptTS chain (scan maximum)", "forward"
    if any(action.startswith("scants_reverse_scan") for action in creating_actions):
        return "reverse ScanTS", "reverse"
    if any(action.startswith("scants_endpoint_scan") for action in creating_actions):
        return "endpoint completion (Opt scan)", "forward"
    if any(
        action.startswith(("scants_scan_range_", "scants_scan_endpoint_extended"))
        for action in creating_actions
    ):
        return "scan continuation", "forward"
    if any(action.startswith("resume_") for action in creating_actions):
        return "resume", "forward"
    return "retry", "forward"


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
