"""Safety tests for untrusted run-dir archive inspection and extraction."""

from __future__ import annotations

import hashlib
import io
import stat
import tarfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from orca_auto.core.ingest import (
    UploadPolicy,
    UploadRejected,
    extract_archive,
    inspect_archive,
)
from orca_auto.core.ingest import archive as archive_module


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
    assert report.selected_entry == "job.inp"

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


def test_atomic_publication_preserves_normal_mkdir_mode(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"x"})
    dest = tmp_path / "runs"
    dest.mkdir()
    reference = dest / "reference"
    reference.mkdir()
    expected_mode = stat.S_IMODE(reference.stat().st_mode)
    reference.rmdir()

    job_dir = extract_archive(archive, dest, "mode", _policy())

    assert stat.S_IMODE(job_dir.stat().st_mode) == expected_mode


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


@pytest.mark.parametrize(
    "name",
    ["job\nname.inp", f"{'a' * 252}.inp"],
)
def test_rejects_unrepresentable_archive_paths(tmp_path: Path, name: str) -> None:
    archive = _zip(tmp_path / "bad-name.zip", {name: b"x"})
    with pytest.raises(UploadRejected, match="control character|too long"):
        inspect_archive(archive, _policy())


def test_rejects_archive_path_symlink(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "real.zip", {"job.inp": b"x"})
    link = tmp_path / "link.zip"
    link.symlink_to(archive)
    with pytest.raises(UploadRejected, match="regular file"):
        inspect_archive(link, _policy())


def test_rejects_zip_symlink_member(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("job.inp")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(info, "../../outside")

    with pytest.raises(UploadRejected, match="symlink"):
        inspect_archive(archive, _policy())


def test_rejects_encrypted_zip_member_before_confirmation(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "encrypted.zip", {"job.inp": b"x"})
    payload = bytearray(archive.read_bytes())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        header = payload.index(signature)
        flags = int.from_bytes(payload[header + flag_offset : header + flag_offset + 2], "little")
        payload[header + flag_offset : header + flag_offset + 2] = (flags | 1).to_bytes(2, "little")
    archive.write_bytes(payload)

    with pytest.raises(UploadRejected, match="encrypted zip member"):
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


def test_zip_entry_preflight_does_not_trust_underreported_eocd_count(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "many.zip", {f"f{i}.xyz": b"x" for i in range(5)})
    payload = bytearray(archive.read_bytes())
    eocd = payload.rfind(b"PK\x05\x06")
    assert eocd >= 0
    payload[eocd + 8 : eocd + 12] = b"\x01\x00\x01\x00"
    archive.write_bytes(payload)

    with pytest.raises(UploadRejected, match="more than 3 entries"):
        inspect_archive(archive, _policy(max_entries=3))


def test_zip_entry_preflight_rejects_unneeded_zip64_eocd_metadata(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "zip64.zip", {"job.inp": b"x"})
    payload = bytearray(archive.read_bytes())
    eocd = payload.rfind(b"PK\x05\x06")
    assert eocd >= 0
    payload[eocd + 12 : eocd + 16] = b"\xff\xff\xff\xff"
    archive.write_bytes(payload)

    with pytest.raises(UploadRejected, match="ZIP64 central-directory"):
        inspect_archive(archive, _policy())


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


def test_tar_gz_bounds_pax_metadata_in_raw_stream(tmp_path: Path) -> None:
    archive = tmp_path / "pax.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tf:
        info = tarfile.TarInfo("job/run.inp")
        info.size = 1
        info.pax_headers = {"comment": "x" * (128 * 1024)}
        tf.addfile(info, io.BytesIO(b"x"))

    # The regular member is one byte, but hidden PAX metadata inflates the raw
    # tar stream well beyond this cap.
    with pytest.raises(UploadRejected, match="tar stream expands beyond"):
        inspect_archive(archive, _policy(max_total_uncompressed_bytes=64 * 1024))


def test_tar_gz_bounds_gnu_longname_metadata_in_raw_stream(tmp_path: Path) -> None:
    archive = tmp_path / "longname.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.GNU_FORMAT) as tf:
        info = tarfile.TarInfo(f"job/{'a' * (128 * 1024)}.inp")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))

    # GNU longname records are metadata entries consumed internally by tarfile;
    # they must still count against the decompressed-container ceiling.
    with pytest.raises(UploadRejected, match="tar stream expands beyond"):
        inspect_archive(archive, _policy(max_total_uncompressed_bytes=64 * 1024))


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


def test_extract_rejects_overlong_job_name(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"x"})
    with pytest.raises(UploadRejected, match="run-dir name is too long"):
        extract_archive(archive, tmp_path / "runs", "a" * 256, _policy())


@pytest.mark.parametrize("job_name", ["queue.json", "queue.lock", "workflow_registry.json"])
def test_extract_rejects_runtime_reserved_job_name(tmp_path: Path, job_name: str) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"x"})

    with pytest.raises(UploadRejected, match="reserved for runtime state"):
        extract_archive(archive, tmp_path / "runs", job_name, _policy())


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


