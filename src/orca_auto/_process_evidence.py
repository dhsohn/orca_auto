"""Process-local provenance used by the systemd freshness check."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROCESS_IMPORT_SOURCE_ENV = "ORCA_AUTO_PROCESS_IMPORT_SOURCE"


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
    if os.environ.get(PROCESS_IMPORT_SOURCE_ENV) == import_source:
        return
    environment = os.environ.copy()
    environment[PROCESS_IMPORT_SOURCE_ENV] = import_source
    os.execve(
        sys.executable,
        [sys.executable, "-m", "orca_auto.cli", *sys.argv[1:]],
        environment,
    )


__all__ = ["PROCESS_IMPORT_SOURCE_ENV", "exec_with_import_source_evidence"]
