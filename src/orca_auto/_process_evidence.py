"""Process-local provenance used by the systemd freshness check."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from orca_auto.core.runtime_bundle import (
    PROCESS_RUNTIME_BUILD_ENV,
    runtime_root_for_import_source,
    verify_runtime_bundle,
)

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
    environment = os.environ.copy()
    environment[PROCESS_IMPORT_SOURCE_ENV] = import_source
    runtime_root = runtime_root_for_import_source(Path(import_source))
    if runtime_root is not None:
        build_id = verify_runtime_bundle(runtime_root)["build_id"]
        if environment.get(PROCESS_RUNTIME_BUILD_ENV, build_id) != build_id:
            raise ValueError("worker runtime build does not match the installed unit")
        environment[PROCESS_RUNTIME_BUILD_ENV] = build_id
    elif environment.get(PROCESS_RUNTIME_BUILD_ENV):
        raise ValueError("managed worker imported code outside its prepared runtime")
    if environment == os.environ:
        return
    os.execve(
        sys.executable,
        [
            sys.executable,
            *(["-I"] if sys.flags.isolated else []),
            "-m",
            "orca_auto.cli",
            *sys.argv[1:],
        ],
        environment,
    )


__all__ = [
    "PROCESS_IMPORT_SOURCE_ENV",
    "exec_with_import_source_evidence",
]