def test_extraction_is_hidden_until_atomic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"! opt\n"})
    dest = tmp_path / "runs"
    copying = threading.Event()
    release = threading.Event()
    real_copy = archive_module._copy_bounded

    def paused_copy(source: object, sink: object, limit: int) -> int:
        copying.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release extraction")
        return real_copy(source, sink, limit)  # type: ignore[arg-type]

    monkeypatch.setattr(archive_module, "_copy_bounded", paused_copy)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(extract_archive, archive, dest, "atomic", _policy())
        assert copying.wait(timeout=5)
        try:
            assert not (dest / "atomic").exists()
            assert len(list(dest.glob(".upload-extract-*"))) == 1
        finally:
            release.set()
        job_dir = future.result(timeout=5)

    assert job_dir == dest / "atomic"
    assert (job_dir / "job.inp").read_bytes() == b"! opt\n"
    assert not list(dest.glob(".upload-extract-*"))


def test_extract_failure_removes_only_owned_temp_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"! opt\n"})
    dest = tmp_path / "runs"
    existing = dest / "same"
    existing.mkdir(parents=True)
    sentinel = existing / "sentinel.txt"
    sentinel.write_text("owned elsewhere", encoding="utf-8")

    def fail_copy(source: object, sink: object, limit: int) -> int:
        del source, sink, limit
        raise OSError("injected extraction failure")

    monkeypatch.setattr(archive_module, "_copy_bounded", fail_copy)
    with pytest.raises(UploadRejected, match="extraction failed"):
        extract_archive(archive, dest, "same", _policy())

    assert sentinel.read_text(encoding="utf-8") == "owned elsewhere"
    assert not (dest / "same-2").exists()
    assert not list(dest.glob(".upload-extract-*"))


def test_extract_rejects_a_member_shorter_than_its_validated_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"! opt\n"})
    dest = tmp_path / "runs"
    real_copy = archive_module._copy_bounded

    def misreport_copy(source: object, sink: object, limit: int) -> int:
        copied = real_copy(source, sink, limit)  # type: ignore[arg-type]
        return copied - 1

    monkeypatch.setattr(archive_module, "_copy_bounded", misreport_copy)
    with pytest.raises(UploadRejected, match="truncated during extraction"):
        extract_archive(archive, dest, "short", _policy())

    assert not (dest / "short").exists()
    assert not list(dest.glob(".upload-extract-*"))


def test_publish_failure_cleans_temp_and_empty_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"! opt\n"})
    dest = tmp_path / "runs"

    def fail_replace(source: object, destination: object) -> None:
        del source, destination
        raise OSError("injected publication failure")

    monkeypatch.setattr(archive_module.os, "replace", fail_replace)
    with pytest.raises(UploadRejected, match="extraction failed"):
        extract_archive(archive, dest, "publish", _policy())

    assert not (dest / "publish").exists()
    assert not list(dest.glob(".upload-extract-*"))


def test_extract_uses_one_stable_snapshot_when_source_path_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"original"})
    replacement = _zip(tmp_path / "replacement.zip", {"job.inp": b"replacement"})
    replacement_bytes = replacement.read_bytes()
    real_build_plan = archive_module._build_plan

    def replace_after_plan(source: object, policy: UploadPolicy) -> object:
        plan = real_build_plan(source, policy)  # type: ignore[arg-type]
        archive.write_bytes(replacement_bytes)
        return plan

    monkeypatch.setattr(archive_module, "_build_plan", replace_after_plan)
    job_dir = extract_archive(archive, tmp_path / "runs", "stable", _policy())

    assert (job_dir / "job.inp").read_bytes() == b"original"


def test_extract_rejects_bytes_that_do_not_match_confirmed_digest(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"confirmed"})
    confirmed = archive.read_bytes()
    replacement = _zip(tmp_path / "replacement.zip", {"job.inp": b"different"})
    archive.write_bytes(replacement.read_bytes())

    with pytest.raises(UploadRejected, match="confirmed digest"):
        extract_archive(
            archive,
            tmp_path / "runs",
            "stable",
            _policy(),
            expected_size=len(confirmed),
            expected_sha256=hashlib.sha256(confirmed).hexdigest(),
        )

    assert not (tmp_path / "runs" / "stable").exists()


def test_snapshot_rejects_source_mutation_while_copying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"original"})
    original = archive.read_bytes()
    real_copy = archive_module._copy_archive_bounded

    def mutate_then_copy(source: object, sink: object, limit: int) -> int:
        archive.write_bytes(original + b"changed")
        return real_copy(source, sink, limit)  # type: ignore[arg-type]

    monkeypatch.setattr(archive_module, "_copy_archive_bounded", mutate_then_copy)
    with pytest.raises(UploadRejected, match="changed while it was being read"):
        inspect_archive(archive, _policy())


