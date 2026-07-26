"""Plain-text engine job notifications for the internal xTB/CREST engines.

A notification is a list of plain-text lines — ``[label] headline`` followed by
``key: value`` rows — wrapped as a Doc-model message and sent one-way through
the configured messenger. Jobs running inside a workflow workspace are
suppressed (the workflow owns their reporting); suppressed sends still report
success so callers never retry them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.messaging import (
    Message,
    Severity,
    build_channel,
    group,
    line,
    raw,
)

EngineLineSender = Callable[[Any, list[str], Severity], bool]

_TERMINAL_PRESENTATION: dict[str, tuple[str, Severity]] = {
    "completed": ("Job finished", "success"),
    "failed": ("Job failed", "error"),
    "cancelled": ("Job cancelled", "warning"),
}


def terminal_headline(status: str) -> str:
    presentation = _TERMINAL_PRESENTATION.get(status)
    return presentation[0] if presentation is not None else "Job status unknown"


def terminal_severity(status: str) -> Severity:
    """Map a structured terminal status to presentation severity, failing closed."""
    presentation = _TERMINAL_PRESENTATION.get(status)
    return presentation[1] if presentation is not None else "info"


def _lines_message(lines: list[str], severity: Severity = "info") -> Message:
    """Wrap pre-formatted plain-text lines as a Doc-model message.

    Engine job notifications are plain text (no markup), so each line becomes a
    ``raw`` span: the Discord renderer collects the lines into the embed
    description. ``severity`` only reaches the Discord embed colour/glyph, so a
    failed job no longer renders in the neutral ``info`` blue.
    """
    title = lines[0] if lines else ""
    return Message(
        title=title,
        severity=severity,
        groups=(group(*(line(raw(text)) for text in lines[1:])),),
        author="orca_auto",
    )


def send_lines(cfg: Any, lines: list[str], severity: Severity = "info") -> bool:
    if not lines:
        return False
    # ``build_channel`` is referenced as a module global (not a default arg) so
    # tests can monkeypatch it.
    channel = build_channel(cfg.messenger)
    result = channel.send(_lines_message(lines, severity))
    return bool(result.sent or result.skipped)


def _is_workflow_child(job_dir: Path, *, engine: str) -> bool:
    parts = tuple(part for part in job_dir.parts if part)
    return any(part.endswith(f"_{engine}") for part in parts)


def _required_value(values: Mapping[str, object], field_name: str) -> object:
    if field_name not in values:
        raise KeyError(field_name)
    return values[field_name]


def _required_str(values: Mapping[str, object], field_name: str) -> str:
    value = _required_value(values, field_name)
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be str")
    return value


def _required_path(values: Mapping[str, object], field_name: str) -> Path:
    value = _required_value(values, field_name)
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be pathlib.Path")
    return value


def _required_int(values: Mapping[str, object], field_name: str) -> int:
    value = _required_value(values, field_name)
    if not isinstance(value, int):
        raise TypeError(f"{field_name} must be int")
    return value


def _optional_int_dict(
    values: Mapping[str, object],
    field_name: str,
) -> dict[str, int] | None:
    value = values.get(field_name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be dict[str, int] or None")
    if not all(isinstance(key, str) and isinstance(item, int) for key, item in value.items()):
        raise TypeError(f"{field_name} must be dict[str, int] or None")
    return dict(value)


@dataclass(frozen=True)
class EngineJobNotifications:
    """Renders and sends the three job events (queued, started, finished)."""

    label: str
    engine: str
    selected_field_name: str
    detail_field_names: tuple[str, ...]
    terminal_count_field: str
    send_fn: EngineLineSender

    def _detail_fields(self, values: Mapping[str, object]) -> list[tuple[str, object]]:
        return [
            (field_name, values[field_name])
            for field_name in self.detail_field_names
            if field_name in values
        ]

    def _send(
        self,
        cfg: Any,
        *,
        job_dir: Path,
        headline: str,
        severity: Severity,
        fields: list[tuple[str, object]],
        extra_lines: list[str] | None = None,
    ) -> bool:
        if _is_workflow_child(job_dir, engine=self.engine):
            return True
        lines = [f"[{self.label}] {headline}"]
        lines.extend(f"{key}: {value}" for key, value in fields)
        if extra_lines:
            lines.extend(extra_lines)
        return self.send_fn(cfg, lines, severity)

    def _notify_lifecycle(self, cfg: Any, values: Mapping[str, object], headline: str) -> bool:
        job_dir = _required_path(values, "job_dir")
        fields: list[tuple[str, object]] = [
            ("job_id", _required_str(values, "job_id")),
            ("queue_id", _required_str(values, "queue_id")),
            *self._detail_fields(values),
            ("job_dir", job_dir.name),
            (self.selected_field_name, _required_path(values, "selected_xyz").name),
        ]
        return self._send(cfg, job_dir=job_dir, headline=headline, severity="info", fields=fields)

    def notify_job_queued(self, cfg: Any, **values: object) -> bool:
        return self._notify_lifecycle(cfg, values, "Job queued")

    def notify_job_started(self, cfg: Any, **values: object) -> bool:
        return self._notify_lifecycle(cfg, values, "Job started")

    def notify_job_finished(self, cfg: Any, **values: object) -> bool:
        status = _required_str(values, "status")
        job_dir = _required_path(values, "job_dir")
        fields: list[tuple[str, object]] = [
            ("job_id", _required_str(values, "job_id")),
            ("queue_id", _required_str(values, "queue_id")),
            ("status", status),
            ("reason", _required_str(values, "reason")),
            *self._detail_fields(values),
            ("job_dir", job_dir.name),
            (self.selected_field_name, _required_path(values, "selected_xyz").name),
            (self.terminal_count_field, _required_int(values, self.terminal_count_field)),
        ]
        extra_lines: list[str] = []
        resource_request = _optional_int_dict(values, "resource_request")
        if resource_request is not None:
            extra_lines.append(f"resource_request: {resource_request}")
        resource_actual = _optional_int_dict(values, "resource_actual")
        if resource_actual is not None:
            extra_lines.append(f"resource_actual: {resource_actual}")
        return self._send(
            cfg,
            job_dir=job_dir,
            headline=terminal_headline(status),
            severity=terminal_severity(status),
            fields=fields,
            extra_lines=extra_lines or None,
        )


_XTB_JOB_NOTIFICATIONS = EngineJobNotifications(
    label="xTB",
    engine="xtb",
    selected_field_name="selected_input_xyz",
    detail_field_names=("job_type", "reaction_key"),
    terminal_count_field="candidate_count",
    send_fn=send_lines,
)

notify_xtb_job_queued = _XTB_JOB_NOTIFICATIONS.notify_job_queued
notify_xtb_job_started = _XTB_JOB_NOTIFICATIONS.notify_job_started
notify_xtb_job_finished = _XTB_JOB_NOTIFICATIONS.notify_job_finished

_CREST_JOB_NOTIFICATIONS = EngineJobNotifications(
    label="CREST",
    engine="crest",
    selected_field_name="selected_xyz",
    detail_field_names=("mode",),
    terminal_count_field="retained_conformer_count",
    send_fn=send_lines,
)

notify_crest_job_queued = _CREST_JOB_NOTIFICATIONS.notify_job_queued
notify_crest_job_started = _CREST_JOB_NOTIFICATIONS.notify_job_started
notify_crest_job_finished = _CREST_JOB_NOTIFICATIONS.notify_job_finished


__all__ = [
    "EngineJobNotifications",
    "notify_crest_job_finished",
    "notify_crest_job_queued",
    "notify_crest_job_started",
    "notify_xtb_job_finished",
    "notify_xtb_job_queued",
    "notify_xtb_job_started",
    "send_lines",
    "terminal_headline",
    "terminal_severity",
]
