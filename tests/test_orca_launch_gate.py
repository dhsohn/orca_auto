from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_launch_gate_eof_never_starts_engine(tmp_path: Path) -> None:
    sentinel = tmp_path / "started"
    executable = tmp_path / "fake_orca"
    executable.write_text(
        f"#!/bin/sh\nprintf started > {sentinel}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    executable_fd = executable.open("rb")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "orca_auto.orca.launch_gate",
                str(executable),
                str(executable_fd.fileno()),
                "job.inp",
            ],
            cwd=tmp_path,
            input=b"",
            pass_fds=(executable_fd.fileno(),),
            check=False,
        )
    finally:
        executable_fd.close()

    assert completed.returncode == 125
    assert not sentinel.exists()


def test_launch_gate_executes_engine_only_after_release(tmp_path: Path) -> None:
    sentinel = tmp_path / "started"
    executable = tmp_path / "fake_orca"
    executable.write_text(
        f"#!/bin/sh\nprintf '%s' \"$1\" > {sentinel}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    executable_fd = executable.open("rb")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "orca_auto.orca.launch_gate",
                str(executable),
                str(executable_fd.fileno()),
                "job.inp",
            ],
            cwd=tmp_path,
            input=b"1",
            pass_fds=(executable_fd.fileno(),),
            check=False,
        )
    finally:
        executable_fd.close()

    assert completed.returncode == 0
    assert sentinel.read_text(encoding="utf-8") == "job.inp"


def test_launch_gate_executes_pinned_engine_after_path_replacement(tmp_path: Path) -> None:
    original_sentinel = tmp_path / "original-started"
    replacement_sentinel = tmp_path / "replacement-started"
    executable = tmp_path / "fake_orca"
    executable.write_text(
        f"#!/bin/sh\nprintf original > {original_sentinel}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    executable_fd = executable.open("rb")
    try:
        executable.rename(tmp_path / "pinned-fake-orca")
        executable.write_text(
            f"#!/bin/sh\nprintf replacement > {replacement_sentinel}\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "orca_auto.orca.launch_gate",
                str(executable),
                str(executable_fd.fileno()),
                "job.inp",
            ],
            cwd=tmp_path,
            input=b"1",
            pass_fds=(executable_fd.fileno(),),
            check=False,
        )
    finally:
        executable_fd.close()

    assert completed.returncode == 0
    assert original_sentinel.read_text(encoding="utf-8") == "original"
    assert not replacement_sentinel.exists()
