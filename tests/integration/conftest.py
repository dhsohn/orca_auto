from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from orca_auto.core.admission import AdmissionSlot, list_slots

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pythonpath() -> str:
    roots = [str(REPO_ROOT)]
    existing = os.environ.get("PYTHONPATH", "").strip()
    if existing:
        roots.append(existing)
    return ":".join(roots)


def _app_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = _pythonpath()
    return env


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _write_shared_config(
    path: Path,
    *,
    workflow_root: Path,
    admission_root: Path,
    xtb_executable: Path,
    crest_executable: Path,
) -> None:
    path.write_text(
        textwrap.dedent(
            f"""
            runs_root: {workflow_root}
            scheduler:
              max_active_simulations: 1
              admission_root: {admission_root}
            workflow:
              paths:
                xtb_executable: {xtb_executable}
                crest_executable: {crest_executable}
            resources:
              max_cores_per_task: 2
              max_memory_gb_per_task: 2
            telegram:
              bot_token: ""
              chat_id: ""
            """
        ).lstrip(),
        encoding="utf-8",
    )


@dataclass(frozen=True)
class SmokeWorkspace:
    root: Path
    repo_root: Path
    pythonpath: str
    xtb_allowed_root: Path
    crest_allowed_root: Path
    admission_root: Path
    config_path: Path
    xtb_config_path: Path
    crest_config_path: Path
    fake_xtb: Path
    fake_crest: Path


@pytest.fixture(autouse=True)
def _require_repo_root() -> None:
    if not REPO_ROOT.exists():
        pytest.skip(f"repository root not found: {REPO_ROOT}")


@pytest.fixture
def smoke_workspace(tmp_path: Path) -> SmokeWorkspace:
    root = tmp_path / "integration_smoke"
    workflow_root = root / "workflow_root"
    xtb_allowed_root = workflow_root / "wf_xtb_manual" / "02_xtb"
    crest_allowed_root = workflow_root / "wf_crest_manual" / "01_crest"
    admission_root = root / "admission"
    bin_dir = root / "bin"

    for path in (
        workflow_root,
        xtb_allowed_root,
        crest_allowed_root,
        admission_root,
        bin_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)

    fake_xtb = bin_dir / "fake_xtb"
    _write_executable(
        fake_xtb,
        """
        #!/usr/bin/env bash
        set -euo pipefail

        mode="opt"
        for arg in "$@"; do
          if [[ "$arg" == "--path" ]]; then
            mode="path_search"
            break
          fi
        done

        if [[ "$mode" == "path_search" ]]; then
          printf '2\\nenergy: -0.45\\nH 0.0 0.0 0.0\\nH 0.0 0.0 0.74\\n' > xtbpath_ts.xyz
          printf '2\\nenergy: -0.90\\nH 0.0 0.0 0.0\\nH 0.0 0.0 0.80\\n2\\nenergy: -0.55\\nH 0.0 0.0 0.0\\nH 0.0 0.0 0.78\\n2\\nenergy: -0.18\\nH 0.0 0.0 0.0\\nH 0.0 0.0 0.76\\n2\\nenergy: 0.05\\nH 0.0 0.0 0.0\\nH 0.0 0.0 0.75\\n2\\nenergy: 0.22\\nH 0.0 0.0 0.0\\nH 0.0 0.0 0.74\\n2\\nenergy: 0.11\\nH 0.0 0.0 0.0\\nH 0.0 0.0 0.73\\n2\\nenergy: -0.12\\nH 0.0 0.0 0.0\\nH 0.0 0.0 0.72\\n' > xtbpath_0.xyz
          printf '{"total energy": -4.2, "electronic energy": -4.4}\\n' > xtbout.json
          printf 'forward barrier (kcal) : 12.4\\n'
          printf 'backward barrier (kcal) : 8.6\\n'
          printf 'reaction energy (kcal) : -3.1\\n'
          printf 'estimated TS on file xtbpath_ts.xyz\\n'
          printf 'path 0 taken with 7 points\\n'
          exit 0
        fi

        printf '1\\nfake xtb optimized\\nH 0.0 0.0 0.0\\n' > xtbopt.xyz
        : > .xtboptok
        printf '{"total energy": -4.2, "electronic energy": -4.4}\\n' > xtbout.json
        printf 'charges\\n' > charges
        printf 'wbo\\n' > wbo
        printf 'topology\\n' > xtbtopo.mol
        exit 0
        """,
    )

    fake_crest = bin_dir / "fake_crest"
    _write_executable(
        fake_crest,
        """
        #!/usr/bin/env bash
        set -euo pipefail

        cat > crest_conformers.xyz <<'EOF'
        1
        conf_a
        H 0.0 0.0 0.0
        1
        conf_b
        H 0.0 0.0 0.1
        EOF
        cat > crest_best.xyz <<'EOF'
        1
        best
        H 0.0 0.0 0.0
        EOF
        exit 0
        """,
    )

    config_path = root / "orca_auto.yaml"
    _write_shared_config(
        config_path,
        workflow_root=workflow_root,
        admission_root=admission_root,
        xtb_executable=fake_xtb,
        crest_executable=fake_crest,
    )

    return SmokeWorkspace(
        root=root,
        repo_root=REPO_ROOT,
        pythonpath=_pythonpath(),
        xtb_allowed_root=xtb_allowed_root,
        crest_allowed_root=crest_allowed_root,
        admission_root=admission_root,
        config_path=config_path,
        xtb_config_path=config_path,
        crest_config_path=config_path,
        fake_xtb=fake_xtb,
        fake_crest=fake_crest,
    )


