"""State and report files are opened as regular files only, never blocking.

Every ``job_state.json``/``machine.json`` reader goes through
``core.utils.json_artifacts.read_json_artifact``: a FIFO is refused without
waiting for a writer, as are a directory, a symlink and a file over the byte
limit, and the other directories are still listed and indexed. Each FIFO case
runs in a child process with a timeout, so a regression fails instead of
hanging the suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from orca_auto.core.artifacts import MAX_RUN_ARTIFACT_JSON_BYTES
from orca_auto.core.utils.json_artifacts import read_json_artifact
from orca_auto.orca.state_reading import read_run_state, state_path
from orca_auto.orca.statuses import RunStatus
from tests.conftest import write_run_state

_REPO = Path(__file__).resolve().parents[2]


def _in_child(code: str, *, timeout: float = 20.0) -> object:
    """Run ``code`` in a fresh interpreter; its last stdout line is JSON."""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _runs_with_a_fifo_state(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    write_run_state(runs / "good", status=RunStatus.COMPLETED, job_id="job-good")
    write_run_state(runs / "fifo", status=RunStatus.COMPLETED, job_id="job-fifo")
    state_path(runs / "fifo").unlink()
    os.mkfifo(state_path(runs / "fifo"))
    return runs


def test_reading_a_fifo_state_is_refused_without_blocking(tmp_path: Path) -> None:
    runs = _runs_with_a_fifo_state(tmp_path)

    result = _in_child(
        f"""
        import json
        from pathlib import Path
        from orca_auto.orca.state_reading import read_run_state
        read = read_run_state(Path({str(runs / "fifo")!r}))
        print(json.dumps([read.kind, read.reason]))
        """
    )

    assert result == ["corrupt", "not_regular"]


def test_rebuild_skips_a_fifo_state_and_indexes_the_others(tmp_path: Path) -> None:
    runs = _runs_with_a_fifo_state(tmp_path)

    result = _in_child(
        f"""
        import json
        from orca_auto.orca.job_locations import rebuild_job_location_records
        preview = rebuild_job_location_records({str(runs)!r}, apply=False)
        print(json.dumps([sorted(row.job_id for row in preview.added), list(preview.skipped)]))
        """
    )

    assert result == [["job-good"], [str(runs / "fifo")]]


def test_run_listing_skips_a_fifo_state_and_lists_the_others(tmp_path: Path) -> None:
    runs = _runs_with_a_fifo_state(tmp_path)

    result = _in_child(
        f"""
        import json
        from pathlib import Path
        from orca_auto.orca.run_snapshot import collect_run_snapshots
        snapshots = collect_run_snapshots(Path({str(runs)!r}))
        print(json.dumps([snapshot.reaction_dir.name for snapshot in snapshots]))
        """
    )

    assert result == ["good"]


def test_a_fifo_report_is_ignored_and_the_state_still_indexes(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    write_run_state(runs / "job", status=RunStatus.COMPLETED, job_id="job-1")
    os.mkfifo(runs / "job" / "machine.json")

    result = _in_child(
        f"""
        import json
        from orca_auto.orca.job_locations import rebuild_job_location_records
        preview = rebuild_job_location_records({str(runs)!r}, apply=False)
        print(json.dumps(sorted(row.job_id for row in preview.added)))
        """
    )

    assert result == ["job-1"]


@pytest.mark.parametrize("kind", ["directory", "symlink", "too-large", "not-json", "not-utf8"])
def test_an_unusable_state_file_is_corrupt(tmp_path: Path, kind: str) -> None:
    write_run_state(tmp_path, status=RunStatus.COMPLETED, job_id="job-1")
    path = state_path(tmp_path)
    if kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "elsewhere.json"
        path.rename(target)
        path.symlink_to(target)
    elif kind == "too-large":
        with path.open("r+b") as handle:  # sparse, so the test writes nothing
            handle.truncate(MAX_RUN_ARTIFACT_JSON_BYTES + 1)
    elif kind == "not-json":
        path.write_text("{not json", encoding="utf-8")
    else:
        path.write_bytes(b'{"x": "\\xff"}'.replace(b"\\xff", b"\xff"))

    read = read_run_state(tmp_path)

    assert read.kind == "corrupt"
    assert read.reason


def test_a_missing_and_a_valid_artifact_read_as_before(tmp_path: Path) -> None:
    assert read_json_artifact(tmp_path / "absent.json", max_bytes=10).kind == "missing"
    (tmp_path / "ok.json").write_text('{"a": 1}', encoding="utf-8")

    read = read_json_artifact(tmp_path / "ok.json", max_bytes=100)

    assert (read.kind, read.value) == ("valid", {"a": 1})
    assert read.status is not None and read.status.st_size == 8


_OVERLONG_INTEGER = '{"unused":' + "9" * 4301 + "}"


def test_an_integer_over_the_parser_limit_reads_as_invalid(tmp_path: Path) -> None:
    # The parser raises a plain ValueError (not a JSONDecodeError) for an
    # integer past Python's int-to-str digit limit.
    (tmp_path / "big.json").write_text(_OVERLONG_INTEGER, encoding="utf-8")

    read = read_json_artifact(tmp_path / "big.json", max_bytes=1 << 20)

    assert read.kind == "invalid"
    assert "ValueError" in read.reason


def test_an_unparseable_state_beside_a_good_one_does_not_stop_the_readers(
    tmp_path: Path,
) -> None:
    from orca_auto.orca.job_locations import rebuild_job_location_records
    from orca_auto.orca.run_snapshot import collect_run_snapshots

    runs = tmp_path / "runs"
    write_run_state(runs / "good", status=RunStatus.COMPLETED, job_id="job-good")
    (runs / "big").mkdir()
    state_path(runs / "big").write_text(_OVERLONG_INTEGER, encoding="utf-8")

    assert read_run_state(runs / "big").kind == "corrupt"
    assert [snapshot.reaction_dir.name for snapshot in collect_run_snapshots(runs)] == ["good"]
    preview = rebuild_job_location_records(runs, apply=False)
    assert [row.job_id for row in preview.added] == ["job-good"]
