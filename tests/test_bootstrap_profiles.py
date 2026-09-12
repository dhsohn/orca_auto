from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/bootstrap_wsl.sh"


@pytest.mark.parametrize("with_workflows", [False, True])
def test_bootstrap_installs_only_the_requested_local_projects(
    tmp_path: Path, with_workflows: bool
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "config").mkdir()
    (repo / "config/orca_auto.yaml.example").write_text("runs_root: /example\n", encoding="utf-8")
    shutil.copy2(_SCRIPT, repo / "scripts/bootstrap_wsl.sh")
    commands = tmp_path / "commands"
    commands.mkdir()
    sudo = commands / "sudo"
    sudo.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    sudo.chmod(0o755)
    python = commands / "fixture-python"
    python.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$BOOTSTRAP_TEST_LOG"\n'
        'if [ "$1 $2" = "-m venv" ]; then\n'
        '  mkdir -p "$3/bin"; cp "$0" "$3/bin/python"\n'
        "fi\nexit 0\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    calls = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "scripts/bootstrap_wsl.sh", *(["--with-workflows"] if with_workflows else [])],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
            "PYTHON_BIN": str(python),
            "ORCA_BIN": str(tmp_path / "no-engine"),
            "BOOTSTRAP_TEST_LOG": str(calls),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    installs = [
        line for line in calls.read_text().splitlines() if line.startswith("-m pip install -e")
    ]
    assert installs == [
        "-m pip install -e . -e ./extensions/workflows" if with_workflows else "-m pip install -e ."
    ]
    assert (repo / "config/orca_auto.yaml").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("argument", "exit_code"), [("--help", 0), ("--unknown", 2)])
def test_bootstrap_parses_options_before_any_installation(argument: str, exit_code: int) -> None:
    result = subprocess.run(
        ["/bin/bash", str(_SCRIPT), argument],
        env={"PATH": "/nonexistent"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == exit_code
    assert "command not found" not in result.stderr
    assert "Installing" not in result.stdout
