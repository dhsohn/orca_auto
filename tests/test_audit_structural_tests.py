from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_structural_tests.sh"


@pytest.mark.parametrize("scan_exit", [0, 1, 2, 127])
def test_structural_audit_distinguishes_no_matches_from_scan_failure(
    tmp_path: Path, scan_exit: int
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / SCRIPT.name
    shutil.copy2(SCRIPT, script)
    (repo / "tests").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rg = bin_dir / "rg"
    rg.write_text(
        "#!/bin/sh\n"
        + (
            "echo 'tests/test_example.py:1:def test_forwards_example():'\n"
            if scan_exit == 0
            else ""
        )
        + ("echo 'partial match'; echo 'scan failed' >&2\n" if scan_exit > 1 else "")
        + f"exit {scan_exit}\n",
        encoding="utf-8",
    )
    rg.chmod(0o755)
    result = subprocess.run(
        ["bash", str(script)],
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    if scan_exit < 2:
        assert result.returncode == 0, result.stderr
        assert f"structural-test-name matches: {int(scan_exit == 0)}" in result.stdout
    else:
        assert result.returncode == scan_exit
        assert "scan failed" in result.stderr
        assert "structural-test-name matches:" not in result.stdout
        assert "partial match" not in result.stdout
