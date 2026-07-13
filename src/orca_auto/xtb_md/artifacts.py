from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from orca_auto.core.utils.persistence import fsync_directory

from .manifest import XtbMdManifest

TRAJECTORY_FILE_NAME = "xtb.trj"
CHECKPOINT_FILE_NAME = "mdrestart"
SUCCESS_MARKER_FILE_NAME = "xtbmdok"

_FATAL_LOG_MARKERS = (
    "md is unstable, emergency exit",
    "but still taking it as converged!",
    "thermostating problem",
    "abnormal termination of xtb",
)
_MAX_STEPS_RE = re.compile(r"^\s*max steps\s*:\s*(\d+)\s*$", re.IGNORECASE | re.MULTILINE)
_FINITE_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?")
_ATTEMPT_IDENTITY_FILE_NAME = ".orca_auto_xtb_md_attempt"
_MAX_ARTIFACT_LINE_BYTES = 1024 * 1024


class XtbMdArtifactError(ValueError):
    """A terminal result cannot be proven safe and complete."""


@dataclass(frozen=True)
class AttemptIdentity:
    path: Path
    device: int
    inode: int
    started_at_ns: int
    token: str
    token_device: int
    token_inode: int
    preexisting_names: frozenset[str]
    preexisting_file_ids: frozenset[tuple[int, int]]


@dataclass(frozen=True)
class ArtifactIdentity:
    path: str
    sha256: str
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class XtbMdTerminalArtifacts:
    trajectory: ArtifactIdentity
    checkpoint: ArtifactIdentity
    success_marker: ArtifactIdentity
    stdout_log: ArtifactIdentity
    stderr_log: ArtifactIdentity
    frame_count: int
    completed_steps: int
    atom_count: int

    @property
    def output_identities(self) -> dict[str, dict[str, str | int]]:
        return {
            name: {
                "path": identity.path,
                "sha256": identity.sha256,
                "size_bytes": identity.size_bytes,
                "mtime_ns": identity.mtime_ns,
            }
            for name, identity in (
                ("trajectory", self.trajectory),
                ("checkpoint", self.checkpoint),
                ("success_marker", self.success_marker),
                ("stdout_log", self.stdout_log),
                ("stderr_log", self.stderr_log),
            )
        }


@dataclass(frozen=True)
class _OpenedArtifact:
    path: Path
    descriptor: int
    before: os.stat_result


@dataclass(frozen=True)
class _LogEvidence:
    fatal_markers: tuple[str, ...]
    md_normal_exit: bool
    global_normal_termination: bool
    max_steps: tuple[int, ...]


