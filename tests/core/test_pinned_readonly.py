from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Isolate the open in a killable process: without nonblocking acquisition these
# readers wait for a FIFO writer before their existing regular-file check runs.
_FIFO_READER = """
import os
import sys
from pathlib import Path
from orca_auto.core import engine_process, engine_scratch

reader, root_text = sys.argv[1:]
root = Path(root_text)
path = root / "artifact.out"
directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
durable_fd = os.open(root / "durable", os.O_RDONLY | os.O_DIRECTORY)
before_fds = len(list(Path("/proc/self/fd").iterdir()))
try:
    try:
        if reader == "tail":
            engine_process.read_confined_tail_lines(
                root, path, label="Tail", max_lines=5
            )
        elif reader == "scratch_input":
            engine_scratch._read_stable_regular_file_at(
                directory_fd, path.name, display_path=path
            )
        elif reader == "scratch_hash":
            engine_scratch._regular_file_sha256_at(
                directory_fd, path.name, display_path=path
            )
        elif reader == "scratch_copy":
            engine_scratch._copy_artifact_to_staging(
                path.name, root, directory_fd, root / "durable", durable_fd
            )
        else:
            raise AssertionError(reader)
    except (ValueError, engine_scratch.EngineScratchError) as exc:
        expected = {
            "tail": "Tail must be a single-link regular file: ",
            "scratch_input": "engine input is not a private regular file: ",
            "scratch_hash": "engine durable artifact is unsafe: ",
            "scratch_copy": "engine scratch artifact is unsafe: ",
        }[reader]
        assert str(exc) == expected + str(path), str(exc)
    else:
        raise AssertionError("FIFO was accepted")
    assert len(list(Path("/proc/self/fd").iterdir())) == before_fds
    assert not list((root / "durable").iterdir())
finally:
    os.close(durable_fd)
    os.close(directory_fd)
"""


@pytest.mark.parametrize("reader", ["tail", "scratch_input", "scratch_hash", "scratch_copy"])
def test_pinned_readers_reject_fifo_without_blocking(tmp_path: Path, reader: str) -> None:
    os.mkfifo(tmp_path / "artifact.out")
    (tmp_path / "durable").mkdir()
    result = subprocess.run(
        [sys.executable, "-c", _FIFO_READER, reader, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
