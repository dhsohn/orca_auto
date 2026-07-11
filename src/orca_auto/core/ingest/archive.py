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

import posixpath
import tarfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .policy import UploadPolicy

_WORKFLOW_MANIFEST_NAMES = frozenset({"flow.yaml", "flow.yml", "workflow.json"})

_GZIP_MAGIC = b"\x1f\x8b"
_BZIP2_MAGIC = b"BZh"
_XZ_MAGIC = b"\xfd7zXZ\x00"


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
    members: tuple[_Member, ...]
    common_top: str
    entry_count: int
    total_uncompressed: int
    engine_hint: str


@dataclass(frozen=True)
class ArchiveReport:
    """Validated summary of an archive, safe to show before extraction."""

    entry_count: int
    total_uncompressed: int
    engine_hint: str
    suggested_name: str


def _reject(reason: str) -> None:
    raise UploadRejected(reason)


def _safe_relpath(raw_name: str) -> str | None:
    """Normalize an archive member name to a safe relative POSIX path.

    Returns ``None`` for directory-only entries.  Raises on any name that is
    absolute, escapes the root via ``..``, or carries a Windows drive/backslash.
    """

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
    return posixpath.join(*parts)


def _detect_format(archive_path: Path) -> str:
    if zipfile.is_zipfile(archive_path):
        return "zip"
    if tarfile.is_tarfile(archive_path):
        return "tar"
    _reject("archive is neither a valid .zip nor a .tar/.tar.gz")
    return ""  # unreachable


def _open_tar(archive_path: Path) -> tarfile.TarFile:
    """Open a tar accepting only gzip-compressed or uncompressed streams.

    ``mode="r:*"`` would transparently decompress xz and bzip2, whose much higher
    compression ratios amplify the cost of a decompression bomb. The feature is
    advertised as ``.tar.gz``/``.tar`` only, so those are the only modes accepted.
    """

    with archive_path.open("rb") as handle:
        magic = handle.read(6)
    if magic[:2] == _GZIP_MAGIC:
        return tarfile.open(archive_path, mode="r:gz")
    if magic[:3] == _BZIP2_MAGIC or magic[:6] == _XZ_MAGIC:
        _reject("only gzip-compressed or uncompressed tar archives are accepted")
    return tarfile.open(archive_path, mode="r:")


def _iter_zip(archive_path: Path) -> Iterator[tuple[str, bool, int]]:
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            is_dir = info.is_dir()
            # Zip has no symlink type flag we rely on; the extraction path guard
            # plus the extension allowlist bound what a regular entry can be.
            yield info.filename, is_dir, int(info.file_size)


def _iter_tar(archive_path: Path) -> Iterator[tuple[str, bool, int]]:
    with _open_tar(archive_path) as tf:
        for member in tf:
            if member.issym() or member.islnk():
                _reject(f"symlink or hardlink in archive: {member.name!r}")
            if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                _reject(f"special file in archive: {member.name!r}")
            if not (member.isfile() or member.isdir()):
                _reject(f"unsupported archive member type: {member.name!r}")
            yield member.name, member.isdir(), int(member.size)


def _validate_members(
    archive_path: Path,
    fmt: str,
    policy: UploadPolicy,
) -> list[_Member]:
    iterator = _iter_zip(archive_path) if fmt == "zip" else _iter_tar(archive_path)
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
        suffix = PurePosixPath(rel).suffix
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


def _engine_hint(members: tuple[_Member, ...], common_top: str) -> str:
    for member in members:
        rel = member.name[len(common_top) + 1 :] if common_top else member.name
        base = PurePosixPath(rel).name.lower()
        if base in _WORKFLOW_MANIFEST_NAMES:
            return "workflow"
    if any(member.name.lower().endswith(".inp") for member in members):
        return "orca"
    return "unknown"


def _build_plan(archive_path: Path, policy: UploadPolicy) -> _ArchivePlan:
    if not archive_path.is_file():
        _reject("uploaded archive is missing")
    if archive_path.stat().st_size > policy.max_archive_bytes:
        _reject(f"archive exceeds {policy.max_archive_bytes} bytes")
    fmt = _detect_format(archive_path)
    members = tuple(_validate_members(archive_path, fmt, policy))
    common_top = _common_top(members)
    total = sum(member.size for member in members)
    return _ArchivePlan(
        members=members,
        common_top=common_top,
        entry_count=len(members),
        total_uncompressed=total,
        engine_hint=_engine_hint(members, common_top),
    )


