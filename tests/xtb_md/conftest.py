from __future__ import annotations

import copy
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml

from orca_auto.xtb_md.manifest import XtbMdManifestLimits, load_manifest


@dataclass(frozen=True)
class XtbMdRuntimeCase:
    root: Path
    runs_root: Path
    admission_root: Path
    config_path: Path
    executable: Path
    job_dir: Path


def _write_fake_xtb(
    path: Path,
    *,
    mode: Literal["success", "false_success", "slow"],
    version: str,
) -> None:
    mode_body = {
        "success": r"""
            cat "$1" "$1" > xtb.trj
            printf '%s\n' \
              '-1.0' \
              '0.1 0.0 0.0 0.0 0.0 0.0' \
              '0.1 0.0 0.0 0.0 0.0 0.0' \
              '0.1 0.0 0.0 0.0 0.0 0.0' > mdrestart
            : > xtbmdok
            printf '%s\n' 'max steps          :     2' 'normal exit of md()'
            printf '%s\n' 'normal termination of xtb' >&2
        """,
        "false_success": r"""
            cat "$1" "$1" > xtb.trj
            printf '%s\n' \
              '-1.0' \
              '0.1 0.0 0.0 0.0 0.0 0.0' \
              '0.1 0.0 0.0 0.0 0.0 0.0' \
              '0.1 0.0 0.0 0.0 0.0 0.0' > mdrestart
            : > xtbmdok
            printf '%s\n' \
              'max steps          :     2' \
              'MD is unstable, emergency exit' \
              'normal exit of md()'
            printf '%s\n' 'normal termination of xtb' >&2
        """,
        "slow": r"""
            sleep 30
        """,
    }[mode]
    script = rf"""
        #!/bin/sh
        set -eu
        if [ "${{1:-}}" = "--version" ]; then
            printf '%s\n' 'xtb version {version}'
            exit 0
        fi
        {textwrap.dedent(mode_body).strip()}
    """
    path.write_text(textwrap.dedent(script).lstrip(), encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def manifest_limits() -> XtbMdManifestLimits:
    return XtbMdManifestLimits(
        max_atoms=100,
        max_steps=1000,
        max_atom_steps=10_000,
        max_frames=1000,
        max_walltime_seconds=3600,
        max_cores=8,
        max_memory_gb=32,
    )


@pytest.fixture
def valid_manifest_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "input_xyz": "water.xyz",
        "gfn": 2,
        "charge": 0,
        "uhf": 0,
        "ensemble": "nvt",
        "temperature_k": 298.15,
        "time_ps": 0.008,
        "walltime_seconds": 300,
        "step_fs": 4.0,
        "dump_fs": 4.0,
        "hydrogen_mass_amu": 4,
        "shake": 2,
        "scc_accuracy": 2.0,
        "resources": {"max_cores": 2, "max_memory_gb": 4},
    }


@pytest.fixture
def manifest_job(
    tmp_path: Path,
    valid_manifest_payload: dict[str, Any],
    manifest_limits: XtbMdManifestLimits,
):
    def build(payload: dict[str, Any] | None = None):
        job_dir = tmp_path / f"job-{len(list(tmp_path.iterdir()))}"
        job_dir.mkdir()
        (job_dir / "water.xyz").write_text(
            "3\nwater\nO 0 0 0\nH 0.75 0 0.5\nH -0.75 0 0.5\n",
            encoding="utf-8",
        )
        manifest_payload = dict(valid_manifest_payload if payload is None else payload)
        (job_dir / "xtb_md_job.yaml").write_text(
            yaml.safe_dump(manifest_payload, sort_keys=False),
            encoding="utf-8",
        )
        return job_dir, load_manifest(job_dir, limits=manifest_limits)

    return build


@pytest.fixture
def runtime_case_factory(tmp_path: Path, valid_manifest_payload: dict[str, Any]):
    counter = 0

    def build(
        *,
        mode: Literal["success", "false_success", "slow"] = "success",
        version: str = "6.7.1",
        manifest_payload: dict[str, Any] | None = None,
    ) -> XtbMdRuntimeCase:
        nonlocal counter
        counter += 1
        root = tmp_path / f"runtime-{counter}"
        runs_root = root / "runs"
        admission_root = root / "admission"
        bin_dir = root / "bin"
        job_dir = runs_root / "water-md"
        for directory in (runs_root, admission_root, bin_dir, job_dir):
            directory.mkdir(parents=True, exist_ok=True)

        executable = bin_dir / "xtb"
        _write_fake_xtb(executable, mode=mode, version=version)
        config_path = root / "orca_auto.yaml"
        config_path.write_text(
            textwrap.dedent(
                f"""
                runs_root: {runs_root}
                scheduler:
                  max_active_simulations: 2
                  max_active_xtb_md: 1
                  admission_root: {admission_root}
                workflow:
                  paths:
                    xtb_executable: {executable}
                resources:
                  max_cores_per_task: 2
                  max_memory_gb_per_task: 4
                """
            ).lstrip(),
            encoding="utf-8",
        )
        (job_dir / "water.xyz").write_text(
            "3\nwater snapshot\nO 0 0 0\nH 0.75 0 0.5\nH -0.75 0 0.5\n",
            encoding="utf-8",
        )
        payload = copy.deepcopy(
            valid_manifest_payload if manifest_payload is None else manifest_payload
        )
        (job_dir / "xtb_md_job.yaml").write_text(
            yaml.safe_dump(payload, sort_keys=False),
            encoding="utf-8",
        )
        return XtbMdRuntimeCase(
            root=root,
            runs_root=runs_root,
            admission_root=admission_root,
            config_path=config_path,
            executable=executable,
            job_dir=job_dir,
        )

    return build
