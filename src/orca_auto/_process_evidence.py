"""Process-local provenance used by the systemd freshness check."""

from __future__ import annotations

import os
import sys
from importlib import import_module
from pathlib import Path

PROCESS_IMPORT_SOURCE_ENV = "ORCA_AUTO_PROCESS_IMPORT_SOURCE"
PROCESS_WORKFLOW_IMPORT_SOURCE_ENV = "ORCA_AUTO_PROCESS_WORKFLOW_IMPORT_SOURCE"


def _workflow_worker_requested(argv: list[str]) -> bool:
    return "--app=workflow" in argv or any(
        flag == "--app" and value == "workflow" for flag, value in zip(argv, argv[1:], strict=False)
    )


def exec_with_import_source_evidence() -> None:
    """Re-exec the module CLI once with its actual imported source in environ.

    ``/proc/<pid>/environ`` exposes only the environment installed by ``exec``;
    mutating ``os.environ`` after Python starts is not reliable process evidence.
    Re-exec preserves the systemd MainPID and kernel process start identity while
    making the source path observable without a sidecar state file.
    """

    if sys.argv[1:3] != ["queue", "worker"]:
        return
    import_source = str(Path(__file__).resolve(strict=False))
    environment = os.environ.copy()
    environment[PROCESS_IMPORT_SOURCE_ENV] = import_source
    if _workflow_worker_requested(sys.argv[3:]):
        from orca_auto.core.extensions import require_workflows

        require_workflows()
        workflow_module = import_module("orca_auto.flow")
        workflow_source = workflow_module.__file__
        if workflow_source is None:
            raise ValueError("workflow import source is unavailable")
        environment[PROCESS_WORKFLOW_IMPORT_SOURCE_ENV] = str(
            Path(workflow_source).resolve(strict=True)
        )
    else:
        environment.pop(PROCESS_WORKFLOW_IMPORT_SOURCE_ENV, None)
    if environment == os.environ:
        return
    os.execve(
        sys.executable,
        [sys.executable, "-m", "orca_auto.cli", *sys.argv[1:]],
        environment,
    )


__all__ = [
    "PROCESS_IMPORT_SOURCE_ENV",
    "PROCESS_WORKFLOW_IMPORT_SOURCE_ENV",
    "exec_with_import_source_evidence",
]
