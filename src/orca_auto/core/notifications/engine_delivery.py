from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._engine_rendering import (
    EngineEventField,
    job_event_fields,
    optional_terminal_lines,
    terminal_headline,
)
from .engine_notifier import EngineNotifier
from .engine_requests import (
    EngineJobFinishedRequest,
    EngineJobLifecycleRequest,
    EngineJobTerminalRequest,
)


@dataclass(frozen=True)
class EngineNotificationDelivery:
    notifier: EngineNotifier
    selected_field_name: str
    detail_field_names: tuple[str, ...]
    terminal_count_field: str

    def detail_fields(self, values: Mapping[str, object]) -> list[EngineEventField]:
        return [
            (field_name, values[field_name])
            for field_name in self.detail_field_names
            if field_name in values
        ]

    def deliver_lifecycle(self, cfg: Any, request: EngineJobLifecycleRequest) -> bool:
        return send_lifecycle_event(
            self.notifier,
            cfg,
            headline=request.headline,
            job_id=request.job_id,
            queue_id=request.queue_id,
            job_dir=request.job_dir,
            selected_xyz=request.selected_xyz,
            selected_field_name=self.selected_field_name,
            detail_fields=self.detail_fields(request.detail_values),
        )

    def deliver_terminal(self, cfg: Any, request: EngineJobTerminalRequest) -> bool:
        return send_terminal_event(
            self.notifier,
            cfg,
            headline=request.headline,
            job_id=request.job_id,
            queue_id=request.queue_id,
            status=request.status,
            reason=request.reason,
            job_dir=request.job_dir,
            selected_xyz=request.selected_xyz,
            selected_field_name=self.selected_field_name,
            detail_fields=self.detail_fields(request.detail_values),
            count_field=(self.terminal_count_field, request.count_value),
            extra_lines=request.extra_lines,
        )

    def deliver_finished(self, cfg: Any, request: EngineJobFinishedRequest) -> bool:
        extra_lines = optional_terminal_lines(
            resource_request=request.resource_request,
            resource_actual=request.resource_actual,
        )
        return self.deliver_terminal(
            cfg,
            EngineJobTerminalRequest(
                headline=terminal_headline(request.status),
                job_id=request.job_id,
                queue_id=request.queue_id,
                status=request.status,
                reason=request.reason,
                job_dir=request.job_dir,
                selected_xyz=request.selected_xyz,
                count_value=request.count_value,
                detail_values=request.detail_values,
                extra_lines=extra_lines or None,
            ),
        )


def send_lifecycle_event(
    notifier: EngineNotifier,
    cfg: Any,
    *,
    headline: str,
    job_id: str,
    queue_id: str,
    job_dir: Path,
    selected_xyz: Path,
    selected_field_name: str,
    detail_fields: list[tuple[str, object]] | None = None,
) -> bool:
    return notifier.send_job_event(
        cfg,
        job_dir=job_dir,
        headline=headline,
        fields=job_event_fields(
            job_id=job_id,
            queue_id=queue_id,
            job_dir=job_dir,
            selected_xyz=selected_xyz,
            selected_field_name=selected_field_name,
            detail_fields=detail_fields,
        ),
    )


def send_terminal_event(
    notifier: EngineNotifier,
    cfg: Any,
    *,
    headline: str,
    job_id: str,
    queue_id: str,
    status: str,
    reason: str,
    job_dir: Path,
    selected_xyz: Path,
    selected_field_name: str,
    count_field: tuple[str, object],
    detail_fields: list[tuple[str, object]] | None = None,
    extra_lines: list[str] | None = None,
) -> bool:
    return notifier.send_job_event(
        cfg,
        job_dir=job_dir,
        headline=headline,
        fields=job_event_fields(
            job_id=job_id,
            queue_id=queue_id,
            status=status,
            reason=reason,
            job_dir=job_dir,
            selected_xyz=selected_xyz,
            selected_field_name=selected_field_name,
            detail_fields=detail_fields,
            count_field=count_field,
        ),
        extra_lines=extra_lines,
    )


__all__ = [
    "EngineNotificationDelivery",
    "send_lifecycle_event",
    "send_terminal_event",
]