def capture_attempt_identity(attempt_dir: str | Path) -> AttemptIdentity:
    """Bind validation to one private directory before the xTB child starts."""

    candidate = Path(attempt_dir).expanduser()
    if candidate.is_symlink():
        raise XtbMdArtifactError("xTB-MD attempt directory must not be a symlink")
    resolved = candidate.resolve()
    try:
        directory_status = resolved.stat()
    except OSError as exc:
        raise XtbMdArtifactError("xTB-MD attempt directory is not readable") from exc
    if not stat.S_ISDIR(directory_status.st_mode):
        raise XtbMdArtifactError("xTB-MD attempt path is not a directory")
    if directory_status.st_uid != os.geteuid():
        raise XtbMdArtifactError("xTB-MD attempt directory must be owned by the current user")
    if stat.S_IMODE(directory_status.st_mode) & 0o077:
        os.chmod(resolved, 0o700)
        directory_status = resolved.stat()
        if stat.S_IMODE(directory_status.st_mode) & 0o077:
            raise XtbMdArtifactError("xTB-MD attempt directory must be private")
    preexisting_entries = tuple(resolved.iterdir())
    preexisting_names = frozenset(entry.name for entry in preexisting_entries)
    preexisting_file_ids = frozenset(
        (entry_status.st_dev, entry_status.st_ino)
        for entry in preexisting_entries
        if stat.S_ISREG((entry_status := entry.stat(follow_symlinks=False)).st_mode)
    )
    if _ATTEMPT_IDENTITY_FILE_NAME in preexisting_names:
        raise XtbMdArtifactError("xTB-MD attempt identity already exists")
    for filename in (TRAJECTORY_FILE_NAME, CHECKPOINT_FILE_NAME, SUCCESS_MARKER_FILE_NAME):
        output = resolved / filename
        if output.exists() or output.is_symlink():
            raise XtbMdArtifactError(f"xTB-MD attempt contains stale canonical output: {filename}")
    token = secrets.token_hex(32)
    token_path = resolved / _ATTEMPT_IDENTITY_FILE_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(token_path, flags, 0o400)
    try:
        payload = token.encode("ascii")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        token_status = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(resolved, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return AttemptIdentity(
        path=resolved,
        device=directory_status.st_dev,
        inode=directory_status.st_ino,
        started_at_ns=time.time_ns(),
        token=token,
        token_device=token_status.st_dev,
        token_inode=token_status.st_ino,
        preexisting_names=preexisting_names,
        preexisting_file_ids=preexisting_file_ids,
    )


def _verify_attempt(attempt: AttemptIdentity) -> Path:
    candidate = attempt.path
    if candidate.is_symlink() or candidate.resolve() != candidate:
        raise XtbMdArtifactError("xTB-MD attempt directory identity changed")
    try:
        current = candidate.stat()
    except OSError as exc:
        raise XtbMdArtifactError("xTB-MD attempt directory is missing") from exc
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
        attempt.device,
        attempt.inode,
    ):
        raise XtbMdArtifactError("xTB-MD attempt directory identity changed")
    token_path = candidate / _ATTEMPT_IDENTITY_FILE_NAME
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    token_descriptor = -1
    try:
        token_descriptor = os.open(token_path, flags)
        token_status = os.fstat(token_descriptor)
        token_payload = os.read(token_descriptor, 65).decode("ascii", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise XtbMdArtifactError("xTB-MD attempt identity changed") from exc
    finally:
        if token_descriptor >= 0:
            os.close(token_descriptor)
    if (
        not stat.S_ISREG(token_status.st_mode)
        or token_status.st_nlink != 1
        or (token_status.st_dev, token_status.st_ino) != (attempt.token_device, attempt.token_inode)
        or token_payload != attempt.token
    ):
        raise XtbMdArtifactError("xTB-MD attempt identity changed")
    return candidate


def _open_fresh_artifact(
    attempt: AttemptIdentity,
    path: str | Path,
    *,
    label: str,
    max_bytes: int,
) -> _OpenedArtifact:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError(f"xTB-MD {label} byte limit must be a positive integer")
    root = _verify_attempt(attempt)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.parent.resolve() != root or candidate.is_symlink():
        raise XtbMdArtifactError(f"xTB-MD {label} must be a direct regular file in the attempt")
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise XtbMdArtifactError(f"xTB-MD {label} is missing or unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise XtbMdArtifactError(f"xTB-MD {label} must be a single-link regular file")
        if before.st_size > max_bytes:
            raise XtbMdArtifactError(f"xTB-MD {label} exceeds its server byte limit")
        if candidate.name in attempt.preexisting_names:
            raise XtbMdArtifactError(f"xTB-MD {label} predates this attempt")
        if (before.st_dev, before.st_ino) in attempt.preexisting_file_ids:
            raise XtbMdArtifactError(f"xTB-MD {label} was renamed from a pre-attempt file")
        return _OpenedArtifact(path=candidate.resolve(), descriptor=descriptor, before=before)
    except BaseException:
        os.close(descriptor)
        raise


def _finish_identity(opened: _OpenedArtifact, digest: Any, total_bytes: int) -> ArtifactIdentity:
    after = os.fstat(opened.descriptor)
    before = opened.before
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or total_bytes != after.st_size:
        raise XtbMdArtifactError(
            f"xTB-MD artifact changed while it was validated: {opened.path.name}"
        )
    try:
        os.fsync(opened.descriptor)
        synced = os.fstat(opened.descriptor)
    except OSError as exc:
        raise XtbMdArtifactError(
            f"xTB-MD artifact could not be durably synchronized: {opened.path.name}"
        ) from exc
    if (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) != (
        synced.st_dev,
        synced.st_ino,
        synced.st_size,
        synced.st_mtime_ns,
        synced.st_ctime_ns,
    ):
        raise XtbMdArtifactError(
            f"xTB-MD artifact changed while it was synchronized: {opened.path.name}"
        )
    return ArtifactIdentity(
        path=str(opened.path),
        sha256=digest.hexdigest(),
        size_bytes=total_bytes,
        mtime_ns=synced.st_mtime_ns,
    )


def _hashed_lines(handle: BinaryIO, digest: Any) -> Iterator[str]:
    while raw_line := handle.readline(_MAX_ARTIFACT_LINE_BYTES + 1):
        if len(raw_line) > _MAX_ARTIFACT_LINE_BYTES:
            raise XtbMdArtifactError("xTB-MD artifact contains an overlong text line")
        digest.update(raw_line)
        try:
            yield raw_line.decode("utf-8", errors="strict").rstrip("\r\n")
        except UnicodeError as exc:
            raise XtbMdArtifactError("xTB-MD artifact is not valid UTF-8 text") from exc


def _read_text_artifact(
    attempt: AttemptIdentity,
    path: str | Path,
    *,
    label: str,
    max_bytes: int,
) -> tuple[str, ArtifactIdentity]:
    opened = _open_fresh_artifact(attempt, path, label=label, max_bytes=max_bytes)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        while chunk := os.read(opened.descriptor, min(1024 * 1024, max_bytes - total + 1)):
            total += len(chunk)
            if total > max_bytes:
                raise XtbMdArtifactError(f"xTB-MD {label} exceeds its server byte limit")
            digest.update(chunk)
            chunks.append(chunk)
        identity = _finish_identity(opened, digest, total)
    finally:
        os.close(opened.descriptor)
    try:
        text = b"".join(chunks).decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise XtbMdArtifactError(f"xTB-MD {label} is not valid UTF-8 text") from exc
    return text, identity


def _scan_log_artifact(
    attempt: AttemptIdentity,
    path: str | Path,
    *,
    label: str,
    max_bytes: int,
    collect_steps: bool,
) -> tuple[_LogEvidence, ArtifactIdentity]:
    opened = _open_fresh_artifact(attempt, path, label=label, max_bytes=max_bytes)
    digest = hashlib.sha256()
    fatal_markers: set[str] = set()
    md_normal_exit = False
    global_normal_termination = False
    max_steps: list[int] = []
    total = 0
    try:
        with os.fdopen(opened.descriptor, "rb", closefd=False) as handle:
            for line in _hashed_lines(handle, digest):
                normalized = line.casefold()
                fatal_markers.update(
                    marker for marker in _FATAL_LOG_MARKERS if marker in normalized
                )
                md_normal_exit = md_normal_exit or "normal exit of md()" in normalized
                global_normal_termination = (
                    global_normal_termination or "normal termination of xtb" in normalized
                )
                if collect_steps and (match := _MAX_STEPS_RE.fullmatch(line)) is not None:
                    max_steps.append(int(match.group(1)))
            total = handle.tell()
        identity = _finish_identity(opened, digest, total)
    finally:
        os.close(opened.descriptor)
    return (
        _LogEvidence(
            fatal_markers=tuple(sorted(fatal_markers)),
            md_normal_exit=md_normal_exit,
            global_normal_termination=global_normal_termination,
            max_steps=tuple(max_steps),
        ),
        identity,
    )


def _next_nonblank(lines: Iterator[str]) -> str | None:
    for line in lines:
        if line.strip():
            return line
    return None


def _parse_finite_float(
    token: str,
    *,
    label: str,
    max_abs_value: float | None = None,
) -> float:
    if token.casefold().lstrip("+-") in {"nan", "inf", "infinity"}:
        raise XtbMdArtifactError(f"xTB-MD {label} contains a non-finite number")
    if not _FINITE_NUMBER_RE.fullmatch(token):
        raise XtbMdArtifactError(f"xTB-MD {label} contains an invalid number")
    try:
        value = float(token.replace("D", "E").replace("d", "e"))
    except ValueError as exc:
        raise XtbMdArtifactError(f"xTB-MD {label} contains an invalid number") from exc
    if not math.isfinite(value):
        raise XtbMdArtifactError(f"xTB-MD {label} contains a non-finite number")
    if max_abs_value is not None and abs(value) > max_abs_value:
        raise XtbMdArtifactError(f"xTB-MD {label} exceeds the coordinate magnitude limit")
    return value


def _validate_trajectory(
    attempt: AttemptIdentity,
    manifest: XtbMdManifest,
    *,
    max_bytes: int,
) -> tuple[ArtifactIdentity, int]:
    opened = _open_fresh_artifact(
        attempt,
        TRAJECTORY_FILE_NAME,
        label="trajectory",
        max_bytes=max_bytes,
    )
    digest = hashlib.sha256()
    frame_count = 0
    total = 0
    try:
        with os.fdopen(opened.descriptor, "rb", closefd=False) as handle:
            lines = _hashed_lines(handle, digest)
            while (atom_count_line := _next_nonblank(lines)) is not None:
                total = handle.tell()
                try:
                    atom_count = int(atom_count_line.strip())
                except ValueError as exc:
                    raise XtbMdArtifactError("xTB-MD trajectory has an invalid atom count") from exc
                if atom_count != manifest.atom_count:
                    raise XtbMdArtifactError(
                        "xTB-MD trajectory atom count does not match the input"
                    )
                try:
                    next(lines)  # XYZ comment; its free-form NaN text is not a terminal verdict.
                except StopIteration as exc:
                    raise XtbMdArtifactError(
                        "xTB-MD trajectory is truncated before its comment"
                    ) from exc
                for atom_index, expected_symbol in enumerate(manifest.atom_symbols, 1):
                    try:
                        atom_line = next(lines)
                    except StopIteration as exc:
                        raise XtbMdArtifactError(
                            "xTB-MD trajectory contains a truncated frame"
                        ) from exc
                    tokens = atom_line.split()
                    if len(tokens) < 4:
                        raise XtbMdArtifactError("xTB-MD trajectory contains a truncated atom row")
                    if len(tokens) > 4:
                        raise XtbMdArtifactError(
                            "xTB-MD trajectory atom row must contain exactly four values"
                        )
                    if tokens[0].casefold() != expected_symbol.casefold():
                        raise XtbMdArtifactError(
                            "xTB-MD trajectory atom order or elements do not match the input"
                        )
                    for token in tokens[1:4]:
                        _parse_finite_float(
                            token,
                            label=f"trajectory atom {atom_index}",
                            max_abs_value=manifest.max_abs_coordinate,
                        )
                frame_count += 1
            total = handle.tell()
        identity = _finish_identity(opened, digest, total)
    finally:
        os.close(opened.descriptor)
    if frame_count != manifest.expected_frames:
        raise XtbMdArtifactError(
            "xTB-MD trajectory frame count does not prove all requested steps completed: "
            f"expected={manifest.expected_frames} actual={frame_count}"
        )
    return identity, frame_count


def _validate_checkpoint(
    attempt: AttemptIdentity,
    manifest: XtbMdManifest,
    *,
    max_bytes: int,
) -> ArtifactIdentity:
    opened = _open_fresh_artifact(
        attempt,
        CHECKPOINT_FILE_NAME,
        label="checkpoint",
        max_bytes=max_bytes,
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(opened.descriptor, "rb", closefd=False) as handle:
            lines = _hashed_lines(handle, digest)
            header = _next_nonblank(lines)
            if header is None or len(header.split()) != 1:
                raise XtbMdArtifactError("xTB-MD checkpoint has an invalid header")
            if _parse_finite_float(header.strip(), label="checkpoint header") != -1.0:
                raise XtbMdArtifactError("xTB-MD checkpoint has an unsupported format marker")
            rows = 0
            for line in lines:
                if not line.strip():
                    continue
                tokens = line.split()
                if len(tokens) != 6:
                    raise XtbMdArtifactError("xTB-MD checkpoint atom row must contain six values")
                for token in tokens:
                    _parse_finite_float(
                        token,
                        label="checkpoint",
                        max_abs_value=manifest.max_abs_coordinate,
                    )
                rows += 1
            total = handle.tell()
        identity = _finish_identity(opened, digest, total)
    finally:
        os.close(opened.descriptor)
    if rows != manifest.atom_count:
        raise XtbMdArtifactError("xTB-MD checkpoint atom count does not match the input")
    return identity


def _validate_empty_marker(
    attempt: AttemptIdentity,
    *,
    max_bytes: int,
) -> ArtifactIdentity:
    text, identity = _read_text_artifact(
        attempt,
        SUCCESS_MARKER_FILE_NAME,
        label="success marker",
        max_bytes=max_bytes,
    )
    if text or identity.size_bytes != 0:
        raise XtbMdArtifactError("xTB-MD success marker must be the fresh empty xtbmdok file")
    return identity


def validate_terminal_artifacts(
    attempt: AttemptIdentity,
    *,
    manifest: XtbMdManifest,
    exit_code: int,
    stdout_log: str | Path,
    stderr_log: str | Path,
    max_log_bytes: int,
    max_trajectory_bytes: int,
    max_checkpoint_bytes: int,
    max_marker_bytes: int = 1,
) -> XtbMdTerminalArtifacts:
    """Prove a fresh run completed, rejecting xTB 6.7.1-style false success."""

    if exit_code != 0:
        raise XtbMdArtifactError(f"xTB-MD process exited with code {exit_code}")
    stdout_evidence, stdout_identity = _scan_log_artifact(
        attempt,
        stdout_log,
        label="stdout log",
        max_bytes=max_log_bytes,
        collect_steps=True,
    )
    stderr_evidence, stderr_identity = _scan_log_artifact(
        attempt,
        stderr_log,
        label="stderr log",
        max_bytes=max_log_bytes,
        collect_steps=False,
    )
    fatal_markers = sorted({*stdout_evidence.fatal_markers, *stderr_evidence.fatal_markers})
    if fatal_markers:
        raise XtbMdArtifactError(f"xTB-MD log contains fatal marker: {fatal_markers[0]}")
    if not (stdout_evidence.md_normal_exit or stderr_evidence.md_normal_exit):
        raise XtbMdArtifactError("xTB-MD log lacks the MD normal-exit marker")
    if not (stdout_evidence.global_normal_termination or stderr_evidence.global_normal_termination):
        raise XtbMdArtifactError("xTB-MD log lacks the global normal-termination marker")
    if stdout_evidence.max_steps != (manifest.expected_steps,):
        raise XtbMdArtifactError(
            "xTB-MD log does not bind the run to the requested number of propagation steps"
        )

    marker_identity = _validate_empty_marker(attempt, max_bytes=max_marker_bytes)
    trajectory_identity, frame_count = _validate_trajectory(
        attempt,
        manifest,
        max_bytes=max_trajectory_bytes,
    )
    checkpoint_identity = _validate_checkpoint(
        attempt,
        manifest,
        max_bytes=max_checkpoint_bytes,
    )
    try:
        fsync_directory(_verify_attempt(attempt))
    except OSError as exc:
        raise XtbMdArtifactError(
            "xTB-MD attempt directory could not be durably synchronized"
        ) from exc
    return XtbMdTerminalArtifacts(
        trajectory=trajectory_identity,
        checkpoint=checkpoint_identity,
        success_marker=marker_identity,
        stdout_log=stdout_identity,
        stderr_log=stderr_identity,
        frame_count=frame_count,
        completed_steps=manifest.expected_steps,
        atom_count=manifest.atom_count,
    )


__all__ = [
    "ArtifactIdentity",
    "AttemptIdentity",
    "CHECKPOINT_FILE_NAME",
    "SUCCESS_MARKER_FILE_NAME",
    "TRAJECTORY_FILE_NAME",
    "XtbMdArtifactError",
    "XtbMdTerminalArtifacts",
    "capture_attempt_identity",
    "validate_terminal_artifacts",
]
