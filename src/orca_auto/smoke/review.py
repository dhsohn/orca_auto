"""Generate a bounded, offline review packet for a smoke-test batch.

The runtime trees remain the artifact source of truth.  This module writes a short,
Windows-safe review projection plus a Markdown summary and an HTML index.  Projection
files are byte copies with explicit source paths and digests; artifact discovery is
rooted at directory file descriptors and never follows symlinks.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from orca_auto.smoke._fsatomic import atomic_write_bytes_at

_MAX_FIELD_CHARS = 512
_MAX_PREVIEW_BYTES = 32 * 1024
_MAX_TOTAL_PREVIEW_BYTES = 4 * 1024 * 1024
_MAX_DIGEST_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_DIGEST_BYTES = 64 * 1024 * 1024
_MAX_ARTIFACTS = 5_000
_MAX_DISCOVERY_ENTRIES = 5_000
_MAX_DIRECTORY_ENTRIES = 5_000
_MAX_DIRECTORY_DEPTH = 32
_MAX_RELATIVE_PATH_BYTES = 4_096
_MAX_PATH_COMPONENT_BYTES = 255
_READ_CHUNK_BYTES = 1024 * 1024
_MAX_PROJECTION_FILE_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_PROJECTION_BYTES = 128 * 1024 * 1024
_MAX_PROJECTION_FILES = 10_000
_MAX_HTML_LINK_SCAN_BYTES = 2 * 1024 * 1024
_MAX_OPEN_COMPONENT_CHARS = 64
_MAX_PORTABLE_FILENAME_CHARS = 40
_MAX_CASE_SLUG_CHARS = 16
_MAX_OPEN_RELATIVE_CHARS = 128
# Version 3: the hidden_harness_alias disposition, the alias_target_* columns,
# the hidden_harness_alias_count aggregate, and H-numbered artifact ids are gone.
_PROJECTION_SCHEMA_VERSION = 3

_DISPOSITION_REVIEW = "review"
_DISPOSITION_BLOCKED = "blocked"

_WINDOWS_RESERVED_NAMES = {
    "aux",
    "clock$",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "con",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
    "nul",
    "prn",
}

_REQUIRED_CASE_FIELDS = (
    "case_id",
    "surface",
    "scenario",
    "expected_terminal",
    "observed_terminal",
    "verdict",
)

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?im)(?:^|[,{\s])['\"]?"
    r"(?:api[_-]?key|access[_-]?key|secret(?:[_-]?key)?|token|password|passwd|"
    r"authorization|bot[_-]?token|private[_-]?key)"
    r"['\"]?\s*[:=]\s*['\"]?[^\s,'\"}]{4,}"
)
_SECRET_TOKEN_RES = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)

_HTML_SUFFIXES = {".html", ".htm"}
_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_JSON_SUFFIXES = {".json", ".jsonl"}
_TABLE_SUFFIXES = {".csv", ".tsv"}
_LOG_SUFFIXES = {".log", ".out", ".err", ".stdout", ".stderr"}
_ENGINE_SUFFIXES = {
    ".inp",
    ".xyz",
    ".trj",
    ".engrad",
    ".hess",
    ".property.txt",
}
_ENGINE_NAMES = {"mdrestart", "xtbmdok", "xtbtopo.mol", "orca.out", "xtb.out"}
_TEXT_SUFFIXES = {".txt", ".yaml", ".yml", ".toml", ".ini", ".cfg"}

_MISSING = object()


class ReviewPacketError(ValueError):
    """Raised when the batch/output root itself cannot be handled safely."""


@dataclass(frozen=True)
class ReviewPacketResult:
    """Paths and artifact count produced by :func:`generate_review_packet`."""

    summary_path: Path
    index_path: Path
    artifact_manifest_path: Path
    artifact_count: int
    openable_count: int


@dataclass(frozen=True)
class _SourceIdentity:
    device: int
    inode: int
    size_bytes: int
    modified_ns: int


@dataclass(frozen=True)
class _DirectoryIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class _ProjectionDependency:
    source_path: str
    open_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class _Artifact:
    runtime_path: str
    batch_path: str
    kind: str
    size_bytes: int | None
    sha256: str | None
    preview: str | None
    preview_note: str
    linkable: bool
    source_identity: _SourceIdentity | None
    disposition: str = _DISPOSITION_REVIEW
    issue: str | None = None
    artifact_id: str = ""
    open_path: str | None = None
    review_sha256: str | None = None
    dependencies: tuple[_ProjectionDependency, ...] = ()


@dataclass
class _CaseReview:
    fields: dict[str, str]
    case_path: str
    runtime_path: str
    open_path: str = ""
    artifacts: list[_Artifact] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass
class _Budget:
    entries_remaining: int = field(default_factory=lambda: _MAX_DISCOVERY_ENTRIES)
    preview_bytes_remaining: int = _MAX_TOTAL_PREVIEW_BYTES
    digest_bytes_remaining: int = _MAX_TOTAL_DIGEST_BYTES


@dataclass
class _ProjectionBudget:
    files_remaining: int = field(default_factory=lambda: _MAX_PROJECTION_FILES)
    bytes_remaining: int = field(default_factory=lambda: _MAX_TOTAL_PROJECTION_BYTES)


def generate_review_packet(
    batch_dir: Path,
    batch_manifest: Mapping[str, object],
    case_manifests: Sequence[Mapping[str, object]] | None = None,
    *,
    batch_root_fd: int | None = None,
    advertised_batch_dir: Path | None = None,
    expected_batch_identity: tuple[int, int] | None = None,
) -> ReviewPacketResult:
    """Write ``summary.md`` and ``review/index.html`` for one smoke batch.

    ``case_dir`` is interpreted relative to ``batch_dir`` and ``runtime_dir`` is
    interpreted relative to that case (default: ``runtime``).  Unsafe or missing
    case paths are rendered as review issues and are never traversed.  A symlink
    encountered below a valid runtime is not followed and remains a visible
    blocked artifact.
    """

    if batch_root_fd is None:
        root, root_fd, root_identity = _open_batch_root(batch_dir)
        display_root = root
    else:
        try:
            root_fd = os.dup(batch_root_fd)
        except OSError as exc:
            raise ReviewPacketError("pinned batch directory is unavailable") from exc
        root_identity = _directory_identity(root_fd)
        if (
            expected_batch_identity is not None
            and (
                root_identity.device,
                root_identity.inode,
            )
            != expected_batch_identity
        ):
            os.close(root_fd)
            raise ReviewPacketError("pinned batch directory identity changed")
        root = Path("/proc") / "self" / "fd" / str(root_fd)
        display_root = Path(advertised_batch_dir or batch_dir).expanduser()
    _assert_batch_namespace_identity(display_root, root_identity)
    review_fd: int | None = None
    artifact_manifest_relative: Path | None = None
    openable_count = 0
    try:
        cases = _normalise_case_manifests(batch_manifest, case_manifests)
        _assert_batch_namespace_identity(display_root, root_identity)
        budget = _Budget()
        reviews = [_build_case_review(root_fd, case, budget) for case in cases]
        _assert_batch_namespace_identity(display_root, root_identity)
        artifact_count = sum(len(case.artifacts) for case in reviews)

        review_fd = _open_or_create_review_directory(root_fd)
        publication = _materialize_review_projection(
            root_fd,
            review_fd,
            reviews,
            batch_manifest=batch_manifest,
        )
        generation = publication.generation
        openable_count = publication.openable_count
        blocked_count = _disposition_count(reviews, _DISPOSITION_BLOCKED)
        artifact_manifest_relative = Path("review") / generation / "artifacts.json"
        summary = _render_summary(
            batch_manifest,
            reviews,
            artifact_count,
            openable_count,
            blocked_count,
        )
        index = _render_index(
            batch_manifest,
            reviews,
            artifact_count,
            openable_count,
            blocked_count,
        )

        # The packet is regenerable from the retained runtime (batch.json is the
        # only terminal authority), so a failed publication is reported as a
        # failure instead of restoring the previous surfaces.
        _atomic_write_text(root_fd, "summary.md", summary)
        review_identity = _directory_identity(review_fd)
        _assert_named_directory_identity(root_fd, "review", review_identity)
        _assert_batch_namespace_identity(display_root, root_identity)
        # index.html is the review packet commit marker and is published last.
        _atomic_write_text(review_fd, "index.html", index)
        _assert_named_directory_identity(root_fd, "review", review_identity)
        _assert_batch_namespace_identity(display_root, root_identity)
    finally:
        if review_fd is not None:
            os.close(review_fd)
        os.close(root_fd)

    if artifact_manifest_relative is None:
        raise ReviewPacketError("review artifact manifest was not published")
    _assert_batch_namespace_identity(display_root, root_identity)
    return ReviewPacketResult(
        summary_path=display_root / "summary.md",
        index_path=display_root / "review" / "index.html",
        artifact_manifest_path=display_root / artifact_manifest_relative,
        artifact_count=artifact_count,
        openable_count=openable_count,
    )


def _normalise_case_manifests(
    batch_manifest: Mapping[str, object],
    case_manifests: Sequence[Mapping[str, object]] | None,
) -> list[Mapping[str, object]]:
    raw_cases: object = (
        batch_manifest.get("cases", ()) if case_manifests is None else case_manifests
    )
    if isinstance(raw_cases, (str, bytes, bytearray)) or not isinstance(raw_cases, Sequence):
        raise ReviewPacketError("case manifests must be a sequence of mappings")
    if len(raw_cases) > _MAX_ARTIFACTS:
        raise ReviewPacketError("case manifest count exceeds the review packet limit")

    cases: list[Mapping[str, object]] = []
    for item in raw_cases:
        if not isinstance(item, Mapping):
            raise ReviewPacketError("each case manifest must be a mapping")
        cases.append(item)
    return cases


def _open_batch_root(batch_dir: Path) -> tuple[Path, int, _DirectoryIdentity]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ReviewPacketError("safe no-follow directory access is unavailable on this platform")
    candidate = Path(batch_dir).expanduser()
    try:
        if candidate.is_symlink():
            raise ReviewPacketError("batch directory must not be a symlink")
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise ReviewPacketError("batch directory is unavailable") from exc
    if not root.is_dir():
        raise ReviewPacketError("batch path must be a directory")

    try:
        root_fd = os.open(root, _directory_open_flags())
    except OSError as exc:
        raise ReviewPacketError("batch directory could not be opened safely") from exc
    if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
        os.close(root_fd)
        raise ReviewPacketError("batch path must be a directory")
    return root, root_fd, _directory_identity(root_fd)


def _assert_batch_namespace_identity(
    root: Path,
    expected: _DirectoryIdentity,
) -> None:
    candidate = Path(root).expanduser()
    try:
        if candidate.is_symlink():
            raise ReviewPacketError("batch directory namespace identity changed")
        descriptor = os.open(candidate, _directory_open_flags())
    except OSError as exc:
        raise ReviewPacketError("batch directory namespace identity changed") from exc
    try:
        if _directory_identity(descriptor) != expected:
            raise ReviewPacketError("batch directory namespace identity changed")
    finally:
        os.close(descriptor)


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _file_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _open_or_create_review_directory(root_fd: int) -> int:
    try:
        os.mkdir("review", mode=0o755, dir_fd=root_fd)
    except FileExistsError:
        pass
    try:
        review_fd = os.open("review", _directory_open_flags(), dir_fd=root_fd)
    except OSError as exc:
        raise ReviewPacketError("review output directory is unavailable or unsafe") from exc
    if not stat.S_ISDIR(os.fstat(review_fd).st_mode):
        os.close(review_fd)
        raise ReviewPacketError("review output path must be a directory")
    return review_fd


def _atomic_write_text(directory_fd: int, name: str, content: str) -> None:
    atomic_write_bytes_at(
        directory_fd,
        name,
        content.encode("utf-8"),
        mode=0o644,
        error=ReviewPacketError,
    )


class _ProjectionUnavailable(ValueError):
    """Raised when one artifact cannot be included in the bounded projection."""


@dataclass(frozen=True)
class _ProjectionPlanEntry:
    artifact: _Artifact
    destination_parts: tuple[str, ...]


@dataclass(frozen=True)
class _ProjectionPublication:
    generation: str
    openable_count: int
    identity: _DirectoryIdentity
    manifest_text: str


class _HtmlReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.has_base = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered_tag = tag.lower()
        if lowered_tag == "base":
            self.has_base = True
        for key, value in attrs:
            if key.lower() in {"href", "src"} and value:
                self.references.append(value)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)


def _open_or_create_directory(parent_fd: int, name: str) -> int:
    if not _windows_safe_component(name):
        raise ReviewPacketError("review projection contains an unsafe directory name")
    created = False
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    try:
        directory_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise ReviewPacketError("review projection directory is unavailable or unsafe") from exc
    if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
        os.close(directory_fd)
        raise ReviewPacketError("review projection path must be a directory")
    if created:
        os.fsync(parent_fd)
    return directory_fd


def _directory_identity(directory_fd: int) -> _DirectoryIdentity:
    details = os.fstat(directory_fd)
    if not stat.S_ISDIR(details.st_mode):
        raise ReviewPacketError("review projection identity is not a directory")
    return _DirectoryIdentity(device=details.st_dev, inode=details.st_ino)


def _open_named_directory_identity(
    parent_fd: int,
    name: str,
    expected: _DirectoryIdentity,
) -> int:
    try:
        directory_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise ReviewPacketError("review projection directory identity changed") from exc
    actual = _directory_identity(directory_fd)
    if actual != expected:
        os.close(directory_fd)
        raise ReviewPacketError("review projection directory identity changed")
    return directory_fd


def _assert_named_directory_identity(
    parent_fd: int,
    name: str,
    expected: _DirectoryIdentity,
) -> None:
    directory_fd = _open_named_directory_identity(parent_fd, name, expected)
    os.close(directory_fd)


def _create_projection_staging(review_fd: int) -> tuple[str, str, int]:
    for _attempt in range(16):
        generation = f"g-{secrets.token_hex(4)}"
        staging = f".{generation}.tmp"
        try:
            os.mkdir(staging, mode=0o700, dir_fd=review_fd)
        except FileExistsError:
            continue
        try:
            staging_fd = os.open(staging, _directory_open_flags(), dir_fd=review_fd)
        except OSError as exc:
            raise ReviewPacketError("review projection staging directory is unsafe") from exc
        os.fsync(review_fd)
        return generation, staging, staging_fd
    raise ReviewPacketError("could not allocate a unique review projection generation")


def _create_artifact_staging(case_fd: int, artifact_component: str) -> tuple[str, int]:
    for _attempt in range(16):
        staging = f".{artifact_component}-{secrets.token_hex(4)}.tmp"
        try:
            os.mkdir(staging, mode=0o700, dir_fd=case_fd)
        except FileExistsError:
            continue
        try:
            staging_fd = os.open(staging, _directory_open_flags(), dir_fd=case_fd)
        except OSError as exc:
            raise ReviewPacketError("artifact projection staging directory is unsafe") from exc
        os.fsync(case_fd)
        return staging, staging_fd
    raise ReviewPacketError("could not allocate artifact projection staging")


def _remove_projection_tree(parent_fd: int, name: str) -> None:
    """Remove one owned projection subtree without following links."""

    try:
        details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return

    directory_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    try:
        with os.scandir(directory_fd) as iterator:
            children = [entry.name for entry in iterator]
        for child in children:
            _remove_projection_tree(directory_fd, child)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _portable_slug(value: str, *, fallback: str, limit: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        slug = fallback
    return slug[:limit].rstrip("-") or fallback


def _portable_filename(name: str) -> str:
    suffixes = PurePosixPath(name).suffixes
    suffix = "".join(suffixes[-2:]) if suffixes else ""
    suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix.lower())
    if len(suffix) > 16:
        suffix = suffix[-16:]
    stem = name[: -len("".join(suffixes))] if suffixes else name
    safe_stem = _portable_slug(
        stem,
        fallback="artifact",
        limit=max(1, _MAX_PORTABLE_FILENAME_CHARS - len(suffix)),
    )
    candidate = f"{safe_stem}{suffix}"
    if candidate.split(".", 1)[0].lower() in _WINDOWS_RESERVED_NAMES:
        candidate = f"artifact-{candidate}"
    return candidate[:_MAX_PORTABLE_FILENAME_CHARS].rstrip(". ") or "artifact"


def _windows_safe_component(component: str) -> bool:
    if not component or len(component) > _MAX_OPEN_COMPONENT_CHARS:
        return False
    if component[-1] in {" ", "."}:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._-]+", component):
        return False
    return component.split(".", 1)[0].lower() not in _WINDOWS_RESERVED_NAMES


def _open_artifact_source(root_fd: int, artifact: _Artifact) -> int:
    identity = artifact.source_identity
    if identity is None:
        raise _ProjectionUnavailable("source identity is unavailable")
    parts = _relative_parts(artifact.batch_path, "artifact source path")
    parent_fd = _open_relative_directory(root_fd, parts[:-1])
    try:
        source_fd = os.open(parts[-1], _file_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise _ProjectionUnavailable("source file could not be reopened safely") from exc
    finally:
        os.close(parent_fd)
    opened = os.fstat(source_fd)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_dev != identity.device
        or opened.st_ino != identity.inode
        or opened.st_size != identity.size_bytes
        or opened.st_mtime_ns != identity.modified_ns
    ):
        os.close(source_fd)
        raise _ProjectionUnavailable("source file changed before review projection")
    return source_fd


def _assert_source_unchanged(source_fd: int, identity: _SourceIdentity) -> None:
    after = os.fstat(source_fd)
    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or after.st_dev != identity.device
        or after.st_ino != identity.inode
        or after.st_size != identity.size_bytes
        or after.st_mtime_ns != identity.modified_ns
    ):
        raise _ProjectionUnavailable("source file changed while review projection was copied")


def _read_artifact_bytes(root_fd: int, artifact: _Artifact, *, limit: int) -> bytes:
    if artifact.size_bytes is None or artifact.size_bytes > limit:
        raise _ProjectionUnavailable("HTML report exceeds the local-link scan limit")
    source_fd = _open_artifact_source(root_fd, artifact)
    try:
        payload = _read_prefix(source_fd, artifact.size_bytes + 1)
        if len(payload) != artifact.size_bytes:
            raise _ProjectionUnavailable("HTML report changed while local links were scanned")
        identity = artifact.source_identity
        assert identity is not None
        _assert_source_unchanged(source_fd, identity)
        return payload
    finally:
        os.close(source_fd)


def _local_reference_parts(reference: str) -> tuple[str, ...] | None:
    try:
        parsed = urlsplit(reference)
    except ValueError as exc:
        raise _ProjectionUnavailable("HTML report contains a malformed reference") from exc
    if parsed.scheme or parsed.netloc:
        return None
    if not parsed.path:
        return None
    if parsed.path.startswith("/"):
        raise _ProjectionUnavailable("HTML report contains an absolute local reference")
    decoded = unquote(parsed.path)
    if "\\" in decoded or any(ord(char) < 32 for char in decoded):
        raise _ProjectionUnavailable("HTML report contains an unsafe local reference")
    parts = tuple(decoded.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _ProjectionUnavailable("HTML report contains an unconfined local reference")
    if not all(_windows_safe_component(part) for part in parts):
        raise _ProjectionUnavailable("HTML report local reference is not Windows-safe")
    return parts


def _html_projection_plan(
    root_fd: int,
    main_artifact: _Artifact,
    main_name: str,
    artifacts_by_path: Mapping[str, _Artifact],
) -> list[_ProjectionPlanEntry]:
    plan = [_ProjectionPlanEntry(main_artifact, (main_name,))]
    destinations: dict[tuple[str, ...], str] = {(main_name,): main_artifact.batch_path}
    queue: list[_ProjectionPlanEntry] = list(plan)
    scanned: set[tuple[str, tuple[str, ...]]] = set()
    while queue:
        current = queue.pop(0)
        scan_key = (current.artifact.batch_path, current.destination_parts)
        if scan_key in scanned or current.artifact.kind != "HTML report":
            continue
        scanned.add(scan_key)
        payload = _read_artifact_bytes(
            root_fd,
            current.artifact,
            limit=_MAX_HTML_LINK_SCAN_BYTES,
        )
        if (
            current.artifact.sha256 is None
            or hashlib.sha256(payload).hexdigest() != current.artifact.sha256
        ):
            raise _ProjectionUnavailable("HTML report changed while local links were scanned")
        parser = _HtmlReferenceParser()
        parser.feed(payload.decode("utf-8", errors="replace"))
        parser.close()
        if parser.has_base:
            raise _ProjectionUnavailable("HTML report contains a base element")
        source_parent = PurePosixPath(current.artifact.batch_path).parent
        destination_parent = current.destination_parts[:-1]
        for reference in parser.references:
            relative_parts = _local_reference_parts(reference)
            if relative_parts is None:
                continue
            source_path = str(source_parent.joinpath(*relative_parts))
            dependency = artifacts_by_path.get(source_path)
            if dependency is None or not dependency.linkable or dependency.source_identity is None:
                raise _ProjectionUnavailable(
                    "HTML report local reference does not resolve to a safe retained artifact"
                )
            destination = (*destination_parent, *relative_parts)
            previous = destinations.get(destination)
            if previous is not None:
                if previous != dependency.batch_path:
                    raise _ProjectionUnavailable("HTML report local references collide")
                continue
            destinations[destination] = dependency.batch_path
            entry = _ProjectionPlanEntry(dependency, destination)
            plan.append(entry)
            queue.append(entry)
            if len(plan) > _MAX_PROJECTION_FILES:
                raise _ProjectionUnavailable("HTML report local-link closure is too large")
    return plan


def _projection_budget_allows(
    budget: _ProjectionBudget,
    plan: Sequence[_ProjectionPlanEntry],
) -> bool:
    sizes: list[int] = []
    for entry in plan:
        size = entry.artifact.size_bytes
        if size is None or size > _MAX_PROJECTION_FILE_BYTES or entry.artifact.sha256 is None:
            return False
        sizes.append(size)
    return len(plan) <= budget.files_remaining and sum(sizes) <= budget.bytes_remaining


def _projection_paths_fit(
    case_open_path: str,
    artifact_component: str,
    plan: Sequence[_ProjectionPlanEntry],
) -> bool:
    prefix = PurePosixPath(case_open_path, artifact_component)
    return all(
        len(PurePosixPath(prefix, *entry.destination_parts).as_posix()) <= _MAX_OPEN_RELATIVE_CHARS
        for entry in plan
    )


def _consume_projection_budget(
    budget: _ProjectionBudget,
    plan: Sequence[_ProjectionPlanEntry],
) -> None:
    budget.files_remaining -= len(plan)
    budget.bytes_remaining -= sum(int(entry.artifact.size_bytes or 0) for entry in plan)


def _open_relative_projection_directory(root_fd: int, parts: Sequence[str]) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = _open_or_create_directory(current_fd, part)
            os.close(current_fd)
            current_fd = next_fd
    except BaseException:
        os.close(current_fd)
        raise
    return current_fd


def _copy_projection_file(
    root_fd: int,
    projection_root_fd: int,
    entry: _ProjectionPlanEntry,
) -> str:
    if not entry.destination_parts or not all(
        _windows_safe_component(part) for part in entry.destination_parts
    ):
        raise ReviewPacketError("review projection destination is unsafe")
    parent_fd = _open_relative_projection_directory(
        projection_root_fd,
        entry.destination_parts[:-1],
    )
    source_fd = _open_artifact_source(root_fd, entry.artifact)
    destination_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        destination_fd = os.open(
            entry.destination_parts[-1],
            flags,
            0o600,
            dir_fd=parent_fd,
        )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source_fd, _READ_CHUNK_BYTES)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("review projection write made no progress")
                view = view[written:]
            digest.update(chunk)
            total += len(chunk)
        os.fsync(destination_fd)
        identity = entry.artifact.source_identity
        assert identity is not None
        _assert_source_unchanged(source_fd, identity)
        if total != identity.size_bytes:
            raise _ProjectionUnavailable("source size changed during review projection")
        rendered_digest = digest.hexdigest()
        if entry.artifact.sha256 is not None and rendered_digest != entry.artifact.sha256:
            raise _ProjectionUnavailable("source digest changed during review projection")
        os.fsync(parent_fd)
        return rendered_digest
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)
        os.close(parent_fd)


def _projection_issue(artifact: _Artifact, message: str) -> _Artifact:
    return replace(
        artifact,
        linkable=False,
        disposition=_DISPOSITION_BLOCKED,
        open_path=None,
        review_sha256=None,
        dependencies=(),
        issue=message,
    )


def _projection_manifest(
    batch_manifest: Mapping[str, object],
    generation: str,
    reviews: Sequence[_CaseReview],
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for case in reviews:
        for artifact in case.artifacts:
            records.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "case_id": case.fields["case_id"],
                    "source_path": artifact.batch_path,
                    "open_path": artifact.open_path,
                    "kind": artifact.kind,
                    "disposition": artifact.disposition,
                    "size_bytes": artifact.size_bytes,
                    "source_sha256": artifact.sha256,
                    "review_sha256": artifact.review_sha256,
                    "issue": artifact.issue,
                    "dependencies": [
                        {
                            "source_path": dependency.source_path,
                            "open_path": dependency.open_path,
                            "size_bytes": dependency.size_bytes,
                            "sha256": dependency.sha256,
                        }
                        for dependency in artifact.dependencies
                    ],
                }
            )
    return {
        "schema_version": _PROJECTION_SCHEMA_VERSION,
        "batch_id": _field_text(batch_manifest.get("batch_id", _MISSING)),
        "generation": generation,
        "runtime_source_of_truth": True,
        "artifact_count": len(records),
        "openable_count": sum(record["open_path"] is not None for record in records),
        "blocked_count": sum(record["disposition"] == _DISPOSITION_BLOCKED for record in records),
        "artifacts": records,
    }


def _verify_source_artifact(root_fd: int, artifact: _Artifact) -> None:
    expected_digest = artifact.sha256
    identity = artifact.source_identity
    if expected_digest is None or identity is None:
        raise ReviewPacketError("projected artifact source provenance is incomplete")
    try:
        source_fd = _open_artifact_source(root_fd, artifact)
    except _ProjectionUnavailable as exc:
        raise ReviewPacketError("projected artifact source changed before publication") from exc
    try:
        digest, size_bytes = _digest_file(source_fd)
        _assert_source_unchanged(source_fd, identity)
    except _ProjectionUnavailable as exc:
        raise ReviewPacketError("projected artifact source changed before publication") from exc
    finally:
        os.close(source_fd)
    if size_bytes != identity.size_bytes or digest != expected_digest:
        raise ReviewPacketError("projected artifact source digest changed before publication")


def _verify_projection_file(
    generation_fd: int,
    parts: tuple[str, ...],
    *,
    expected_size: int,
    expected_digest: str,
) -> None:
    if not parts or not all(_windows_safe_component(part) for part in parts):
        raise ReviewPacketError("published review projection path is unsafe")
    try:
        parent_fd = _open_relative_directory(generation_fd, parts[:-1])
        try:
            file_fd = os.open(parts[-1], _file_open_flags(), dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise ReviewPacketError("published review projection file is unavailable") from exc
    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ReviewPacketError("published review projection file is unsafe")
        digest, size_bytes = _digest_file(file_fd)
        after = os.fstat(file_fd)
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_nlink != 1
        ):
            raise ReviewPacketError("published review projection file changed during verification")
        if size_bytes != expected_size or before.st_size != expected_size:
            raise ReviewPacketError("published review projection file size is invalid")
        if digest != expected_digest:
            raise ReviewPacketError("published review projection file digest is invalid")
    finally:
        os.close(file_fd)


def _generation_open_parts(open_path: str, generation: str) -> tuple[str, ...]:
    parts = _relative_parts(open_path, "review open path")
    if len(parts) < 3 or parts[:2] != ("review", generation):
        raise ReviewPacketError("review open path does not belong to its generation")
    return parts[2:]


def _verify_review_projection(
    root_fd: int,
    review_fd: int,
    reviews: Sequence[_CaseReview],
    *,
    publication: _ProjectionPublication,
) -> None:
    generation_fd = _open_named_directory_identity(
        review_fd,
        publication.generation,
        publication.identity,
    )
    try:
        manifest_bytes = publication.manifest_text.encode("utf-8")
        _verify_projection_file(
            generation_fd,
            ("artifacts.json",),
            expected_size=len(manifest_bytes),
            expected_digest=hashlib.sha256(manifest_bytes).hexdigest(),
        )
        artifacts_by_path = {
            artifact.batch_path: artifact
            for case in reviews
            for artifact in case.artifacts
            if artifact.source_identity is not None
        }
        sources: dict[str, _Artifact] = {}
        copies: list[tuple[str, int, str]] = []
        for case in reviews:
            for artifact in case.artifacts:
                if artifact.open_path is None:
                    continue
                if artifact.review_sha256 is None or artifact.size_bytes is None:
                    raise ReviewPacketError("published review artifact provenance is incomplete")
                sources[artifact.batch_path] = artifact
                copies.append((artifact.open_path, artifact.size_bytes, artifact.review_sha256))
                for dependency in artifact.dependencies:
                    source_artifact = artifacts_by_path.get(dependency.source_path)
                    if source_artifact is None:
                        raise ReviewPacketError("review dependency source provenance is missing")
                    sources[dependency.source_path] = source_artifact
                    copies.append((dependency.open_path, dependency.size_bytes, dependency.sha256))

        for artifact in sources.values():
            _verify_source_artifact(root_fd, artifact)
        for open_path, size_bytes, digest in copies:
            _verify_projection_file(
                generation_fd,
                _generation_open_parts(open_path, publication.generation),
                expected_size=size_bytes,
                expected_digest=digest,
            )
    finally:
        os.close(generation_fd)


def _materialize_review_projection(
    root_fd: int,
    review_fd: int,
    reviews: list[_CaseReview],
    *,
    batch_manifest: Mapping[str, object],
) -> _ProjectionPublication:
    generation, staging, staging_fd = _create_projection_staging(review_fd)
    staging_identity = _directory_identity(staging_fd)
    open_fd: int | None = None
    cleanup_name = staging
    committed = False
    try:
        open_fd = _open_or_create_directory(staging_fd, "open")
        artifacts_by_path = {
            artifact.batch_path: artifact
            for case in reviews
            for artifact in case.artifacts
            if artifact.linkable and artifact.source_identity is not None
        }
        projection_budget = _ProjectionBudget()
        openable_count = 0
        for case_index, case in enumerate(reviews, start=1):
            case_component = f"c{case_index:02d}-" + _portable_slug(
                case.fields["case_id"],
                fallback="case",
                limit=_MAX_CASE_SLUG_CHARS,
            )
            case.open_path = f"review/{generation}/open/{case_component}"
            case_fd = _open_or_create_directory(open_fd, case_component)
            updated: list[_Artifact] = []
            try:
                for artifact_index, artifact in enumerate(case.artifacts, start=1):
                    artifact_id = f"C{case_index:02d}-A{artifact_index:04d}"
                    artifact = replace(artifact, artifact_id=artifact_id)
                    if not artifact.linkable or artifact.source_identity is None:
                        updated.append(artifact)
                        continue
                    artifact_component = f"a{artifact_index:04d}"
                    filename = _portable_filename(PurePosixPath(artifact.runtime_path).name)
                    try:
                        if artifact.kind == "HTML report":
                            plan = _html_projection_plan(
                                root_fd,
                                artifact,
                                filename,
                                artifacts_by_path,
                            )
                        else:
                            plan = [_ProjectionPlanEntry(artifact, (filename,))]
                    except _ProjectionUnavailable as exc:
                        _append_issue_once(case, str(exc))
                        updated.append(_projection_issue(artifact, str(exc)))
                        continue
                    if not _projection_paths_fit(case.open_path, artifact_component, plan):
                        issue = "portable review path exceeds the short-path limit"
                        _append_issue_once(case, issue)
                        updated.append(_projection_issue(artifact, issue))
                        continue
                    if not _projection_budget_allows(projection_budget, plan):
                        issue = "portable review copy omitted by bounded projection limit"
                        _append_issue_once(case, issue)
                        updated.append(_projection_issue(artifact, issue))
                        continue
                    artifact_staging, artifact_fd = _create_artifact_staging(
                        case_fd,
                        artifact_component,
                    )
                    artifact_identity = _directory_identity(artifact_fd)
                    copied: list[tuple[_ProjectionPlanEntry, str]] = []
                    try:
                        for entry in plan:
                            copied.append(
                                (
                                    entry,
                                    _copy_projection_file(root_fd, artifact_fd, entry),
                                )
                            )
                        os.fsync(artifact_fd)
                    except _ProjectionUnavailable as exc:
                        os.close(artifact_fd)
                        artifact_fd = -1
                        _remove_projection_tree(case_fd, artifact_staging)
                        _append_issue_once(case, str(exc))
                        updated.append(_projection_issue(artifact, str(exc)))
                        continue
                    finally:
                        if artifact_fd >= 0:
                            os.close(artifact_fd)
                    _assert_named_directory_identity(
                        case_fd,
                        artifact_staging,
                        artifact_identity,
                    )
                    os.rename(
                        artifact_staging,
                        artifact_component,
                        src_dir_fd=case_fd,
                        dst_dir_fd=case_fd,
                    )
                    _assert_named_directory_identity(
                        case_fd,
                        artifact_component,
                        artifact_identity,
                    )
                    os.fsync(case_fd)
                    _consume_projection_budget(projection_budget, plan)
                    main_digest = copied[0][1]
                    open_path = (
                        f"{case.open_path}/{artifact_component}/"
                        + PurePosixPath(*plan[0].destination_parts).as_posix()
                    )
                    dependencies = tuple(
                        _ProjectionDependency(
                            source_path=entry.artifact.batch_path,
                            open_path=(
                                f"{case.open_path}/{artifact_component}/"
                                + PurePosixPath(*entry.destination_parts).as_posix()
                            ),
                            size_bytes=int(entry.artifact.size_bytes or 0),
                            sha256=digest,
                        )
                        for entry, digest in copied[1:]
                    )
                    updated.append(
                        replace(
                            artifact,
                            sha256=artifact.sha256 or main_digest,
                            open_path=open_path,
                            review_sha256=main_digest,
                            dependencies=dependencies,
                        )
                    )
                    openable_count += 1
            finally:
                os.close(case_fd)
            case.artifacts = updated

        payload = _projection_manifest(batch_manifest, generation, reviews)
        manifest_text = (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        _atomic_write_text(
            staging_fd,
            "artifacts.json",
            manifest_text,
        )
        os.fsync(staging_fd)
        _assert_named_directory_identity(review_fd, staging, staging_identity)
        os.rename(staging, generation, src_dir_fd=review_fd, dst_dir_fd=review_fd)
        cleanup_name = generation
        _assert_named_directory_identity(review_fd, generation, staging_identity)
        os.fsync(review_fd)
        publication = _ProjectionPublication(
            generation=generation,
            openable_count=openable_count,
            identity=staging_identity,
            manifest_text=manifest_text,
        )
        _verify_review_projection(
            root_fd,
            review_fd,
            reviews,
            publication=publication,
        )
        committed = True
        return publication
    except FileExistsError as exc:
        raise ReviewPacketError("review projection generation collided during publication") from exc
    finally:
        if open_fd is not None:
            os.close(open_fd)
        os.close(staging_fd)
        if not committed:
            try:
                _remove_projection_tree(review_fd, cleanup_name)
            except OSError:
                # The packet was not committed and index.html is therefore left
                # untouched. Cleanup is best-effort so it cannot mask the cause.
                pass


def _build_case_review(
    root_fd: int,
    manifest: Mapping[str, object],
    budget: _Budget,
) -> _CaseReview:
    fields = {name: _field_text(manifest.get(name, _MISSING)) for name in _REQUIRED_CASE_FIELDS}
    fields["expected_terminal"] = _terminal_text(manifest.get("expected_terminal", _MISSING))
    fields["observed_terminal"] = _terminal_text(manifest.get("observed_terminal", _MISSING))
    fields["review_status"] = _field_text(manifest.get("review_status", "unreviewed"))
    missing = [name for name in _REQUIRED_CASE_FIELDS if manifest.get(name, _MISSING) is _MISSING]
    if missing:
        fields["verdict"] = "invalid"

    try:
        case_parts = _relative_parts(manifest.get("case_dir", _MISSING), "case_dir")
        runtime_parts = _relative_parts(manifest.get("runtime_dir", "runtime"), "runtime_dir")
        if len(case_parts) + len(runtime_parts) > _MAX_DIRECTORY_DEPTH:
            raise ReviewPacketError("runtime path exceeds the directory depth limit")
    except ReviewPacketError:
        review = _CaseReview(fields=fields, case_path="[invalid]", runtime_path="[invalid]")
        review.issues.append("unsafe or invalid manifest path; artifact discovery was blocked")
        if missing:
            review.issues.append("required manifest fields are missing: " + ", ".join(missing))
        return review

    case_path = PurePosixPath(*case_parts).as_posix()
    runtime_path = PurePosixPath(*case_parts, *runtime_parts).as_posix()
    review = _CaseReview(fields=fields, case_path=case_path, runtime_path=runtime_path)
    if missing:
        review.issues.append("required manifest fields are missing: " + ", ".join(missing))

    try:
        runtime_fd = _open_relative_directory(root_fd, (*case_parts, *runtime_parts))
    except OSError:
        review.issues.append("runtime directory is missing, not a directory, or unsafe")
        return review

    try:
        _walk_runtime(
            runtime_fd,
            runtime_relative=(),
            batch_prefix=(*case_parts, *runtime_parts),
            depth=0,
            review=review,
            budget=budget,
        )
        review.artifacts.sort(key=_artifact_review_order)
    finally:
        os.close(runtime_fd)
    return review


def _artifact_review_order(artifact: _Artifact) -> tuple[int, str]:
    name = PurePosixPath(artifact.runtime_path).name.lower()
    if name in {
        "job_report.html",
        "workflow_report.html",
        "si_block.md",
        "workflow_si.md",
        "si_data.csv",
    }:
        priority = 0
    elif name in {
        "job_state.json",
        "job_report.json",
        "job_report.md",
        "workflow.json",
        "case.json",
    }:
        priority = 1
    elif name in {"harness.stdout.log", "harness.stderr.log", "pytest.xml"}:
        priority = 2
    elif artifact.kind in {"log / engine output", "raw engine artifact"}:
        priority = 3
    else:
        priority = 4
    return priority, artifact.runtime_path


def _relative_parts(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, os.PathLike):
        text = os.fspath(value)
    elif isinstance(value, str):
        text = value
    else:
        raise ReviewPacketError(f"{field_name} must be a relative POSIX path")
    if not isinstance(text, str):
        raise ReviewPacketError(f"{field_name} must be a relative POSIX path")
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ReviewPacketError(f"{field_name} could not be represented safely") from exc
    if len(encoded) > _MAX_RELATIVE_PATH_BYTES:
        raise ReviewPacketError(f"{field_name} exceeds the relative path limit")
    if not text or "\\" in text or any(ord(char) < 32 for char in text):
        raise ReviewPacketError(f"{field_name} must be a relative POSIX path")
    raw_parts = text.split("/")
    if text.startswith("/") or any(part in {"", ".", ".."} for part in raw_parts):
        raise ReviewPacketError(f"{field_name} must be a confined relative path")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts:
        raise ReviewPacketError(f"{field_name} must be a confined relative path")
    if any(len(part.encode("utf-8")) > _MAX_PATH_COMPONENT_BYTES for part in path.parts):
        raise ReviewPacketError(f"{field_name} contains an oversized path component")
    return path.parts


def _open_relative_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                os.close(next_fd)
                raise NotADirectoryError(part)
            os.close(current_fd)
            current_fd = next_fd
    except BaseException:
        os.close(current_fd)
        raise
    return current_fd


def _walk_runtime(
    directory_fd: int,
    *,
    runtime_relative: tuple[str, ...],
    batch_prefix: tuple[str, ...],
    depth: int,
    review: _CaseReview,
    budget: _Budget,
) -> None:
    if budget.entries_remaining <= 0:
        _append_issue_once(
            review,
            "global discovery entry limit reached; remaining entries were not discovered",
        )
        return
    if depth > _MAX_DIRECTORY_DEPTH:
        _append_issue_once(
            review, "directory depth limit reached; deeper entries were not discovered"
        )
        return

    entries: list[str] = []
    truncated = False
    entry_limit = min(_MAX_DIRECTORY_ENTRIES, budget.entries_remaining)
    try:
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                if len(entries) >= entry_limit:
                    truncated = True
                    break
                entries.append(entry.name)
    except OSError:
        _append_issue_once(review, "a runtime directory could not be read safely")
        return
    if truncated:
        issue = (
            "global discovery entry limit reached; remaining entries were not discovered"
            if entry_limit == budget.entries_remaining
            else "directory entry limit reached; remaining entries were omitted"
        )
        _append_issue_once(review, issue)

    for name in sorted(entries):
        if budget.entries_remaining <= 0:
            _append_issue_once(
                review,
                "global discovery entry limit reached; remaining entries were not discovered",
            )
            return
        # Every filesystem entry consumes the global traversal budget, including
        # ordinary directories. This keeps wide empty-directory trees bounded.
        budget.entries_remaining -= 1
        if not _safe_filesystem_name(name):
            review.artifacts.append(
                _blocked_artifact(
                    (*runtime_relative, "[unsafe filename]"),
                    (*batch_prefix, *runtime_relative, "[unsafe filename]"),
                    "filename could not be represented safely",
                )
            )
            continue
        relative_parts = (*runtime_relative, name)
        batch_parts = (*batch_prefix, *relative_parts)
        try:
            opened = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            review.artifacts.append(
                _blocked_artifact(relative_parts, batch_parts, "entry became unavailable")
            )
            continue

        if stat.S_ISLNK(opened.st_mode):
            review.artifacts.append(
                _blocked_artifact(relative_parts, batch_parts, "symlink blocked (target not read)")
            )
            continue
        if stat.S_ISDIR(opened.st_mode):
            if depth >= _MAX_DIRECTORY_DEPTH:
                review.artifacts.append(
                    _blocked_artifact(relative_parts, batch_parts, "directory depth limit reached")
                )
                continue
            try:
                child_fd = os.open(name, _directory_open_flags(), dir_fd=directory_fd)
            except OSError:
                review.artifacts.append(
                    _blocked_artifact(
                        relative_parts, batch_parts, "directory could not be opened safely"
                    )
                )
                continue
            try:
                _walk_runtime(
                    child_fd,
                    runtime_relative=relative_parts,
                    batch_prefix=batch_prefix,
                    depth=depth + 1,
                    review=review,
                    budget=budget,
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(opened.st_mode):
            review.artifacts.append(
                _blocked_artifact(relative_parts, batch_parts, "non-regular file blocked")
            )
            continue

        review.artifacts.append(
            _inspect_regular_file(
                directory_fd,
                name,
                relative_parts,
                batch_parts,
                budget,
                expected_stat=opened,
            )
        )


def _blocked_artifact(
    runtime_parts: tuple[str, ...],
    batch_parts: tuple[str, ...],
    issue: str,
) -> _Artifact:
    return _Artifact(
        runtime_path=PurePosixPath(*runtime_parts).as_posix(),
        batch_path=PurePosixPath(*batch_parts).as_posix(),
        kind="blocked",
        size_bytes=None,
        sha256=None,
        preview=None,
        preview_note="not opened",
        linkable=False,
        source_identity=None,
        disposition=_DISPOSITION_BLOCKED,
        issue=issue,
    )


def _inspect_regular_file(
    directory_fd: int,
    name: str,
    runtime_parts: tuple[str, ...],
    batch_parts: tuple[str, ...],
    budget: _Budget,
    *,
    expected_stat: os.stat_result,
) -> _Artifact:
    runtime_path = PurePosixPath(*runtime_parts).as_posix()
    batch_path = PurePosixPath(*batch_parts).as_posix()
    kind = _artifact_kind(name)
    try:
        file_fd = os.open(name, _file_open_flags(), dir_fd=directory_fd)
    except OSError:
        return _blocked_artifact(runtime_parts, batch_parts, "file could not be opened safely")

    try:
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            return _blocked_artifact(runtime_parts, batch_parts, "non-regular file blocked")
        if opened.st_nlink != 1:
            return _blocked_artifact(
                runtime_parts,
                batch_parts,
                "hard-linked file blocked (content not read)",
            )
        if (opened.st_dev, opened.st_ino) != (expected_stat.st_dev, expected_stat.st_ino):
            return _blocked_artifact(
                runtime_parts,
                batch_parts,
                "file changed before it could be opened safely",
            )
        size_bytes = opened.st_size
        preview_limit = min(_MAX_PREVIEW_BYTES, budget.preview_bytes_remaining)
        preview_bytes = _read_prefix(file_fd, preview_limit)
        budget.preview_bytes_remaining -= len(preview_bytes)

        digest: str | None = None
        bytes_read = 0
        if size_bytes <= _MAX_DIGEST_BYTES and size_bytes <= budget.digest_bytes_remaining:
            os.lseek(file_fd, 0, os.SEEK_SET)
            digest, bytes_read = _digest_file(file_fd)
            budget.digest_bytes_remaining -= bytes_read
        after = os.fstat(file_fd)
        digest_incomplete = digest is not None and bytes_read != size_bytes
        if (
            digest_incomplete
            or after.st_size != size_bytes
            or after.st_ino != opened.st_ino
            or after.st_dev != opened.st_dev
            or after.st_nlink != 1
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            return _Artifact(
                runtime_path=runtime_path,
                batch_path=batch_path,
                kind=kind,
                size_bytes=size_bytes,
                sha256=None,
                preview=None,
                preview_note="not shown",
                linkable=False,
                source_identity=None,
                disposition=_DISPOSITION_BLOCKED,
                issue="file changed while it was inspected",
            )

        preview, preview_note = _make_preview(
            preview_bytes,
            size_bytes=size_bytes,
            preview_limit=preview_limit,
            kind=kind,
        )
        if digest is None:
            preview_note += "; digest omitted by bounded scan limit"
        return _Artifact(
            runtime_path=runtime_path,
            batch_path=batch_path,
            kind=kind,
            size_bytes=size_bytes,
            sha256=digest,
            preview=preview,
            preview_note=preview_note,
            linkable=True,
            source_identity=_SourceIdentity(
                device=opened.st_dev,
                inode=opened.st_ino,
                size_bytes=size_bytes,
                modified_ns=opened.st_mtime_ns,
            ),
        )
    except OSError:
        return _blocked_artifact(runtime_parts, batch_parts, "file could not be read safely")
    finally:
        os.close(file_fd)


def _read_prefix(file_fd: int, limit: int) -> bytes:
    if limit <= 0:
        return b""
    chunks: list[bytes] = []
    remaining = limit
    while remaining:
        chunk = os.read(file_fd, min(remaining, _READ_CHUNK_BYTES))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _digest_file(file_fd: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(file_fd, _READ_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _make_preview(
    content: bytes,
    *,
    size_bytes: int,
    preview_limit: int,
    kind: str,
) -> tuple[str | None, str]:
    if preview_limit <= 0:
        return None, "preview omitted because the packet preview budget was exhausted"
    if not content and size_bytes == 0:
        return "[empty file]", "complete escaped text preview"
    if _is_probably_binary(content):
        return None, "binary content; no inline preview"

    decoded = content.decode("utf-8", errors="replace")
    decoded = _sanitize_control_characters(decoded)
    if _contains_possible_secret(decoded):
        return "[preview redacted: possible secret material]", "content was not embedded"
    truncated = size_bytes > len(content)
    type_note = "escaped source preview" if kind == "HTML report" else "escaped text preview"
    if truncated:
        return decoded, f"{type_note}; truncated to {len(content)} bytes"
    return decoded, f"complete {type_note}"


def _is_probably_binary(content: bytes) -> bool:
    if b"\x00" in content:
        return True
    if not content:
        return False
    control_count = sum(byte < 9 or 13 < byte < 32 for byte in content)
    return control_count / len(content) > 0.05


def _contains_possible_secret(text: str) -> bool:
    if _SECRET_ASSIGNMENT_RE.search(text):
        return True
    return any(pattern.search(text) for pattern in _SECRET_TOKEN_RES)


def _safe_filesystem_name(name: str) -> bool:
    return bool(name) and all(not 0xD800 <= ord(char) <= 0xDFFF for char in name)


def _sanitize_control_characters(text: str) -> str:
    return "".join(char if char in "\n\r\t" or ord(char) >= 32 else "\ufffd" for char in text)


def _artifact_kind(name: str) -> str:
    lower_name = name.lower()
    suffix = Path(lower_name).suffix
    if suffix in _HTML_SUFFIXES:
        return "HTML report"
    if suffix in _MARKDOWN_SUFFIXES:
        return "Markdown"
    if suffix in _JSON_SUFFIXES:
        return "JSON"
    if suffix in _TABLE_SUFFIXES:
        return "table"
    if suffix in _LOG_SUFFIXES:
        return "log / engine output"
    if (
        lower_name in _ENGINE_NAMES
        or suffix in _ENGINE_SUFFIXES
        or lower_name.endswith(".property.txt")
    ):
        return "raw engine artifact"
    if suffix in _TEXT_SUFFIXES:
        return "text"
    return "artifact"


def _append_issue_once(review: _CaseReview, issue: str) -> None:
    if issue not in review.issues:
        review.issues.append(issue)


def _field_text(value: object) -> str:
    if value is _MISSING:
        return "[missing]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        text = str(value)
    else:
        return "[invalid value]"
    text = _sanitize_control_characters(text)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    bounded = text[: _MAX_FIELD_CHARS + 1]
    if _contains_possible_secret(bounded):
        return "[redacted]"
    if len(bounded) > _MAX_FIELD_CHARS:
        return bounded[: _MAX_FIELD_CHARS - 1] + "\u2026"
    return bounded


def _terminal_text(value: object) -> str:
    if not isinstance(value, Mapping):
        return _field_text(value)
    status_text = _field_text(value.get("status", _MISSING))
    raw_reasons = value.get("reasons")
    if isinstance(raw_reasons, str):
        reasons = [_field_text(raw_reasons)]
    elif isinstance(raw_reasons, Sequence) and not isinstance(raw_reasons, (bytes, bytearray)):
        reasons = [_field_text(item) for item in raw_reasons[:3]]
        if len(raw_reasons) > 3:
            reasons.append("\u2026")
    else:
        reasons = []
    return status_text if not reasons else status_text + " \u2014 " + "; ".join(reasons)


def _batch_fields(manifest: Mapping[str, object]) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for key, label in (
        ("batch_id", "Batch"),
        ("profile", "Profile"),
        ("status", "Candidate status"),
        ("review_status", "Review status"),
        ("started_at", "Started"),
        ("finished_at", "Finished"),
        ("git_revision", "Git revision"),
        ("verdict", "Declared verdict"),
    ):
        if key in manifest:
            fields.append((label, _field_text(manifest[key])))
    source = manifest.get("source")
    if isinstance(source, Mapping) and "git_short" in source:
        fields.append(("Git revision", _field_text(source["git_short"])))
    if not fields:
        fields.append(("Batch", "[metadata unavailable]"))
    return fields


def _disposition_count(cases: Sequence[_CaseReview], disposition: str) -> int:
    return sum(artifact.disposition == disposition for case in cases for artifact in case.artifacts)


def _render_summary(
    manifest: Mapping[str, object],
    cases: list[_CaseReview],
    artifact_count: int,
    openable_count: int,
    blocked_count: int,
) -> str:
    artifact_manifest = _projection_manifest_batch_path(cases)
    lines = [
        "# Smoke review packet",
        "",
        "Open the offline HTML review surface: [review/index.html](review/index.html)",
        "Terminal authority: [batch.json](batch.json). The status shown in this review packet "
        "is provisional until that manifest records a terminal status.",
        f"Artifact provenance map: [{artifact_manifest}]({artifact_manifest})",
        "",
    ]
    for label, value in _batch_fields(manifest):
        lines.append(f"- {_md_text(label)}: `{_md_code(value)}`")
    lines.extend(
        [
            f"- Cases: `{len(cases)}`",
            f"- Discovered entries: `{artifact_count}`",
            f"- Files to review: `{openable_count}`",
            f"- Blocked entries: `{blocked_count}`",
            "",
            "Expected simulation failures count as smoke PASS only when the declared verdict says so. "
            "Expected, observed, and verdict are shown separately.",
            "",
            "| Case | Surface | Scenario | Expected | Observed | Verdict | Review issues |",
            "| --- | --- | --- | --- | --- | --- | ---: |",
        ]
    )
    for case in cases:
        values = [
            case.fields["case_id"],
            case.fields["surface"],
            case.fields["scenario"],
            case.fields["expected_terminal"],
            case.fields["observed_terminal"],
            case.fields["verdict"],
            str(len(case.issues)),
        ]
        lines.append("| " + " | ".join(_md_table(value) for value in values) + " |")

    for case in cases:
        lines.extend(
            [
                "",
                f"## {_md_text(case.fields['case_id'])}",
                "",
                f"- Review files: `{_md_code(case.open_path)}`",
                f"- Runtime provenance: `{_md_code(artifact_manifest)}`",
                f"- Expected terminal: `{_md_code(case.fields['expected_terminal'])}`",
                f"- Observed terminal: `{_md_code(case.fields['observed_terminal'])}`",
                f"- Verdict: `{_md_code(case.fields['verdict'])}`",
            ]
        )
        for issue in case.issues:
            lines.append(f"- Review issue: {_md_text(issue)}")
        lines.extend(["", "### Artifacts", ""])
        if not case.artifacts:
            lines.append("No reviewable runtime artifacts were discovered.")
            continue
        for artifact in case.artifacts:
            details = [artifact.kind]
            if artifact.size_bytes is not None:
                details.append(_format_size(artifact.size_bytes))
            if artifact.sha256 is not None:
                details.append(f"sha256 `{artifact.sha256}`")
            if artifact.issue:
                details.append(artifact.issue)
            label = _md_table(_artifact_label(artifact))
            if artifact.open_path is not None:
                target = quote(artifact.open_path, safe="/")
                lines.append(
                    f"- [{label}]({target}) \u2014 " + "; ".join(_md_text(x) for x in details)
                )
            else:
                lines.append(
                    f"- `{_md_code(_artifact_label(artifact))}` \u2014 "
                    + "; ".join(_md_text(x) for x in details)
                )
    return "\n".join(lines) + "\n"


def _render_index(
    manifest: Mapping[str, object],
    cases: list[_CaseReview],
    artifact_count: int,
    openable_count: int,
    blocked_count: int,
) -> str:
    batch_metadata = "".join(
        '<div class="metric"><span>'
        + html.escape(label)
        + "</span><strong>"
        + html.escape(value)
        + "</strong></div>"
        for label, value in _batch_fields(manifest)
    )
    batch_metadata += (
        f'<div class="metric"><span>Cases</span><strong>{len(cases)}</strong></div>'
        f'<div class="metric"><span>Entries found</span><strong>{artifact_count}</strong></div>'
        f'<div class="metric"><span>Files to review</span><strong>{openable_count}</strong></div>'
        f'<div class="metric"><span>Blocked entries</span><strong>{blocked_count}</strong></div>'
    )
    case_sections = "\n".join(
        _render_case_html(case, index=index) for index, case in enumerate(cases, start=1)
    )
    if not case_sections:
        case_sections = '<section class="empty">No case manifests were supplied.</section>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>orca_auto smoke review packet</title>
<style>
:root {{ color-scheme: light dark; --bg:#f4f7fb; --panel:#fff; --ink:#162033; --muted:#607086; --line:#dce3ec; --accent:#135e96; --good:#167348; --bad:#b42318; --warn:#9a6700; --code:#eef3f8; }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#10151d; --panel:#171e28; --ink:#eef4fb; --muted:#aab8c8; --line:#303b49; --accent:#72b7e8; --good:#56d39b; --bad:#ff8a82; --warn:#f6c85f; --code:#111822; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ width:min(1180px,calc(100% - 32px)); margin:32px auto 72px; }}
.hero,.case,.artifact,.empty {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; box-shadow:0 8px 24px rgba(20,34,54,.06); }}
.hero {{ padding:28px; }} h1,h2,h3 {{ line-height:1.2; }} h1 {{ margin:0 0 8px; font-size:28px; }}
.lede,.muted {{ color:var(--muted); }} .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-top:20px; }}
.metric {{ border:1px solid var(--line); border-radius:10px; padding:10px 12px; min-width:0; }} .metric span {{ display:block; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }} .metric strong {{ display:block; overflow-wrap:anywhere; }}
.case {{ margin-top:22px; padding:24px; }} .case-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }} .case-head h2 {{ margin:0; overflow-wrap:anywhere; }}
.badge {{ display:inline-block; border:1px solid currentColor; border-radius:999px; padding:3px 10px; font-size:12px; font-weight:700; text-transform:uppercase; }} .pass {{ color:var(--good); }} .fail {{ color:var(--bad); }} .warn {{ color:var(--warn); }} .neutral {{ color:var(--muted); }}
.contract {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px; margin:18px 0; }} .contract div {{ background:var(--code); border-radius:9px; padding:10px 12px; overflow-wrap:anywhere; }} .contract span {{ display:block; color:var(--muted); font-size:12px; }}
.issues {{ border-left:4px solid var(--warn); background:color-mix(in srgb,var(--warn) 8%,transparent); padding:10px 14px; }} .issues li {{ margin:4px 0; }}
.artifact {{ margin-top:12px; padding:15px; box-shadow:none; }} .artifact-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; }} .artifact-title {{ min-width:0; }} .artifact-title code {{ overflow-wrap:anywhere; }}
.meta {{ color:var(--muted); font-size:13px; margin-top:4px; overflow-wrap:anywhere; }} a.open {{ color:var(--accent); border:1px solid currentColor; border-radius:8px; padding:6px 10px; text-decoration:none; white-space:nowrap; }} a.open:hover {{ text-decoration:underline; }}
.provenance {{ margin-top:10px; }} .provenance div {{ margin-top:7px; color:var(--muted); font-size:12px; overflow-wrap:anywhere; }}
details {{ margin-top:10px; }} summary {{ color:var(--accent); cursor:pointer; }} pre {{ max-height:420px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; background:var(--code); border-radius:9px; padding:12px; font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace; }} code {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }}
.blocked {{ border-color:var(--warn); }} .empty {{ padding:24px; margin-top:22px; }}
@media (max-width:640px) {{ main {{ width:min(100% - 18px,1180px); margin-top:10px; }} .hero,.case {{ padding:17px; }} .case-head,.artifact-head {{ display:block; }} a.open {{ display:inline-block; margin-top:10px; }} }}
</style>
</head>
<body><main>
<section class="hero">
<h1>Smoke review packet</h1>
<p class="lede"><strong>This packet shows a provisional candidate status.</strong> The only terminal authority is <a class="open" href="../batch.json">batch.json</a>. Expected and observed terminal states are deliberately separate. Open links use bounded short-path copies; full runtime paths and source digests remain under Provenance. Pytest "current" shortcuts are summarized instead of shown as artifacts.</p>
<div class="metrics">{batch_metadata}</div>
</section>
{case_sections}
</main></body></html>
"""


