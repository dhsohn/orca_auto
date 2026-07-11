"""Safety tests for untrusted run-dir archive inspection and extraction."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from orca_auto.core.ingest import (
    UploadPolicy,
    UploadRejected,
    extract_archive,
    inspect_archive,
)


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def _policy(**overrides: object) -> UploadPolicy:
    base: dict[str, object] = {"enabled": True}
    base.update(overrides)
    return UploadPolicy(**base)  # type: ignore[arg-type]


def test_happy_path_zip_strips_common_top(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "mol42.zip",
        {"mol42/job.inp": b"! r2scan-3c\n", "mol42/geo.xyz": b"1\n\nH 0 0 0\n"},
    )
    report = inspect_archive(archive, _policy())
    assert report.entry_count == 2
    assert report.engine_hint == "orca"
    assert report.suggested_name == "mol42"

    dest = tmp_path / "runs"
    job_dir = extract_archive(archive, dest, "mol42", _policy())
    assert job_dir == dest / "mol42"
    assert (job_dir / "job.inp").read_bytes() == b"! r2scan-3c\n"
    assert (job_dir / "geo.xyz").exists()
    # Common top directory is stripped, not nested.
    assert not (job_dir / "mol42").exists()


def test_workflow_hint_from_manifest(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "rxn.zip",
        {"rxn/flow.yaml": b"kind: scan_ts\n", "rxn/reactants/r.xyz": b"1\n\nH 0 0 0\n"},
    )
    assert inspect_archive(archive, _policy()).engine_hint == "workflow"


def test_root_level_files_use_job_name(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"x", "b.xyz": b"y"})
    job_dir = extract_archive(archive, tmp_path / "runs", "chosen", _policy())
    assert job_dir.name == "chosen"
    assert (job_dir / "job.inp").exists()


def test_name_collision_suffixes(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"x"})
    dest = tmp_path / "runs"
    first = extract_archive(archive, dest, "mol", _policy())
    second = extract_archive(archive, dest, "mol", _policy())
    assert first.name == "mol"
    assert second.name == "mol-2"


def test_rejects_zip_slip(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "evil.zip", {"../escape.inp": b"x"})
    with pytest.raises(UploadRejected, match="traversal"):
        inspect_archive(archive, _policy())


def test_rejects_absolute_path(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "evil.zip", {"/etc/passwd.inp": b"x"})
    with pytest.raises(UploadRejected, match="absolute"):
        inspect_archive(archive, _policy())


def test_rejects_disallowed_extension(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "evil.zip", {"payload.sh": b"rm -rf /"})
    with pytest.raises(UploadRejected, match="not allowed"):
        inspect_archive(archive, _policy())


def test_rejects_per_file_bomb(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "big.zip", {"job.inp": b"0" * 4096})
    with pytest.raises(UploadRejected, match="exceeds"):
        inspect_archive(archive, _policy(max_file_bytes=1024))


def test_rejects_total_uncompressed_bomb(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "big.zip",
        {"a.xyz": b"0" * 4096, "b.xyz": b"0" * 4096},
    )
    with pytest.raises(UploadRejected, match="expands beyond"):
        inspect_archive(archive, _policy(max_total_uncompressed_bytes=4096))


def test_rejects_too_many_entries(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "many.zip", {f"f{i}.xyz": b"x" for i in range(5)})
    with pytest.raises(UploadRejected, match="more than"):
        inspect_archive(archive, _policy(max_entries=3))


def test_rejects_oversize_archive(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"0" * 8192})
    with pytest.raises(UploadRejected, match="archive exceeds"):
        inspect_archive(archive, _policy(max_archive_bytes=64))


def test_rejects_non_archive(tmp_path: Path) -> None:
    plain = tmp_path / "not.zip"
    plain.write_bytes(b"just text, definitely not an archive")
    with pytest.raises(UploadRejected, match="neither a valid"):
        inspect_archive(plain, _policy())


def test_rejects_empty_archive(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "empty.zip", {})
    with pytest.raises(UploadRejected, match="no usable"):
        inspect_archive(archive, _policy())


def test_tar_gz_happy_path(tmp_path: Path) -> None:
    archive = tmp_path / "job.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        data = b"! r2scan-3c\n"
        info = tarfile.TarInfo("job/run.inp")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    job_dir = extract_archive(archive, tmp_path / "runs", "job", _policy())
    assert (job_dir / "run.inp").read_bytes() == b"! r2scan-3c\n"


def test_tar_rejects_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as tf:
        link = tarfile.TarInfo("job/link.xyz")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    with pytest.raises(UploadRejected, match="symlink"):
        inspect_archive(archive, _policy())


def test_rejects_directory_bomb_early(tmp_path: Path) -> None:
    # An archive of only directory entries must trip the entry cap, not sail past
    # both the file and byte caps and get enumerated in full.
    archive = tmp_path / "dirs.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for i in range(50):
            info = tarfile.TarInfo(f"d{i}/")
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
    with pytest.raises(UploadRejected, match="more than 3 entries"):
        inspect_archive(archive, _policy(max_entries=3))


def test_rejects_tar_xz(tmp_path: Path) -> None:
    archive = tmp_path / "job.tar.xz"
    with tarfile.open(archive, "w:xz") as tf:
        data = b"! r2scan-3c\n"
        info = tarfile.TarInfo("job/run.inp")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    with pytest.raises(UploadRejected, match="gzip-compressed or uncompressed"):
        inspect_archive(archive, _policy())


def test_malformed_archive_raises_upload_rejected_not_library_error(tmp_path: Path) -> None:
    # A tar.gz truncated mid-stream must surface as UploadRejected, never a raw
    # tarfile.ReadError leaking out of inspect_archive.
    good = tmp_path / "job.tar.gz"
    with tarfile.open(good, "w:gz") as tf:
        data = b"x" * 8192
        info = tarfile.TarInfo("job/big.xyz")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    raw = good.read_bytes()
    truncated = tmp_path / "trunc.tar.gz"
    truncated.write_bytes(raw[: len(raw) - 40])
    with pytest.raises(UploadRejected):
        inspect_archive(truncated, _policy())


def test_extract_rejects_unsafe_job_name(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"x"})
    dest = tmp_path / "runs"
    for bad in ("../evil", "a/b", "/abs", "..", ""):
        with pytest.raises(UploadRejected, match="unsafe run-dir name|escapes"):
            extract_archive(archive, dest, bad, _policy())
    # A sibling-escaping name must not have created anything outside dest.
    assert not (tmp_path / "evil").exists()


def test_extract_cleans_up_on_rejection(tmp_path: Path) -> None:
    good = _zip(tmp_path / "good.zip", {"job.inp": b"x"})
    dest = tmp_path / "runs"
    # A policy that passes inspection but a mid-extract failure would leave no dir.
    extract_archive(good, dest, "ok", _policy())
    # Rejected archive must not leave a partial run-dir behind.
    evil = _zip(tmp_path / "evil.zip", {"../x.inp": b"x"})
    with pytest.raises(UploadRejected):
        extract_archive(evil, dest, "evil", _policy())
    assert not (dest / "evil").exists()
