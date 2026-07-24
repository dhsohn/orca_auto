from __future__ import annotations

from pathlib import Path
from typing import Any

from . import _artifacts as _artifacts
from . import _contract_context as _contract_context
from . import _runtime_context as _runtime_context
from ._models import (
    JobArtifactContext,
    JobRuntimeContext,
)


def resolve_latest_job_dir(index_root: str | Path, target: str) -> Path | None:
    return _artifacts.resolve_latest_job_dir(index_root, target)


def load_job_artifact_context(
    index_root: str | Path,
    target: str,
) -> JobArtifactContext:
    return _artifacts.load_job_artifact_context(index_root, target)


def load_job_runtime_context(
    index_root: str | Path,
    target: str,
    *,
    queue_id: str = "",
    run_id: str = "",
    reaction_dir: str = "",
) -> JobRuntimeContext:
    return _runtime_context.load_job_runtime_context(
        index_root,
        target,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
    )


def load_orca_contract_payload(
    index_root: str | Path,
    target: str,
    *,
    queue_id: str = "",
    run_id: str = "",
    reaction_dir: str = "",
) -> dict[str, Any]:
    runtime = load_job_runtime_context(
        index_root,
        target,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
    )
    ctx = _contract_context.payload_context_from_runtime(
        runtime=runtime,
        target=target,
        run_id=run_id,
        reaction_dir=reaction_dir,
    )
    return _contract_context.payload_from_context(ctx, queue_id=queue_id)


def load_job_artifacts(
    index_root: str | Path,
    target: str,
) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
    return _artifacts.load_job_artifacts(index_root, target)