def _render_case_html(case: _CaseReview, *, index: int) -> str:
    fields = case.fields
    badge_class = _verdict_class(fields["verdict"])
    issues = ""
    if case.issues:
        issue_items = "".join(f"<li>{html.escape(issue)}</li>" for issue in case.issues)
        issues = f'<div class="issues"><strong>Review issues</strong><ul>{issue_items}</ul></div>'
    artifacts = "".join(_render_artifact_html(artifact) for artifact in case.artifacts)
    if not artifacts:
        artifacts = '<p class="muted">No reviewable runtime artifacts were discovered.</p>'
    return f"""<section class="case" id="case-{index}">
<div class="case-head"><div><div class="muted">{html.escape(fields["surface"])} \u00b7 {html.escape(fields["scenario"])}</div><h2>{html.escape(fields["case_id"])}</h2></div><span class="badge {badge_class}">{html.escape(fields["verdict"])}</span></div>
<div class="contract">
<div><span>Expected terminal</span>{html.escape(fields["expected_terminal"])}</div>
<div><span>Observed terminal</span>{html.escape(fields["observed_terminal"])}</div>
<div><span>Review files</span>{html.escape(case.open_path)}</div>
<div><span>Review status</span>{html.escape(fields["review_status"])}</div>
</div>
<details class="provenance"><summary>Case provenance</summary><div><strong>Runtime source:</strong> <code>{html.escape(case.runtime_path)}</code></div></details>
{issues}

<h3>Artifacts</h3>
{artifacts}
</section>"""


