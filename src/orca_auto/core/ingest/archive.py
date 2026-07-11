"""Safe inspection and extraction of uploaded run-dir archives.

An uploaded archive is untrusted: it may attempt path traversal (``../`` or
absolute members), smuggle symlinks that point outside the target, or inflate to
exhaust the disk.  :func:`inspect_archive` validates an archive against an
:class:`UploadPolicy` without writing anything; :func:`extract_archive` re-runs
the same validation and materializes the run-dir under a trusted root.  Both are
pure with respect to configuration and share one member-validation pass so the
inspection shown to a user can never diverge from what extraction accepts.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import posixpath
import re
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, NoReturn, Protocol

from .policy import UploadPolicy

_FLOW_MANIFEST_NAME = "flow.yaml"
_FLOW_MANIFEST_ALIAS = "flow.yml"

# These are generated durable state, coordination, or report artifacts.  They
# are outputs of a trusted local run, never valid remote-upload inputs.  In
# particular, accepting ``workflow.json`` would turn an upload into a restart of
# attacker-supplied persisted workflow state rather than a fresh ``flow.yaml``
# submission.
_FORBIDDEN_RUNTIME_BASENAMES = frozenset(
    {
        ".dft_monitor_state.json",
        ".orca-auto-upload",
        ".orca.process.lock",
        ".upload-session",
        ".upload_sessions.lock",
        ".workflow_create.lock",
        "admission.lock",
        "admission_slots.json",
        "crest_job.yaml",
        "crest_queue_worker.pid",
        "job_locations.json",
        "job_locations.lock",
        "job_report.html",
        "job_report.json",
        "job_report.md",
        "job_state.json",
        "orca.process.json",
        "queue.json",
        "queue.lock",
        "queue_worker.pid",
        "records.jsonl",
        "run.lock",
        "si_block.md",
        "si_data.csv",
        "upload_sessions.json",
        "workflow.json",
        "workflow.lock",
        "workflow_registry.journal.jsonl",
        "workflow_registry.json",
        "workflow_registry.lock",
        "workflow_registry_cleared.json",
        "workflow_report.html",
        "workflow_si.md",
        "workflow_worker.lock",
        "workflow_worker_state.json",
        "xtb_job.yaml",
        "xtb_queue_worker.pid",
    }
)
_FORBIDDEN_RUNTIME_SUFFIXES = frozenset(
    {
        # Parallel ORCA automatically consumes ``<input-stem>.nodes`` and may
        # use its host list for remote process launch. It is executable runtime
        # configuration, not an inert run-dir input artifact.
        ".nodes",
    }
)

_GZIP_MAGIC = b"\x1f\x8b"
_BZIP2_MAGIC = b"BZh"
_XZ_MAGIC = b"\xfd7zXZ\x00"
_MAX_PATH_COMPONENT_BYTES = 255
_MAX_RELATIVE_PATH_BYTES = 2048
_ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP_CENTRAL_SIGNATURE = b"PK\x01\x02"
_ZIP_EOCD_SIZE = 22
_ZIP_MAX_COMMENT_BYTES = (1 << 16) - 1
_ZIP_CENTRAL_HEADER_SIZE = 46
_SAFE_ORCA_ENTRYPOINT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+\-]{0,123}\.inp\Z")


class _BinaryReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class UploadRejected(Exception):
    """Raised when an archive violates the upload policy or is malformed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Member:
    """One regular-file entry, with its archive-relative POSIX path."""

    name: str
    size: int


@dataclass(frozen=True)
class _ArchivePlan:
    format: str
    members: tuple[_Member, ...]
    common_top: str
    entry_count: int
    total_uncompressed: int
    engine_hint: str
    selected_entry: str


@dataclass(frozen=True)
class ArchiveReport:
    """Validated summary of an archive, safe to show before extraction."""

    entry_count: int
    total_uncompressed: int
    engine_hint: str
    suggested_name: str
    selected_entry: str = ""


