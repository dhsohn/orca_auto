from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .artifacts import (
    ENGINE_ARTIFACT_SCHEMA_VERSION,
    EngineArtifactInput,
    EngineArtifactJob,
    EngineArtifactProcess,
    EngineArtifactRecovery,
    EngineArtifactResources,
    EngineArtifactSchema,
    EngineArtifactStatus,
    EngineArtifactTimestamps,
    build_engine_artifact_payload,
    load_engine_artifact_payload,
)
from .definition_builder import (
    build_engine_runtime_roots,
    build_lazy_engine_callback,
    build_lazy_queue_worker_runner,
    build_lazy_worker_child_runner,
    build_queue_engine_definition,
    build_queue_entry_by_id,
)
from .definitions import (
    EngineArtifactAdapter,
    EngineCancellationHooks,
    EngineContextBuilder,
    EngineDefinition,
    EngineNotificationHooks,
    EngineQueueFunctions,
    EngineRunnerCallbacks,
)
from .identity import entry_matches_engine_identity, own_engine_accept_entry
from .registry import get_engine_definition, known_engine_ids

if TYPE_CHECKING:
    from .queue_worker import EngineQueueWorker
    from .worker_child import (
        EngineWorkerChild,
        build_worker_child_command,
        build_worker_child_command_for_engine,
        run_engine_worker_child_job,
    )

_LAZY_WORKER_CHILD_EXPORTS = frozenset(
    {
        "EngineWorkerChild",
        "build_worker_child_command",
        "build_worker_child_command_for_engine",
        "run_engine_worker_child_job",
    }
)


def __getattr__(name: str) -> Any:
    if name == "EngineQueueWorker":
        value = getattr(import_module(f"{__name__}.queue_worker"), name)
        globals()[name] = value
        return value
    if name in _LAZY_WORKER_CHILD_EXPORTS:
        value = getattr(import_module(f"{__name__}.worker_child"), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ENGINE_ARTIFACT_SCHEMA_VERSION",
    "EngineArtifactSchema",
    "EngineArtifactAdapter",
    "EngineCancellationHooks",
    "EngineContextBuilder",
    "EngineArtifactInput",
    "EngineArtifactJob",
    "EngineArtifactProcess",
    "EngineArtifactRecovery",
    "EngineArtifactResources",
    "EngineArtifactStatus",
    "EngineArtifactTimestamps",
    "EngineDefinition",
    "EngineNotificationHooks",
    "EngineQueueFunctions",
    "EngineQueueWorker",
    "EngineRunnerCallbacks",
    "EngineWorkerChild",
    "build_engine_artifact_payload",
    "build_engine_runtime_roots",
    "build_lazy_engine_callback",
    "build_lazy_queue_worker_runner",
    "build_lazy_worker_child_runner",
    "build_queue_engine_definition",
    "build_queue_entry_by_id",
    "build_worker_child_command",
    "build_worker_child_command_for_engine",
    "entry_matches_engine_identity",
    "get_engine_definition",
    "known_engine_ids",
    "load_engine_artifact_payload",
    "own_engine_accept_entry",
    "run_engine_worker_child_job",
]