def _render_artifact_html(artifact: _Artifact) -> str:
    classes = "artifact blocked" if artifact.open_path is None else "artifact"
    link = ""
    if artifact.open_path is not None:
        href = _index_href(artifact.open_path)
        link_label = "Open HTML report" if artifact.kind == "HTML report" else "Open artifact"
        link = (
            f'<a class="open" href="{html.escape(href, quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">{link_label}</a>'
        )
    metadata = [artifact.kind]
    if artifact.size_bytes is not None:
        metadata.append(_format_size(artifact.size_bytes))
    if artifact.issue is not None:
        metadata.append(artifact.issue)
    preview = ""
    if artifact.preview is not None:
        preview = (
            "<details><summary>Preview \u2014 "
            + html.escape(artifact.preview_note)
            + "</summary><pre>"
            + html.escape(artifact.preview)
            + "</pre></details>"
        )
    else:
        preview = f'<div class="meta">{html.escape(artifact.preview_note)}</div>'
    metadata_html = html.escape(" \u00b7 ".join(metadata))
    source_digest = artifact.sha256 or "unavailable"
    review_digest = artifact.review_sha256 or "unavailable"
    provenance = f"""<details class="provenance"><summary>Provenance</summary>
<div><strong>Runtime source:</strong> <code>{html.escape(artifact.batch_path)}</code></div>
<div><strong>Source SHA-256:</strong> <code>{html.escape(source_digest)}</code></div>
<div><strong>Review SHA-256:</strong> <code>{html.escape(review_digest)}</code></div>
</details>"""
    return f"""<article class="{classes}">
<div class="artifact-head"><div class="artifact-title"><code>{html.escape(_artifact_label(artifact))}</code><div class="meta">{metadata_html}</div></div>{link}</div>
{provenance}
{preview}
</article>"""