def _reject(reason: str) -> NoReturn:
    raise UploadRejected(reason)


def _safe_relpath(raw_name: str) -> str | None:
    """Normalize an archive member name to a safe relative POSIX path.

    Returns ``None`` for directory-only entries. Raises on names that are
    absolute, escape via ``..``, contain control characters, or exceed the
    conservative filesystem-safe path bounds. Windows separators are normalized
    before applying the same checks.
    """

    if any(ord(character) < 32 or ord(character) == 127 for character in raw_name):
        _reject(f"control character in archive path: {raw_name!r}")
    name = raw_name.replace("\\", "/").strip()
    if not name:
        _reject("archive contains an empty entry name")
    if name.startswith("/"):
        _reject(f"absolute path in archive: {raw_name!r}")
    # A Windows drive prefix (``C:``) would also escape a POSIX join.
    if len(name) >= 2 and name[1] == ":":
        _reject(f"drive-qualified path in archive: {raw_name!r}")
    pure = PurePosixPath(name)
    parts = [part for part in pure.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        _reject(f"path traversal in archive: {raw_name!r}")
    if not parts:
        return None
    rel = posixpath.join(*parts)
    try:
        component_sizes = [len(os.fsencode(part)) for part in parts]
        path_size = len(os.fsencode(rel))
    except UnicodeError:
        _reject(f"archive path cannot be represented on this filesystem: {raw_name!r}")
    if any(size > _MAX_PATH_COMPONENT_BYTES for size in component_sizes):
        _reject(f"archive path component is too long: {raw_name!r}")
    if path_size > _MAX_RELATIVE_PATH_BYTES:
        _reject(f"archive path is too long: {raw_name!r}")
    return rel


def _detect_format(archive: BinaryIO) -> str:
    archive.seek(0)
    is_zip = zipfile.is_zipfile(archive)
    archive.seek(0)
    if is_zip:
        return "zip"
    # Do not use tarfile.is_tarfile() here.  For gzip inputs it decompresses and
    # parses the stream before our expansion limit is in force.  Non-zip input is
    # instead opened by the bounded tar reader below; malformed input is
    # normalized to UploadRejected by _safe_plan.
    return "tar"


@contextmanager
def _open_tar(
    archive: BinaryIO,
    max_stream_bytes: int,
) -> Iterator[tarfile.TarFile]:
    """Open a tar accepting only gzip-compressed or uncompressed streams.

    ``mode="r:*"`` would transparently decompress xz and bzip2, whose much higher
    compression ratios amplify the cost of a decompression bomb. The feature is
    advertised as ``.tar.gz``/``.tar`` only, so those are the only modes accepted.

    A gzip stream is fully materialized into a temporary raw-tar file *before*
    tarfile sees it.  The byte ceiling therefore includes tar headers, padding,
    GNU longname records, and PAX metadata -- data that member.size accounting
    does not expose.  This also makes both inspection and extraction obey the
    same hard decompression boundary.
    """

    archive.seek(0)
    magic = archive.read(6)
    archive.seek(0)
    if magic[:2] == _GZIP_MAGIC:
        with tempfile.TemporaryFile(mode="w+b") as raw_tar:
            with gzip.GzipFile(fileobj=archive, mode="rb") as source:
                _copy_tar_stream_bounded(source, raw_tar, max_stream_bytes)
            raw_tar.seek(0)
            with tarfile.open(fileobj=raw_tar, mode="r:") as tf:
                yield tf
        return
    if magic[:3] == _BZIP2_MAGIC or magic[:6] == _XZ_MAGIC:
        _reject("only gzip-compressed or uncompressed tar archives are accepted")
    archive.seek(0, 2)
    stream_size = archive.tell()
    if stream_size > max_stream_bytes:
        _reject(f"tar stream expands beyond {max_stream_bytes} bytes")
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:") as tf:
        yield tf


def _copy_tar_stream_bounded(source: _BinaryReader, sink: BinaryIO, limit: int) -> None:
    written = 0
    while True:
        # Read at most one byte beyond the remaining budget.  This both proves
        # overflow and prevents a tiny configured ceiling from still allocating
        # a full MiB of attacker-controlled decompressor output.
        chunk = source.read(min(1024 * 1024, limit - written + 1))
        if not chunk:
            return
        written += len(chunk)
        if written > limit:
            _reject(f"tar stream expands beyond {limit} bytes")
        sink.write(chunk)


def _preflight_zip_entry_count(archive: BinaryIO, max_entries: int) -> None:
    """Bound central-directory object creation before ``ZipFile`` loads it.

    ``ZipFile`` eagerly materializes the complete central directory, so counting
    entries from ``infolist()`` is too late for an entry-count memory bomb. Scan
    the fixed headers directly first; the compressed archive-byte cap bounds the
    amount of metadata this parser can encounter.
    """

    archive.seek(0, 2)
    archive_size = archive.tell()
    tail_size = min(archive_size, _ZIP_EOCD_SIZE + _ZIP_MAX_COMMENT_BYTES)
    archive.seek(archive_size - tail_size)
    tail = archive.read(tail_size)
    eocd_offset = tail.rfind(_ZIP_EOCD_SIGNATURE)
    if eocd_offset < 0 or eocd_offset + _ZIP_EOCD_SIZE > len(tail):
        _reject("zip archive has no valid end-of-central-directory record")

    eocd = tail[eocd_offset : eocd_offset + _ZIP_EOCD_SIZE]
    comment_size = int.from_bytes(eocd[20:22], "little")
    if eocd_offset + _ZIP_EOCD_SIZE + comment_size != len(tail):
        _reject("zip archive has malformed trailing data")
    disk_number = int.from_bytes(eocd[4:6], "little")
    central_disk = int.from_bytes(eocd[6:8], "little")
    entries_on_disk = int.from_bytes(eocd[8:10], "little")
    declared_entries = int.from_bytes(eocd[10:12], "little")
    central_size = int.from_bytes(eocd[12:16], "little")
    central_offset = int.from_bytes(eocd[16:20], "little")
    if disk_number or central_disk or entries_on_disk != declared_entries:
        _reject("multi-disk zip archives are not accepted")

    # A saturated EOCD field denotes ZIP64 replacement metadata. Every valid
    # reason for these fields (>=65,535 entries or >=4 GiB directory
    # offsets/metadata) is outside this ingress contract. Reject it rather than
    # letting the eager stdlib parser allocate from attacker-controlled values.
    if declared_entries == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        if declared_entries > max_entries:
            _reject(f"archive has more than {max_entries} entries")
        _reject("ZIP64 central-directory metadata is not accepted")

    absolute_eocd = archive_size - tail_size + eocd_offset
    central_start = absolute_eocd - central_size
    if central_start < 0 or central_offset > central_start:
        _reject("zip archive has an invalid central-directory offset")
    archive.seek(central_start)
    remaining = central_size
    observed_entries = 0
    while remaining:
        if remaining < _ZIP_CENTRAL_HEADER_SIZE:
            _reject("zip archive has a truncated central-directory header")
        header = archive.read(_ZIP_CENTRAL_HEADER_SIZE)
        if len(header) != _ZIP_CENTRAL_HEADER_SIZE or not header.startswith(_ZIP_CENTRAL_SIGNATURE):
            _reject("zip archive has an invalid central-directory header")
        variable_size = sum(
            int.from_bytes(header[offset : offset + 2], "little") for offset in (28, 30, 32)
        )
        record_size = _ZIP_CENTRAL_HEADER_SIZE + variable_size
        if record_size > remaining:
            _reject("zip archive has a truncated central-directory entry")
        archive.seek(variable_size, 1)
        remaining -= record_size
        observed_entries += 1
        if observed_entries > max_entries:
            _reject(f"archive has more than {max_entries} entries")
    if observed_entries != declared_entries:
        _reject("zip archive central-directory entry count is inconsistent")
    archive.seek(0)


def _iter_zip(archive: BinaryIO, max_entries: int) -> Iterator[tuple[str, bool, int]]:
    _preflight_zip_entry_count(archive, max_entries)
    archive.seek(0)
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.flag_bits & 0x1:
                _reject(f"encrypted zip member is not accepted: {info.filename!r}")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(unix_mode)
            if file_type == stat.S_IFLNK:
                _reject(f"symlink in archive: {info.filename!r}")
            if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                _reject(f"special file in archive: {info.filename!r}")
            is_dir = info.is_dir()
            if file_type == stat.S_IFDIR and not is_dir:
                _reject(f"directory type has a non-directory name: {info.filename!r}")
            yield info.filename, is_dir, int(info.file_size)


def _iter_tar(
    archive: BinaryIO,
    max_stream_bytes: int,
) -> Iterator[tuple[str, bool, int]]:
    with _open_tar(archive, max_stream_bytes) as tf:
        for member in tf:
            if member.issym() or member.islnk():
                _reject(f"symlink or hardlink in archive: {member.name!r}")
            if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                _reject(f"special file in archive: {member.name!r}")
            if not (member.isfile() or member.isdir()):
                _reject(f"unsupported archive member type: {member.name!r}")
            yield member.name, member.isdir(), int(member.size)


def _validate_members(
    archive: BinaryIO,
    fmt: str,
    policy: UploadPolicy,
) -> list[_Member]:
    iterator = (
        _iter_zip(archive, policy.max_entries)
        if fmt == "zip"
        else _iter_tar(archive, policy.max_total_uncompressed_bytes)
    )
    members: list[_Member] = []
    total = 0
    count = 0
    for raw_name, is_dir, size in iterator:
        # Count EVERY member (directories included) against the cap and reject
        # early. A directory-only archive would otherwise trip neither the file
        # nor the byte cap, letting millions of entries exhaust RAM/CPU while the
        # iterator enumerates them all before the terminal "no usable files".
        count += 1
        if count > policy.max_entries:
            _reject(f"archive has more than {policy.max_entries} entries")
        rel = _safe_relpath(raw_name)
        if rel is None or is_dir:
            continue
        if size < 0:
            _reject(f"archive member has a negative size: {rel}")
        basename = PurePosixPath(rel).name.lower()
        if basename in _FORBIDDEN_RUNTIME_BASENAMES:
            _reject(f"runtime state file is not allowed: {rel}")
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix in _FORBIDDEN_RUNTIME_SUFFIXES:
            _reject(f"runtime control file is not allowed: {rel}")
        if not policy.allows_suffix(suffix):
            shown = suffix or PurePosixPath(rel).name
            _reject(f"file type not allowed: {shown}")
        if size > policy.max_file_bytes:
            _reject(f"file exceeds {policy.max_file_bytes} bytes: {rel}")
        total += size
        if total > policy.max_total_uncompressed_bytes:
            _reject(f"archive expands beyond {policy.max_total_uncompressed_bytes} bytes")
        members.append(_Member(name=rel, size=size))
    if not members:
        _reject("archive contains no usable run-dir files")
    return members


def _common_top(members: tuple[_Member, ...]) -> str:
    """Return the single shared top-level directory, or ``""`` if none.

    ``mol42.zip`` conventionally wraps its contents in a ``mol42/`` directory;
    stripping it keeps the extracted run-dir one level deep.
    """

    tops = {member.name.split("/", 1)[0] for member in members}
    if len(tops) != 1:
        return ""
    top = next(iter(tops))
    # Only a real prefix directory (every member is ``top/...``) is strippable.
    if all("/" in member.name for member in members):
        return top
    return ""


def _final_member_names(members: tuple[_Member, ...], common_top: str) -> tuple[str, ...]:
    strip = f"{common_top}/" if common_top else ""
    return tuple(
        member.name[len(strip) :] if strip and member.name.startswith(strip) else member.name
        for member in members
    )


def _select_entrypoint(members: tuple[_Member, ...], common_top: str) -> tuple[str, str]:
    """Return the unambiguous root-level run-dir engine and entrypoint.

    The local CLI has richer restart semantics and mtime-based ORCA input
    selection.  Neither is suitable for untrusted archives: persisted workflow
    state is forbidden above, and archive entry order must not decide which of
    multiple inputs runs.  Remote ingress therefore accepts exactly one fresh
    ``flow.yaml`` or one root-level, lower-case ``*.inp``.
    """

    names = _final_member_names(members, common_top)
    lowered = tuple(name.lower() for name in names)

    if len(names) != len(set(names)):
        _reject("archive contains duplicate final file paths")
    paths = set(names)
    for name in names:
        parts = PurePosixPath(name).parts
        for length in range(1, len(parts)):
            prefix = posixpath.join(*parts[:length])
            if prefix in paths:
                _reject(f"archive has a file/directory path collision: {prefix}")

    if any(PurePosixPath(name).name == _FLOW_MANIFEST_ALIAS for name in lowered):
        _reject("flow.yml is not supported; name the root manifest flow.yaml")

    flow_like = [
        name
        for name, lower in zip(names, lowered, strict=True)
        if PurePosixPath(lower).name == _FLOW_MANIFEST_NAME
    ]
    if any(name != _FLOW_MANIFEST_NAME for name in flow_like):
        _reject("workflow manifest must be root-level and named exactly flow.yaml")

    inp_like = [
        name
        for name, lower in zip(names, lowered, strict=True)
        if PurePosixPath(lower).suffix == ".inp"
    ]
    if any("/" in name or not name.endswith(".inp") for name in inp_like):
        _reject("ORCA entrypoint must be one root-level lower-case *.inp file")

    root_flow = _FLOW_MANIFEST_NAME in names
    root_inputs = sorted(name for name in names if "/" not in name and name.endswith(".inp"))
    if root_flow and root_inputs:
        _reject("archive is ambiguous: it contains both flow.yaml and an ORCA input")
    if root_flow:
        return "workflow", _FLOW_MANIFEST_NAME
    if len(root_inputs) > 1:
        _reject("archive contains multiple root-level ORCA *.inp files")
    if root_inputs:
        if _SAFE_ORCA_ENTRYPOINT_RE.fullmatch(root_inputs[0]) is None:
            _reject(
                "ORCA entrypoint filename must be a short ASCII shell-safe name: "
                f"{root_inputs[0]!r}"
            )
        return "orca", root_inputs[0]
    _reject("archive has no supported root-level flow.yaml or *.inp entrypoint")
    return "", ""  # unreachable


def _build_plan(archive: BinaryIO, policy: UploadPolicy) -> _ArchivePlan:
    fmt = _detect_format(archive)
    members = tuple(_validate_members(archive, fmt, policy))
    common_top = _common_top(members)
    total = sum(member.size for member in members)
    engine_hint, selected_entry = _select_entrypoint(members, common_top)
    return _ArchivePlan(
        format=fmt,
        members=members,
        common_top=common_top,
        entry_count=len(members),
        total_uncompressed=total,
        engine_hint=engine_hint,
        selected_entry=selected_entry,
    )


def _safe_plan(archive: BinaryIO, policy: UploadPolicy) -> _ArchivePlan:
    """``_build_plan`` with library parse errors normalized to ``UploadRejected``.

    A truncated or corrupt archive can raise ``BadZipFile``/``tarfile.ReadError``
    or a decompressor error while being read; callers should only ever have to
    catch ``UploadRejected``.
    """

    try:
        return _build_plan(archive, policy)
    except UploadRejected:
        raise
    except tarfile.ReadError as exc:
        raise UploadRejected("archive is neither a valid .zip nor a .tar/.tar.gz") from exc
    except Exception as exc:  # noqa: BLE001 - corrupt/malformed archive input
        raise UploadRejected(f"archive could not be read: {type(exc).__name__}") from exc


@contextmanager
def _open_archive_snapshot(
    archive_path: Path,
    max_archive_bytes: int,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> Iterator[BinaryIO]:
    """Yield stable uploaded bytes from an automatically cleaned temporary file.

    Validation and extraction must consume the same bytes.  Copying through a
    bounded, unnamed temporary file closes the source descriptor before parsing,
    enforces the compressed-byte limit even if the source changes while read,
    and prevents a path replacement between planning and extraction.
    """

    try:
        snapshot = tempfile.TemporaryFile(mode="w+b")
    except OSError as exc:
        raise UploadRejected(f"archive could not be read: {type(exc).__name__}") from exc
    try:
        try:
            before = os.stat(archive_path, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                _reject("uploaded archive must be a regular file, not a symlink or device")
            if before.st_size > max_archive_bytes:
                _reject(f"archive exceeds {max_archive_bytes} bytes")

            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(archive_path, flags)
            try:
                with os.fdopen(descriptor, "rb") as source:
                    descriptor = -1
                    opened = os.fstat(source.fileno())
                    if not stat.S_ISREG(opened.st_mode) or not _same_file(before, opened):
                        _reject("uploaded archive changed before it could be read")
                    copied = _copy_archive_bounded(source, snapshot, max_archive_bytes)
                    after = os.fstat(source.fileno())
                    path_after = os.stat(archive_path, follow_symlinks=False)
                    if (
                        copied != opened.st_size
                        or not _same_file(opened, after, include_times=True)
                        or not _same_file(after, path_after, include_times=True)
                    ):
                        _reject("uploaded archive changed while it was being read")
                    if expected_size is not None and copied != expected_size:
                        _reject("uploaded archive no longer matches the confirmed size")
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except UploadRejected:
            raise
        except FileNotFoundError as exc:
            raise UploadRejected("uploaded archive is missing") from exc
        except Exception as exc:  # noqa: BLE001 - unstable/missing source file
            raise UploadRejected(f"archive could not be read: {type(exc).__name__}") from exc
        snapshot.seek(0)
        if expected_sha256 is not None:
            digest = hashlib.sha256()
            while chunk := snapshot.read(1024 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != expected_sha256.strip().lower():
                _reject("uploaded archive no longer matches the confirmed digest")
        snapshot.seek(0)
        yield snapshot
    finally:
        snapshot.close()


def _same_file(
    left: os.stat_result,
    right: os.stat_result,
    *,
    include_times: bool = False,
) -> bool:
    identity = (left.st_dev, left.st_ino, left.st_mode, left.st_size) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
    )
    if not identity or not include_times:
        return identity
    return (left.st_mtime_ns, left.st_ctime_ns) == (right.st_mtime_ns, right.st_ctime_ns)


def _copy_archive_bounded(source: _BinaryReader, sink: BinaryIO, limit: int) -> int:
    written = 0
    while True:
        chunk = source.read(min(1024 * 1024, limit - written + 1))
        if not chunk:
            return written
        written += len(chunk)
        if written > limit:
            _reject(f"archive exceeds {limit} bytes")
        sink.write(chunk)


def inspect_archive(archive_path: Path, policy: UploadPolicy) -> ArchiveReport:
    """Validate an archive against ``policy`` without writing to disk."""

    with _open_archive_snapshot(archive_path, policy.max_archive_bytes) as archive:
        plan = _safe_plan(archive, policy)
    return ArchiveReport(
        entry_count=plan.entry_count,
        total_uncompressed=plan.total_uncompressed,
        engine_hint=plan.engine_hint,
        suggested_name=plan.common_top,
        selected_entry=plan.selected_entry,
    )


def _validate_job_name(job_name: str) -> None:
    """Reject a run-dir name that is not a single safe path component.

    ``extract_archive`` joins ``dest_root / job_name``; a name with a separator,
    ``..``, or absolute prefix would escape the trusted root. Today the caller
    passes ``safe_name`` output, but this makes the containment invariant local to
    the extraction layer rather than an implicit cross-module contract.
    """

    if (
        job_name in ("", ".", "..")
        or "/" in job_name
        or "\\" in job_name
        or PurePosixPath(job_name).is_absolute()
        or any(ord(character) < 32 or ord(character) == 127 for character in job_name)
    ):
        _reject(f"unsafe run-dir name: {job_name!r}")
    try:
        name_size = len(os.fsencode(job_name))
    except UnicodeError:
        _reject(f"unsafe run-dir name: {job_name!r}")
    if name_size > _MAX_PATH_COMPONENT_BYTES:
        _reject(f"run-dir name is too long: {job_name!r}")
    if job_name.lower() in _FORBIDDEN_RUNTIME_BASENAMES:
        _reject(f"run-dir name is reserved for runtime state: {job_name!r}")


def validate_run_dir_name(job_name: str) -> None:
    """Validate one proposed published run-directory component."""

    _validate_job_name(job_name)


def _fresh_dir(dest_root: Path, job_name: str) -> Path:
    """Atomically claim and return a destination owned by this extraction."""

    for suffix in range(1, 1000):
        name = job_name if suffix == 1 else f"{job_name}-{suffix}"
        candidate = dest_root / name
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    _reject("too many run-dirs with the same name")
    return dest_root / job_name  # unreachable


def _relative_within(dest: Path, rel: str) -> Path:
    target = (dest / rel).resolve()
    root = dest.resolve()
    if target != root and root not in target.parents:
        _reject(f"path escapes the run-dir: {rel!r}")
    return target


def extract_archive(
    archive_path: Path,
    dest_root: Path,
    job_name: str,
    policy: UploadPolicy,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> Path:
    """Extract a validated archive into ``dest_root/<job_name>`` and return it.

    Re-validates every member (defence in depth against a swapped archive), then
    writes only regular files, each confirmed to resolve inside the run-dir.
    """

    _validate_job_name(job_name)
    temp_dir: Path | None = None
    reservation: Path | None = None
    published = False
    try:
        with _open_archive_snapshot(
            archive_path,
            policy.max_archive_bytes,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        ) as archive:
            plan = _safe_plan(archive, policy)
            dest_root = dest_root.expanduser().resolve()
            dest_root.mkdir(parents=True, exist_ok=True)
            temp_dir = Path(tempfile.mkdtemp(prefix=".upload-extract-", dir=dest_root))
            strip = f"{plan.common_top}/" if plan.common_top else ""
            wanted = {member.name: member.size for member in plan.members}
            if plan.format == "zip":
                _extract_zip(archive, temp_dir, strip, wanted, policy.max_file_bytes)
            else:
                _extract_tar(
                    archive,
                    temp_dir,
                    strip,
                    wanted,
                    policy.max_file_bytes,
                    policy.max_total_uncompressed_bytes,
                )

        # Claim the public name only after extraction has completed. Replacing
        # our own empty reservation atomically makes a partial run-dir invisible
        # and retains the race-free suffix allocation of _fresh_dir().
        reservation = _fresh_dir(dest_root, job_name)
        if reservation.resolve().parent != dest_root:
            _reject(f"run-dir escapes the runs root: {job_name!r}")
        # mkdtemp intentionally creates 0700. Preserve the mode a normal
        # destination mkdir under the operator's umask would have received.
        temp_dir.chmod(stat.S_IMODE(reservation.stat().st_mode))
        _fsync_tree(temp_dir)
        os.replace(temp_dir, reservation)
        published = True
        _fsync_directory(dest_root)
        return reservation
    except UploadRejected:
        raise
    except Exception as exc:  # noqa: BLE001 - archive corruption or I/O failure
        raise UploadRejected(f"extraction failed: {type(exc).__name__}") from exc
    finally:
        if not published:
            if temp_dir is not None:
                _rmtree_quiet(temp_dir)
            if reservation is not None:
                _rmdir_quiet(reservation)


def _target_for(
    job_dir: Path,
    raw_name: str,
    strip: str,
    wanted: Mapping[str, int],
) -> tuple[Path, int, str] | None:
    rel = _safe_relpath(raw_name)
    if rel is None:
        return None
    expected_size = wanted.get(rel)
    if expected_size is None:
        return None
    stripped = rel[len(strip) :] if strip and rel.startswith(strip) else rel
    if not stripped:
        return None
    return _relative_within(job_dir, stripped), expected_size, rel


def _extract_zip(
    archive: BinaryIO,
    job_dir: Path,
    strip: str,
    wanted: Mapping[str, int],
    max_file_bytes: int,
) -> None:
    archive.seek(0)
    extracted_names: set[str] = set()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target_info = _target_for(job_dir, info.filename, strip, wanted)
            if target_info is None:
                continue
            target, expected_size, rel = target_info
            if int(info.file_size) != expected_size:
                _reject(f"archive member size changed after validation: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, target.open("wb") as sink:
                copied = _copy_bounded(source, sink, min(max_file_bytes, expected_size))
            if copied != expected_size:
                _reject(f"archive member was truncated during extraction: {info.filename}")
            extracted_names.add(rel)
    missing = set(wanted).difference(extracted_names)
    if missing:
        _reject(f"validated archive members disappeared during extraction: {sorted(missing)!r}")


def _extract_tar(
    archive: BinaryIO,
    job_dir: Path,
    strip: str,
    wanted: Mapping[str, int],
    max_file_bytes: int,
    max_stream_bytes: int,
) -> None:
    extracted_names: set[str] = set()
    with _open_tar(archive, max_stream_bytes) as tf:
        for member in tf:
            if not member.isfile():
                continue
            target_info = _target_for(job_dir, member.name, strip, wanted)
            if target_info is None:
                continue
            target, expected_size, rel = target_info
            if int(member.size) != expected_size:
                _reject(f"archive member size changed after validation: {member.name}")
            extracted = tf.extractfile(member)
            if extracted is None:
                _reject(f"archive member could not be read during extraction: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with extracted as source, target.open("wb") as sink:
                copied = _copy_bounded(source, sink, min(max_file_bytes, expected_size))
            if copied != expected_size:
                _reject(f"archive member was truncated during extraction: {member.name}")
            extracted_names.add(rel)
    missing = set(wanted).difference(extracted_names)
    if missing:
        _reject(f"validated archive members disappeared during extraction: {sorted(missing)!r}")


def _copy_bounded(source: _BinaryReader, sink: BinaryIO, limit: int) -> int:
    # The zip/tar reader already self-limits to the validated member size, but a
    # hard stop here keeps that guarantee local: a future raw-decompressor caller
    # cannot turn this into an unbounded write.
    written = 0
    while True:
        chunk = source.read(min(1024 * 1024, limit - written + 1))
        if not chunk:
            return written
        written += len(chunk)
        if written > limit:
            _reject("a file exceeded its allowed size during extraction")
        sink.write(chunk)


def _fsync_tree(root: Path) -> None:
    """Durably flush extracted regular files and every containing directory."""

    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    for current, _directories, filenames in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for filename in filenames:
            descriptor = os.open(current_path / filename, file_flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    _reject(f"extracted path is not a regular file: {filename!r}")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _fsync_directory(current_path, strict=True)


def _fsync_directory(path: Path, *, strict: bool = False) -> None:
    """Best-effort durability for the atomic publication rename."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        if strict:
            raise
        return
    try:
        os.fsync(descriptor)
    except OSError:
        if strict:
            raise
    finally:
        os.close(descriptor)


def _rmtree_quiet(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _rmdir_quiet(path: Path) -> None:
    """Remove only an empty reservation; never recurse into an alien directory."""

    try:
        path.rmdir()
    except OSError:
        pass


__all__ = [
    "ArchiveReport",
    "UploadRejected",
    "extract_archive",
    "inspect_archive",
    "validate_run_dir_name",
]