@pytest.fixture
def app_runner(smoke_workspace: SmokeWorkspace):
    def _run(repo_root: Path, module_name: str, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", module_name, *argv],
            cwd=repo_root,
            env=_app_env(),
            capture_output=True,
            text=True,
            check=False,
        )

    return _run


@pytest.fixture
def spawn_app(smoke_workspace: SmokeWorkspace):
    def _spawn(repo_root: Path, module_name: str, *argv: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-m", module_name, *argv],
            cwd=repo_root,
            env=_app_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    return _spawn


def _write_xyz(path: Path, *, comment: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "3",
                comment,
                "O 0.000000 0.000000 0.000000",
                "H 0.000000 0.000000 0.970000",
                "H 0.000000 0.750000 -0.240000",
                "",
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture
def xtb_opt_job(smoke_workspace: SmokeWorkspace, app_runner) -> Path:
    job_dir = smoke_workspace.xtb_allowed_root / "manual_xtb"
    _write_xyz(job_dir / "input.xyz", comment="manual xTB opt")
    (job_dir / "xtb_job.yaml").write_text(
        "job_type: opt\ngfn: 2\ncharge: 0\nuhf: 0\ninput_xyz: input.xyz\ndry_run: true\n",
        encoding="utf-8",
    )
    return job_dir


@pytest.fixture
def xtb_path_search_job(smoke_workspace: SmokeWorkspace, app_runner) -> Path:
    job_dir = smoke_workspace.xtb_allowed_root / "manual_path_search"
    _write_xyz(job_dir / "reactants" / "r1.xyz", comment="manual xTB reactant")
    _write_xyz(job_dir / "products" / "p1.xyz", comment="manual xTB product")
    (job_dir / "xtb_job.yaml").write_text(
        "\n".join(
            [
                "job_type: path_search",
                "gfn: 2",
                "charge: 0",
                "uhf: 0",
                "reactant_xyz: r1.xyz",
                "product_xyz: p1.xyz",
                "dry_run: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return job_dir


@pytest.fixture
def crest_job(smoke_workspace: SmokeWorkspace, app_runner) -> Path:
    job_dir = smoke_workspace.crest_allowed_root / "manual_crest"
    _write_xyz(job_dir / "input.xyz", comment="manual CREST input")
    (job_dir / "crest_job.yaml").write_text(
        "mode: standard\nspeed: quick\ngfn: 2\ninput_xyz: input.xyz\n",
        encoding="utf-8",
    )
    return job_dir


def wait_for_active_slots(
    root: Path, *, expected: int, timeout: float = 5.0
) -> list[AdmissionSlot]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        slots = list_slots(root)
        if len(slots) == expected:
            return slots
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {expected} active admission slot(s)")
