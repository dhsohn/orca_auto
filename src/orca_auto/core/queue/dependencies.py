from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


class SleepTimer(Protocol):
    def sleep(self, seconds: float) -> None: ...


QueueEntryDequeuer = Callable[[Any], tuple[Path, Any] | None]
AdmissionReserver = Callable[[Any], str | None]


class SlotReleaser(Protocol):
    def __call__(self, admission_root: str | Path, admission_token: str, /) -> object: ...


class DequeuedEntryReserver(Protocol):
    def __call__(
        self,
        cfg: Any,
        *,
        admission_root: str | Path,
        reserve_slot_fn: AdmissionReserver,
        dequeue_next_fn: QueueEntryDequeuer,
        release_slot_fn: SlotReleaser,
    ) -> tuple[str, Any | None]: ...


class BackgroundJobProcessStarter(Protocol):
    def __call__(
        self,
        *,
        config_path: str,
        queue_root: Path,
        entry: Any,
        admission_root: Any,
        admission_token: str,
    ) -> Any: ...


@dataclass(frozen=True)
class ChildQueueWorkerDeps:
    poll_interval_seconds: int
    time: SleepTimer
    admission_root: Callable[[Any], str]
    start_background_job_process: BackgroundJobProcessStarter
    release_slot: SlotReleaser
    reserve_dequeued_entry: DequeuedEntryReserver
    dequeue_next_entry: QueueEntryDequeuer
    try_reserve_admission_slot: AdmissionReserver


def dependency_group(value: T | None, default_factory: Callable[[], T]) -> T:
    return value if value is not None else default_factory()


def resolve_dependency_groups(
    overrides: Mapping[str, Any],
    default_factories: Mapping[str, Callable[[], Any]],
) -> dict[str, Any]:
    return {
        name: dependency_group(overrides.get(name), default_factory)
        for name, default_factory in default_factories.items()
    }


def build_dependency_container(
    container_type: Callable[..., T],
    overrides: Mapping[str, Any],
    default_factories: Mapping[str, Callable[[], Any]],
    *,
    extra_fields: Mapping[str, Any] | None = None,
) -> T:
    resolved = resolve_dependency_groups(overrides, default_factories)
    if extra_fields:
        resolved.update(extra_fields)
    return container_type(**resolved)


__all__ = [
    "ChildQueueWorkerDeps",
    "BackgroundJobProcessStarter",
    "DequeuedEntryReserver",
    "build_dependency_container",
    "dependency_group",
    "resolve_dependency_groups",
    "QueueEntryDequeuer",
    "SleepTimer",
    "SlotReleaser",
]
