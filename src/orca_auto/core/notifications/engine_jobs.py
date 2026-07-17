from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from ._engine_delivery import EngineLineSender
from ._engine_rendering import EngineEventField
from .engine_delivery import EngineNotificationDelivery
from .engine_notifier import EngineNotifier
from .engine_requests import (
    EngineJobFinishedRequest,
    EngineJobLifecycleRequest,
    EngineJobTerminalRequest,
)
from .engine_validation import (
    _optional_int_dict,
    _optional_lines,
    _required_int,
    _required_path,
    _required_str,
)


@dataclass(frozen=True)
class EngineNotificationModule:
    notifier: EngineNotifier
    selected_field_name: str
    detail_field_names: tuple[str, ...]
    terminal_count_field: str

    @cached_property
    def delivery(self) -> EngineNotificationDelivery:
        return EngineNotificationDelivery(
            notifier=self.notifier,
            selected_field_name=self.selected_field_name,
            detail_field_names=self.detail_field_names,
            terminal_count_field=self.terminal_count_field,
        )

    def detail_fields(self, values: Mapping[str, object]) -> list[EngineEventField]:
        return self.delivery.detail_fields(values)

    def notify_lifecycle_request(
        self,
        cfg: Any,
        request: EngineJobLifecycleRequest,
    ) -> bool:
        return self.delivery.deliver_lifecycle(cfg, request)

    def notify_terminal_request(
        self,
        cfg: Any,
        request: EngineJobTerminalRequest,
    ) -> bool:
        return self.delivery.deliver_terminal(cfg, request)

    def notify_finished_request(
        self,
        cfg: Any,
        request: EngineJobFinishedRequest,
    ) -> bool:
        return self.delivery.deliver_finished(cfg, request)

    def notify_finished(
        self,
        cfg: Any,
        *,
        job_id: str,
        queue_id: str,
        status: str,
        reason: str,
        job_dir: Path,
        selected_xyz: Path,
        count_value: int,
        detail_values: Mapping[str, object],
        resource_request: dict[str, int] | None = None,
        resource_actual: dict[str, int] | None = None,
    ) -> bool:
        return self.notify_finished_request(
            cfg,
            EngineJobFinishedRequest(
                job_id=job_id,
                queue_id=queue_id,
                status=status,
                reason=reason,
                job_dir=job_dir,
                selected_xyz=selected_xyz,
                count_value=count_value,
                detail_values=detail_values,
                resource_request=resource_request,
                resource_actual=resource_actual,
            ),
        )


def build_engine_notification_module(
    *,
    label: str,
    engine: str,
    selected_field_name: str,
    detail_field_names: tuple[str, ...],
    terminal_count_field: str,
    send_fn: EngineLineSender,
) -> EngineNotificationModule:
    return EngineNotificationModule(
        notifier=EngineNotifier(label=label, engine=engine, send_fn=send_fn),
        selected_field_name=selected_field_name,
        detail_field_names=detail_field_names,
        terminal_count_field=terminal_count_field,
    )


