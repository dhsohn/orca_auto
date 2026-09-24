"""Worker freshness for prepared runtimes: process build pin versus installed pin.

A managed worker imports from a sealed runtime bundle and publishes that
bundle's build id at exec time. Its verdict never consults Git: the process is
fresh when its recorded build matches the bundle it imports from and the pin
the installed unit currently declares. Installing a unit and daemon-reloading
does not replace the running process, so a valid old bundle still reports a
pending cutover.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto import cli_systemd_units
from orca_auto.cli_systemd_evidence import (
    WorkerImportEvidence,
    WorkerVerdict,
    process_identity_race_detail,
)
from orca_auto.core.runtime_bundle import (
    PROCESS_RUNTIME_BUILD_ENV,
    runtime_root_for_import_source,
    verify_runtime_bundle,
)
from orca_auto.core.utils.coercion import normalize_text

_RUNTIME_ERRORS = (OSError, ValueError, RuntimeError)


def judge_runtime_worker(
    *,
    label: str,
    unit: str,
    pid: int,
    evidence: WorkerImportEvidence,
    run: Callable[..., subprocess.CompletedProcess[Any]],
    read_process_file: Callable[[str], bytes],
) -> WorkerVerdict:
    """Judge one worker that imports from no checkout.

    A prepared runtime is verified against its manifest and the build id the
    process published. An unmanaged installed wheel has nothing to compare, so
    it is reported as ``uncompared`` rather than turned into an undetermined
    deployment; in a mixed deployment the git-backed workers are still judged.
    """
    base_row: dict[str, Any] = {"label": label, "unit": unit, "pid": pid}
    import_source = evidence.import_source
    try:
        runtime_root = runtime_root_for_import_source(import_source)
        if runtime_root is not None:
            manifest = verify_runtime_bundle(runtime_root)
            if manifest["build_id"] != evidence.runtime_build_id:
                raise ValueError("worker runtime build evidence is missing or mismatched")
            detail = process_identity_race_detail(
                unit,
                pid=pid,
                process_start_ticks=evidence.process_start_ticks,
                run=run,
                read_process_file=read_process_file,
            )
            if detail:
                raise ValueError(detail)
            return WorkerVerdict(
                "worker",
                {
                    **base_row,
                    "import_source": str(import_source),
                    "source_root": str(runtime_root),
                    "process_start_ticks": evidence.process_start_ticks,
                    "runtime_build_id": manifest["build_id"],
                    "runtime_version": manifest["identity"]["version"],
                },
            )
        if evidence.runtime_build_id:
            raise ValueError("managed worker has no prepared runtime manifest")
    except _RUNTIME_ERRORS as exc:
        return WorkerVerdict(
            "undetermined",
            {
                **base_row,
                "import_source": str(import_source),
                "detail": f"cannot verify worker runtime: {exc}",
            },
        )
    return WorkerVerdict(
        "uncompared",
        {
            **base_row,
            "source_root": str(import_source.parent),
            "import_source": str(import_source),
            "process_start_ticks": evidence.process_start_ticks,
            "reason": "installed_distribution",
        },
    )


def installed_unit_property(
    unit: str,
    name: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    completed = cli_systemd_units.show_unit_property(unit, name, run=run)
    if completed.returncode != 0 or normalize_text(completed.stderr):
        raise ValueError(f"cannot read installed unit {name}")
    return str(completed.stdout or "").strip()


def judge_installed_runtime(
    verdict: WorkerVerdict,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
    read_process_file: Callable[[str], bytes],
) -> WorkerVerdict:
    """Compare process-bound source evidence with the currently installed pin.

    A unit without a runtime pin (an editable install) leaves the verdict as
    it was. A pinned unit turns a worker that runs another build or root into
    ``stale``, including the first switch from editable code.
    """
    if verdict.kind == "undetermined":
        return verdict
    row = verdict.row
    unit = row["unit"]
    try:
        environment = installed_unit_property(unit, "Environment", run=run)
        prefix = f"{PROCESS_RUNTIME_BUILD_ENV}="
        values = [
            item[len(prefix) :] for item in shlex.split(environment) if item.startswith(prefix)
        ]
        if not values:
            if row.get("runtime_build_id"):
                raise ValueError("installed unit has no pinned runtime build")
            return verdict
        if len(values) != 1 or not values[0]:
            raise ValueError("installed unit runtime build is missing or ambiguous")
        if any(
            installed_unit_property(unit, prop, run=run)
            for prop in ("EnvironmentFiles", "UnsetEnvironment")
        ):
            raise ValueError("installed unit has unsupported environment overrides")
        directory = installed_unit_property(unit, "WorkingDirectory", run=run)
        root = Path(directory)
        if not root.is_absolute() or root.resolve(strict=True) != root:
            raise ValueError("installed unit runtime root must be an absolute resolved path")
        manifest = verify_runtime_bundle(root)
        if manifest["build_id"] != values[0]:
            raise ValueError("installed unit pin differs from its prepared runtime")
        if (
            installed_unit_property(unit, "Environment", run=run) != environment
            or installed_unit_property(unit, "WorkingDirectory", run=run) != directory
        ):
            raise ValueError("installed unit runtime changed during freshness inspection")
        detail = process_identity_race_detail(
            unit,
            pid=row["pid"],
            process_start_ticks=row["process_start_ticks"],
            run=run,
            read_process_file=read_process_file,
        )
        if detail:
            raise ValueError(detail)
        return WorkerVerdict(
            "worker",
            {**row, "expected_runtime_build_id": values[0], "expected_runtime_root": str(root)},
            stale=(
                verdict.stale
                or row.get("runtime_build_id") != values[0]
                or row.get("source_root") != str(root)
            ),
        )
    except _RUNTIME_ERRORS as exc:
        return WorkerVerdict(
            "undetermined", {**row, "detail": f"cannot verify installed runtime: {exc}"}
        )


__all__ = ["installed_unit_property", "judge_installed_runtime", "judge_runtime_worker"]