def _projection_manifest_batch_path(cases: Sequence[_CaseReview]) -> str:
    for case in cases:
        parts = PurePosixPath(case.open_path).parts
        if len(parts) >= 2 and parts[0] == "review":
            return PurePosixPath(parts[0], parts[1], "artifacts.json").as_posix()
    return "review/artifacts.json"


def _artifact_label(artifact: _Artifact) -> str:
    name = PurePosixPath(artifact.runtime_path).name
    if len(name) > 80:
        name = name[:76] + "\u2026"
    identifier = artifact.artifact_id or "artifact"
    return f"{identifier} \u00b7 {name}"


def _index_href(batch_path: str) -> str:
    quoted = quote(batch_path, safe="/")
    prefix = "review/"
    return quoted[len(prefix) :] if quoted.startswith(prefix) else "../" + quoted


def _verdict_class(verdict: str) -> str:
    lowered = verdict.strip().lower()
    if lowered in {"pass", "passed", "success", "succeeded"}:
        return "pass"
    if lowered in {"fail", "failed", "failure", "error", "invalid"}:
        return "fail"
    if lowered in {"warn", "warning", "review"}:
        return "warn"
    return "neutral"


def _format_size(size_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{size_bytes} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def _md_text(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char in ("\\", "`", "*", "_", "{", "}", "[", "]", "(", ")", "#", "+", "-", ".", "!", "|"):
        escaped = escaped.replace(char, "\\" + char)
    return escaped


def _md_table(text: str) -> str:
    return _md_text(text).replace("\n", " ").replace("\r", " ")


def _md_code(text: str) -> str:
    return text.replace("`", "\u02cb").replace("\n", " ").replace("\r", " ").replace("\t", " ")


__all__ = ["ReviewPacketError", "ReviewPacketResult", "generate_review_packet"]
