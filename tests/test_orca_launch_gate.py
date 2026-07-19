from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from orca_auto.orca import launch_gate


def _run_launch_gate(
    executable_fd: int,
    executable_display: str,
    input_name: str,
    *,
    cwd: Path,
    release: bytes,
    clean_startup: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    gate_path = Path(launch_gate.__file__).resolve()
    gate_fd = os.open(gate_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        gate_details = os.fstat(gate_fd)
        executable_details = os.fstat(executable_fd)
        environment = dict(os.environ)
        environment.update(
            {
                "EXPECTED_GATE_DEVICE": str(gate_details.st_dev),
                "EXPECTED_GATE_INODE": str(gate_details.st_ino),
                "EXPECTED_EXECUTABLE_DEVICE": str(executable_details.st_dev),
                "EXPECTED_EXECUTABLE_INODE": str(executable_details.st_ino),
            }
        )
        command = ["/proc/self/exe"]
        if clean_startup:
            command.append("-S")
        command.extend(
            [
                f"/proc/self/fd/{gate_fd}",
                str(gate_fd),
                executable_display,
                str(executable_fd),
                input_name,
            ]
        )
        return subprocess.run(
            command,
            cwd=cwd,
            input=release,
            env=environment,
            pass_fds=(gate_fd, executable_fd),
            check=False,
        )
    finally:
        os.close(gate_fd)


_ASSERT_PINNED_FDS_CLOSED = """
import os
from pathlib import Path

expected = {
    (int(os.environ["EXPECTED_GATE_DEVICE"]), int(os.environ["EXPECTED_GATE_INODE"])),
    (
        int(os.environ["EXPECTED_EXECUTABLE_DEVICE"]),
        int(os.environ["EXPECTED_EXECUTABLE_INODE"]),
    ),
}
for candidate in Path("/proc/self/fd").iterdir():
    try:
        details = candidate.stat()
    except OSError:
        continue
    if (details.st_dev, details.st_ino) in expected:
        raise SystemExit(91)
"""


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
        completed = _run_launch_gate(
            executable_fd.fileno(),
            str(executable),
            "job.inp",
            cwd=tmp_path,
            release=b"",
        )
    finally:
        executable_fd.close()

    assert completed.returncode == 125
    assert not sentinel.exists()


def test_launch_gate_clean_startup_does_not_import_package_siblings(tmp_path: Path) -> None:
    executable = tmp_path / "fake_orca"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    executable_fd = executable.open("rb")
    try:
        completed = _run_launch_gate(
            executable_fd.fileno(),
            str(executable),
            "job.inp",
            cwd=tmp_path,
            release=b"",
            clean_startup=True,
        )
    finally:
        executable_fd.close()

    assert completed.returncode == 125


def test_launch_gate_executes_engine_only_after_release(tmp_path: Path) -> None:
    sentinel = tmp_path / "started"
    executable = tmp_path / "fake_orca"
    executable.write_text(
        f"#!{sys.executable}\n"
        f"{_ASSERT_PINNED_FDS_CLOSED}\n"
        "import sys\n"
        f"Path({str(sentinel)!r}).write_text(sys.argv[1], encoding='utf-8')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    executable_fd = executable.open("rb")
    try:
        completed = _run_launch_gate(
            executable_fd.fileno(),
            str(executable),
            "job.inp",
            cwd=tmp_path,
            release=b"1",
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
        completed = _run_launch_gate(
            executable_fd.fileno(),
            str(executable),
            "job.inp",
            cwd=tmp_path,
            release=b"1",
        )
    finally:
        executable_fd.close()

    assert completed.returncode == 0
    assert original_sentinel.read_text(encoding="utf-8") == "original"
    assert not replacement_sentinel.exists()


def test_launch_gate_native_executable_does_not_inherit_pinned_fds(tmp_path: Path) -> None:
    sentinel = tmp_path / "native-started"
    input_path = tmp_path / "job.py"
    input_path.write_text(
        f"{_ASSERT_PINNED_FDS_CLOSED}\n"
        f"Path({str(sentinel)!r}).write_text('clean', encoding='utf-8')\n",
        encoding="utf-8",
    )
    executable = Path(sys.executable).resolve()
    executable_fd = executable.open("rb")
    try:
        completed = _run_launch_gate(
            executable_fd.fileno(),
            str(executable),
            input_path.name,
            cwd=tmp_path,
            release=b"1",
        )
    finally:
        executable_fd.close()

    assert completed.returncode == 0
    assert sentinel.read_text(encoding="utf-8") == "clean"


def test_launch_gate_preserves_shebang_exit_status(tmp_path: Path) -> None:
    executable = tmp_path / "fake_orca"
    executable.write_text(
        f"#!{sys.executable}\nraise SystemExit(37)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    executable_fd = executable.open("rb")
    try:
        completed = _run_launch_gate(
            executable_fd.fileno(),
            str(executable),
            "job.inp",
            cwd=tmp_path,
            release=b"1",
        )
    finally:
        executable_fd.close()

    assert completed.returncode == 37


def test_launch_gate_preserves_shebang_signal_status(tmp_path: Path) -> None:
    executable = tmp_path / "fake_orca"
    executable.write_text(
        f"#!{sys.executable}\nimport os\nimport signal\nos.kill(os.getpid(), signal.SIGTERM)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    executable_fd = executable.open("rb")
    try:
        completed = _run_launch_gate(
            executable_fd.fileno(),
            str(executable),
            "job.inp",
            cwd=tmp_path,
            release=b"1",
        )
    finally:
        executable_fd.close()

    assert completed.returncode == -signal.SIGTERM