def _safe_plan(archive_path: Path, policy: UploadPolicy) -> _ArchivePlan:
    """``_build_plan`` with library parse errors normalized to ``UploadRejected``.

    A truncated or corrupt archive that still passes ``is_zipfile``/``is_tarfile``
    can raise ``BadZipFile``/``tarfile.ReadError``/``zlib.error`` while being read;
    callers should only ever have to catch ``UploadRejected``.
    """

    try:
        return _build_plan(archive_path, policy)
    except UploadRejected:
        raise
    except Exception as exc:  # noqa: BLE001 - corrupt/malformed archive input
        raise UploadRejected(f"archive could not be read: {type(exc).__name__}") from exc


def inspect_archive(archive_path: Path, policy: UploadPolicy) -> ArchiveReport:
    """Validate an archive against ``policy`` without writing to disk."""

    plan = _safe_plan(archive_path, policy)
    return ArchiveReport(
        entry_count=plan.entry_count,
        total_uncompressed=plan.total_uncompressed,
        engine_hint=plan.engine_hint,
        suggested_name=plan.common_top,
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
    ):
        _reject(f"unsafe run-dir name: {job_name!r}")


def _fresh_dir(dest_root: Path, job_name: str) -> Path:
    candidate = dest_root / job_name
    if not candidate.exists():
        return candidate
    for suffix in range(2, 1000):
        candidate = dest_root / f"{job_name}-{suffix}"
        if not candidate.exists():
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
) -> Path:
    """Extract a validated archive into ``dest_root/<job_name>`` and return it.

    Re-validates every member (defence in depth against a swapped archive), then
    writes only regular files, each confirmed to resolve inside the run-dir.
    """

    _validate_job_name(job_name)
    plan = _safe_plan(archive_path, policy)
    dest_root = dest_root.expanduser().resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    job_dir = _fresh_dir(dest_root, job_name)
    # Belt-and-suspenders: the destination itself must resolve inside dest_root.
    if job_dir.resolve().parent != dest_root:
        _reject(f"run-dir escapes the runs root: {job_name!r}")
    strip = f"{plan.common_top}/" if plan.common_top else ""
    fmt = _detect_format(archive_path)
    wanted = {member.name for member in plan.members}
    try:
        job_dir.mkdir(parents=True)
        if fmt == "zip":
            _extract_zip(archive_path, job_dir, strip, wanted, policy.max_file_bytes)
        else:
            _extract_tar(archive_path, job_dir, strip, wanted, policy.max_file_bytes)
    except UploadRejected:
        _rmtree_quiet(job_dir)
        raise
    except Exception as exc:  # noqa: BLE001 - archive corruption or I/O failure
        _rmtree_quiet(job_dir)
        raise UploadRejected(f"extraction failed: {type(exc).__name__}") from exc
    return job_dir


def _target_for(job_dir: Path, raw_name: str, strip: str, wanted: set[str]) -> Path | None:
    rel = _safe_relpath(raw_name)
    if rel is None or rel not in wanted:
        return None
    stripped = rel[len(strip) :] if strip and rel.startswith(strip) else rel
    if not stripped:
        return None
    return _relative_within(job_dir, stripped)


def _extract_zip(
    archive_path: Path, job_dir: Path, strip: str, wanted: set[str], max_file_bytes: int
) -> None:
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = _target_for(job_dir, info.filename, strip, wanted)
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, target.open("wb") as sink:
                _copy_bounded(source, sink, max_file_bytes)


def _extract_tar(
    archive_path: Path, job_dir: Path, strip: str, wanted: set[str], max_file_bytes: int
) -> None:
    with _open_tar(archive_path) as tf:
        for member in tf:
            if not member.isfile():
                continue
            target = _target_for(job_dir, member.name, strip, wanted)
            if target is None:
                continue
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with extracted as source, target.open("wb") as sink:
                _copy_bounded(source, sink, max_file_bytes)


def _copy_bounded(source: object, sink: object, limit: int) -> None:
    # The zip/tar reader already self-limits to the validated member size, but a
    # hard stop here keeps that guarantee local: a future raw-decompressor caller
    # cannot turn this into an unbounded write.
    read = source.read  # type: ignore[attr-defined]
    write = sink.write  # type: ignore[attr-defined]
    written = 0
    while True:
        chunk = read(1024 * 1024)
        if not chunk:
            break
        written += len(chunk)
        if written > limit:
            _reject("a file exceeded its allowed size during extraction")
        write(chunk)


def _rmtree_quiet(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "ArchiveReport",
    "UploadRejected",
    "extract_archive",
    "inspect_archive",
]