def test_concurrent_same_name_extractions_claim_distinct_owned_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _zip(tmp_path / "up.zip", {"job.inp": b"! opt\n"})
    dest = tmp_path / "runs"
    barrier = threading.Barrier(2)
    real_fresh_dir = archive_module._fresh_dir

    def synchronized_claim(dest_root: Path, job_name: str) -> Path:
        claimed = real_fresh_dir(dest_root, job_name)
        barrier.wait(timeout=5)
        return claimed

    monkeypatch.setattr(archive_module, "_fresh_dir", synchronized_claim)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(extract_archive, archive, dest, "same", _policy()) for _ in range(2)
        ]
        extracted = [future.result(timeout=5) for future in futures]

    assert {path.name for path in extracted} == {"same", "same-2"}
    assert all((path / "job.inp").read_bytes() == b"! opt\n" for path in extracted)


@pytest.mark.parametrize(
    ("entries", "reason"),
    [
        ({"readme.txt": b"x", "nested/job.inp": b"! opt\n"}, "root-level"),
        ({"flow.yml": b"workflow_type: conformer\n"}, "flow.yml is not supported"),
        ({"geometry.xyz": b"1\n\nH 0 0 0\n"}, "no supported root-level"),
        ({"JOB.INP": b"! opt\n"}, "lower-case"),
        (
            {"readme.txt": b"x", "nested/flow.yaml": b"workflow_type: conformer\n"},
            "manifest must be root-level",
        ),
        ({"FLOW.YAML": b"workflow_type: conformer\n"}, "named exactly flow.yaml"),
    ],
)
def test_rejects_layouts_without_exact_root_entrypoint(
    tmp_path: Path,
    entries: dict[str, bytes],
    reason: str,
) -> None:
    archive = _zip(tmp_path / "layout.zip", entries)
    with pytest.raises(UploadRejected, match=reason):
        inspect_archive(archive, _policy())


def test_rejects_multiple_orca_entrypoints(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "multi.zip", {"a.inp": b"a", "b.inp": b"b"})
    with pytest.raises(UploadRejected, match="multiple root-level"):
        inspect_archive(archive, _policy())


@pytest.mark.parametrize(
    "filename",
    [
        "-job.inp",
        ".job.inp",
        "job name.inp",
        "job;id.inp",
        "job$(id).inp",
        "작업.inp",
        f"{'a' * 125}.inp",
    ],
)
def test_rejects_orca_entrypoint_names_unsafe_for_internal_module_commands(
    tmp_path: Path,
    filename: str,
) -> None:
    archive = _zip(tmp_path / "unsafe-entry.zip", {filename: b"! r2scan-3c\n"})

    with pytest.raises(UploadRejected, match="shell-safe"):
        inspect_archive(archive, _policy())


def test_rejects_ambiguous_workflow_and_orca_entrypoints(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "mixed.zip",
        {"flow.yaml": b"workflow_type: conformer\n", "job.inp": b"! opt\n"},
    )
    with pytest.raises(UploadRejected, match="ambiguous"):
        inspect_archive(archive, _policy())


def test_rejects_duplicate_normalized_file_paths(tmp_path: Path) -> None:
    archive = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("job.inp", b"first")
        zf.writestr("./job.inp", b"second")
    with pytest.raises(UploadRejected, match="duplicate final file paths"):
        inspect_archive(archive, _policy())


def test_rejects_file_directory_path_collision(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "collision.zip",
        {
            "job.inp": b"! opt\n",
            "inputs.xyz": b"1\n\nH 0 0 0\n",
            "inputs.xyz/note.txt": b"collision",
        },
    )
    with pytest.raises(UploadRejected, match="file/directory path collision"):
        inspect_archive(archive, _policy())


@pytest.mark.parametrize(
    "basename",
    [
        "workflow.json",
        "job_state.json",
        "orca.process.json",
        "queue.json",
        "xtb_job.yaml",
        "crest_job.yaml",
        "upload_sessions.json",
        "WORKFLOW.JSON",
        ".orca-auto-upload",
    ],
)
def test_rejects_uploaded_runtime_state_basenames(tmp_path: Path, basename: str) -> None:
    archive = _zip(
        tmp_path / "state.zip",
        {"job.inp": b"! opt\n", f"nested/{basename}": b"{}"},
    )
    with pytest.raises(UploadRejected, match="runtime state file is not allowed"):
        inspect_archive(archive, _policy())


def test_rejects_orca_nodes_even_when_operator_extension_list_includes_it(
    tmp_path: Path,
) -> None:
    archive = _zip(
        tmp_path / "remote-hosts.zip",
        {"job.inp": b"! opt PAL2\n", "job.nodes": b"untrusted-host slots=2\n"},
    )
    allowed = UploadPolicy().allowed_extensions + (".nodes",)

    with pytest.raises(UploadRejected, match="runtime control file is not allowed"):
        inspect_archive(archive, _policy(allowed_extensions=allowed))
