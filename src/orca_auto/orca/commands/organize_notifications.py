from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..config import AppConfig
from ..result_organizer_models import SkipReason
from ..telegram_notifier import escape_html, send_message

logger = logging.getLogger(__name__)

_ORGANIZE_RESULT_LIMIT = 10
_ORGANIZE_FAILURE_LIMIT = 5
_ORGANIZE_SKIP_LIMIT = 5


def _organize_summary_parts(
    organized_count: int, skipped_count: int, failed_count: int
) -> list[str]:
    summary_parts: list[str] = []
    if organized_count > 0:
        summary_parts.append(f"\u2705 Organized: {organized_count}")
    if skipped_count > 0:
        summary_parts.append(f"\u23ed Skipped: {skipped_count}")
    if failed_count > 0:
        summary_parts.append(f"\u274c Failed: {failed_count}")
    return summary_parts


def _format_organized_line(
    item: dict[str, Any],
    *,
    escape_html_fn: Callable[[Any], str] = escape_html,
) -> str:
    plan = item.get("_plan")
    if plan is None:
        return f"\u2705 <b>{escape_html_fn(item.get('run_id', '?'))}</b>"

    job_label = plan.job_type.upper() if plan.job_type else "-"
    mol_label = plan.molecule_key or "-"
    return (
        f"\u2705 <b>{escape_html_fn(plan.run_id[:12])}</b>\n"
        f"   \U0001f4c2 {escape_html_fn(str(plan.source_dir.name))} \u2192 "
        f"{escape_html_fn(plan.target_rel_path)}\n"
        f"   \U0001f3f7 {escape_html_fn(job_label)} | {escape_html_fn(mol_label)}"
    )


def _organized_section(
    organized: list[dict[str, Any]],
    *,
    escape_html_fn: Callable[[Any], str] = escape_html,
) -> str | None:
    organized_count = len(organized)
    if organized_count == 0:
        return None

    lines = [
        _format_organized_line(item, escape_html_fn=escape_html_fn)
        for item in organized[:_ORGANIZE_RESULT_LIMIT]
    ]
    detail_header = f"\u2705 <b>Organized</b>  ({organized_count})"
    if organized_count > _ORGANIZE_RESULT_LIMIT:
        detail_header += f"  showing {_ORGANIZE_RESULT_LIMIT}/{organized_count}"
    return detail_header + "\n\n" + "\n\n".join(lines)


def _failure_section(
    failures: list[dict[str, Any]],
    *,
    escape_html_fn: Callable[[Any], str] = escape_html,
) -> str | None:
    failed_count = len(failures)
    if failed_count == 0:
        return None

    lines = [
        f"\u274c <b>{escape_html_fn(item.get('run_id', '?'))}</b>\n"
        f"   \U0001f4ac {escape_html_fn(item.get('reason', 'unknown'))}"
        for item in failures[:_ORGANIZE_FAILURE_LIMIT]
    ]
    return f"\u274c <b>Failed</b>  ({failed_count})\n\n" + "\n\n".join(lines)


def _skip_section(
    skips: list[SkipReason],
    skipped_count: int,
    *,
    escape_html_fn: Callable[[Any], str] = escape_html,
) -> str | None:
    if not skips:
        return None

    skip_lines = [
        f"\u23ed {escape_html_fn(skip.reaction_dir)}\n   \U0001f4ac {escape_html_fn(skip.reason)}"
        for skip in skips[:_ORGANIZE_SKIP_LIMIT]
    ]
    skip_header = f"\u23ed <b>Skipped</b>  ({skipped_count})"
    if skipped_count > _ORGANIZE_SKIP_LIMIT:
        skip_header += f"  showing {_ORGANIZE_SKIP_LIMIT}/{skipped_count}"
    return skip_header + "\n\n" + "\n\n".join(skip_lines)


def _build_organize_message(
    organized: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    skips: list[SkipReason],
    *,
    escape_html_fn: Callable[[Any], str] = escape_html,
) -> str | None:
    """Compose a Telegram HTML message for organize results.

    Returns None if there is nothing to report.
    """
    organized_count = len(organized)
    skipped_count = len(skipped) + len(skips)
    failed_count = len(failures)

    if organized_count == 0 and skipped_count == 0 and failed_count == 0:
        return None

    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    header = f"\U0001f4c1 <b>orca_auto organize</b>  <code>{escape_html_fn(now)}</code>"
    divider = "\u2500" * 28

    sections: list[str] = [header, divider]

    summary_parts = _organize_summary_parts(organized_count, skipped_count, failed_count)
    sections.append(f"\U0001f4ca <b>Summary</b>\n{' | '.join(summary_parts)}")

    for detail_section in (
        _organized_section(organized, escape_html_fn=escape_html_fn),
        _failure_section(failures, escape_html_fn=escape_html_fn),
        _skip_section(skips, skipped_count, escape_html_fn=escape_html_fn),
    ):
        if detail_section is not None:
            sections.append(detail_section)

    sections.append(divider)

    return "\n\n".join(sections)


def _send_organize_notification(
    cfg: AppConfig,
    *,
    organized: list[dict[str, Any]],
    skipped_results: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    skips: list[SkipReason],
    build_message_fn: Callable[
        [list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[SkipReason]],
        str | None,
    ] = _build_organize_message,
    send_message_fn: Callable[[Any, str], bool] = send_message,
    log: logging.Logger = logger,
) -> None:
    if not cfg.telegram.enabled:
        return

    message = build_message_fn(organized, skipped_results, failures, skips)
    if message is None:
        return
    if send_message_fn(cfg.telegram, message):
        log.info("Telegram organize notification sent successfully")
    else:
        log.warning("Failed to send Telegram organize notification")
