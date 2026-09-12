from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InternalEngineSubmitterSpec:
    run_dir_api_name: str
    cancel_api_name: str
    extra_fields_fn: Callable[[Any | None, Any | None], dict[str, Any]] | None = None


@dataclass(frozen=True)
class InternalEngineSubmitterDeps:
    load_config_fn: Callable[[Any], Any]
    resolve_job_dir_fn: Callable[[Any, str], Any]
    load_manifest_fn: Callable[[Any], dict[str, Any]]
    build_submission_fn: Callable[[Any, Any, dict[str, Any], Any], Any]
    record_queued_fn: Callable[[Any, Any, Any], Any]
    enqueue_fn: Callable[..., Any]
    load_queue_config_fn: Callable[[Any], Any]
    queue_entries_with_roots_fn: Callable[[Any], list[tuple[Any, Any]]]
    request_cancel_fn: Callable[..., Any | None]
    display_status_fn: Callable[[Any], str]
    before_pending_cancel_fn: Callable[..., Any] | None = None