@dataclass(frozen=True)
class EngineNotificationRequestFactory:
    detail_field_names: tuple[str, ...]
    terminal_count_field: str
    terminal_count_param_name: str | None = None

    @property
    def terminal_count_param(self) -> str:
        return self.terminal_count_param_name or self.terminal_count_field

    def detail_values(self, values: Mapping[str, object]) -> dict[str, object]:
        return {
            field_name: values[field_name]
            for field_name in self.detail_field_names
            if field_name in values
        }

    def lifecycle_request(
        self,
        values: Mapping[str, object],
        headline: str,
    ) -> EngineJobLifecycleRequest:
        return EngineJobLifecycleRequest(
            headline=headline,
            job_id=_required_str(values, "job_id"),
            queue_id=_required_str(values, "queue_id"),
            job_dir=_required_path(values, "job_dir"),
            selected_xyz=_required_path(values, "selected_xyz"),
            detail_values=self.detail_values(values),
        )

    def terminal_request(self, values: Mapping[str, object]) -> EngineJobTerminalRequest:
        return EngineJobTerminalRequest(
            headline=_required_str(values, "headline"),
            job_id=_required_str(values, "job_id"),
            queue_id=_required_str(values, "queue_id"),
            status=_required_str(values, "status"),
            reason=_required_str(values, "reason"),
            job_dir=_required_path(values, "job_dir"),
            selected_xyz=_required_path(values, "selected_xyz"),
            count_value=_required_int(values, self.terminal_count_param),
            detail_values=self.detail_values(values),
            extra_lines=_optional_lines(values, "extra_lines"),
        )

    def finished_request(self, values: Mapping[str, object]) -> EngineJobFinishedRequest:
        return EngineJobFinishedRequest(
            job_id=_required_str(values, "job_id"),
            queue_id=_required_str(values, "queue_id"),
            status=_required_str(values, "status"),
            reason=_required_str(values, "reason"),
            job_dir=_required_path(values, "job_dir"),
            selected_xyz=_required_path(values, "selected_xyz"),
            count_value=_required_int(values, self.terminal_count_param),
            detail_values=self.detail_values(values),
            resource_request=_optional_int_dict(values, "resource_request"),
            resource_actual=_optional_int_dict(values, "resource_actual"),
        )


@dataclass(frozen=True)
class EngineJobNotifications:
    notifications: EngineNotificationModule
    terminal_count_param_name: str | None = None

    @cached_property
    def request_factory(self) -> EngineNotificationRequestFactory:
        return EngineNotificationRequestFactory(
            detail_field_names=self.notifications.detail_field_names,
            terminal_count_field=self.notifications.terminal_count_field,
            terminal_count_param_name=self.terminal_count_param_name,
        )

    def _lifecycle_request(
        self,
        values: Mapping[str, object],
        headline: str,
    ) -> EngineJobLifecycleRequest:
        return self.request_factory.lifecycle_request(values, headline)

    def _terminal_request(
        self,
        values: Mapping[str, object],
    ) -> EngineJobTerminalRequest:
        return self.request_factory.terminal_request(values)

    def _finished_request(
        self,
        values: Mapping[str, object],
    ) -> EngineJobFinishedRequest:
        return self.request_factory.finished_request(values)

    def notify_job_queued(self, cfg: Any, **values: object) -> bool:
        return self.notifications.notify_lifecycle_request(
            cfg,
            self._lifecycle_request(values, "Job queued"),
        )

    def notify_job_started(self, cfg: Any, **values: object) -> bool:
        return self.notifications.notify_lifecycle_request(
            cfg,
            self._lifecycle_request(values, "Job started"),
        )

    def notify_job_terminal(self, cfg: Any, **values: object) -> bool:
        return self.notifications.notify_terminal_request(cfg, self._terminal_request(values))

    def notify_job_finished(self, cfg: Any, **values: object) -> bool:
        return self.notifications.notify_finished_request(cfg, self._finished_request(values))


def build_engine_job_notifications(
    *,
    label: str,
    engine: str,
    selected_field_name: str,
    detail_field_names: tuple[str, ...],
    terminal_count_field: str,
    send_fn: EngineLineSender,
    terminal_count_param_name: str | None = None,
) -> EngineJobNotifications:
    return EngineJobNotifications(
        notifications=build_engine_notification_module(
            label=label,
            engine=engine,
            selected_field_name=selected_field_name,
            detail_field_names=detail_field_names,
            terminal_count_field=terminal_count_field,
            send_fn=send_fn,
        ),
        terminal_count_param_name=terminal_count_param_name,
    )


__all__ = [
    "EngineJobNotifications",
    "EngineNotificationModule",
    "EngineNotificationRequestFactory",
    "build_engine_job_notifications",
    "build_engine_notification_module",
]
