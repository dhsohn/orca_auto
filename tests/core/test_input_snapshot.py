from __future__ import annotations

import errno
from pathlib import Path

import pytest

from orca_auto.core.queue.engine import input_snapshot
from orca_auto.core.utils import persistence


def _source(job_dir: Path) -> Path:
    source = job_dir / "input.xyz"
    source.write_text("2\nH2\nH 0 0 0\nH 0 0 0.7\n", encoding="utf-8")
    return source


def test_snapshot_root_fsync_error_propagates_before_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    source = _source(job_dir)

    def fail_fsync(_path: str | Path) -> None:
        raise OSError(errno.EIO, "simulated root fsync failure")

    monkeypatch.setattr(persistence, "fsync_directory", fail_fsync)

    with pytest.raises(OSError, match="simulated root fsync failure"):
        input_snapshot.snapshot_input_file(job_dir, source, role="selected", namespace="job-1")

    assert not list((job_dir / input_snapshot.SNAPSHOT_DIR_NAME).glob("**/input-*"))


def test_snapshot_namespace_fsync_error_propagates_before_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    source = _source(job_dir)
    snapshot_root = job_dir / input_snapshot.SNAPSHOT_DIR_NAME
    snapshot_root.mkdir(mode=0o700)
    real_fsync = persistence.fsync_directory

    def fail_namespace_fsync(path: str | Path) -> None:
        if Path(path) == snapshot_root:
            raise OSError(errno.EACCES, "simulated namespace fsync failure")
        real_fsync(path)

    monkeypatch.setattr(persistence, "fsync_directory", fail_namespace_fsync)

    with pytest.raises(OSError, match="simulated namespace fsync failure"):
        input_snapshot.snapshot_input_file(job_dir, source, role="selected", namespace="job-2")

    assert not list(snapshot_root.glob("**/input-*"))


def test_snapshot_publication_fsync_error_never_returns_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    source = _source(job_dir)

    def fail_publication_fsync(_path: str | Path) -> None:
        raise OSError(errno.EIO, "simulated publication fsync failure")

    monkeypatch.setattr(input_snapshot, "fsync_directory", fail_publication_fsync)

    with pytest.raises(OSError, match="simulated publication fsync failure"):
        input_snapshot.snapshot_input_file(job_dir, source, role="selected", namespace="job-3")


def test_stable_read_enforces_cumulative_cap_for_growing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "growing.bin"
    source.write_bytes(b"x")
    chunks = iter((b"abc", b"de", b"must-not-be-read"))
    read_sizes: list[int] = []

    def growing_read(_descriptor: int, size: int) -> bytes:
        read_sizes.append(size)
        return next(chunks)

    monkeypatch.setattr(input_snapshot.os, "read", growing_read)

    with pytest.raises(ValueError, match="exceeds 4 bytes"):
        input_snapshot.read_stable_regular_file(source, max_bytes=4)

    assert read_sizes == [5, 2]


def test_cleanup_unowned_snapshot_namespace_removes_only_requested_generation(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    source = _source(job_dir)
    first = input_snapshot.snapshot_input_file(
        job_dir,
        source,
        role="selected",
        namespace="generation-first",
    )
    second = input_snapshot.snapshot_input_file(
        job_dir,
        source,
        role="selected",
        namespace="generation-second",
    )

    input_snapshot.cleanup_unowned_input_snapshot_namespace(job_dir, "generation-first")

    assert not Path(first["snapshot_path"]).exists()
    assert Path(second["snapshot_path"]).is_file()


def test_snapshot_namespace_reservation_is_exclusive_and_preserves_owner(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    namespace = input_snapshot.reserve_input_snapshot_namespace(job_dir, "same-generation")
    owner_marker = namespace / "owner.txt"
    owner_marker.write_text("owner", encoding="utf-8")

    with pytest.raises(FileExistsError):
        input_snapshot.reserve_input_snapshot_namespace(job_dir, "same-generation")

    assert owner_marker.read_text(encoding="utf-8") == "owner"


@pytest.mark.parametrize(
    "namespace",
    ["x" * 81, "contains spaces", "../escape", " leading", "trailing "],
)
def test_snapshot_namespace_rejects_lossy_path_normalization(
    tmp_path: Path,
    namespace: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    with pytest.raises(ValueError, match="safe path segment"):
        input_snapshot.reserve_input_snapshot_namespace(job_dir, namespace)
