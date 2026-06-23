from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_UNC_RE = re.compile(r"^\\\\")
_WSL_WINDOWS_MOUNT_RE = re.compile(r"^/mnt/[a-zA-Z](/|$)")

ExecutableErrorMessage = str | Callable[[Path], str]


def is_rejected_windows_path(path_text: str) -> bool:
    text = str(path_text).strip()
    return bool(
        _WINDOWS_DRIVE_RE.match(text)
        or _WINDOWS_UNC_RE.match(text)
        or _WSL_WINDOWS_MOUNT_RE.match(text)
    )


def _executable_error_message(message: ExecutableErrorMessage, path: Path) -> str:
    if callable(message):
        return message(path)
    return message


def validate_executable_file(
    path_value: str | Path,
    *,
    missing_message: ExecutableErrorMessage,
    not_file_message: ExecutableErrorMessage,
    not_executable_message: ExecutableErrorMessage,
    access_fn: Callable[[str, int], bool] = os.access,
) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.exists():
        raise ValueError(_executable_error_message(missing_message, path))
    if not path.is_file():
        raise ValueError(_executable_error_message(not_file_message, path))
    if not access_fn(str(path), os.X_OK):
        raise ValueError(_executable_error_message(not_executable_message, path))
    return path


def validate_configured_executable_path(
    path_value: str | Path,
    *,
    label: str,
    display_name: str,
    access_fn: Callable[[str, int], bool] = os.access,
) -> Path:
    text = str(path_value).strip()
    if not text:
        raise ValueError(f"{label} is required.")
    if is_rejected_windows_path(text):
        raise ValueError(
            f"{label} must be a Linux path (Windows paths are not supported): {text!r}"
        )
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute Linux path: {text!r}")
    if text.lower().endswith(".exe"):
        raise ValueError(
            f"{label} must point to Linux {display_name} binary, not Windows executable: {text!r}"
        )
    return validate_executable_file(
        candidate,
        missing_message=lambda _resolved: (
            f"{label} not found: {text!r}. "
            f"Verify the path points to an existing {display_name} binary."
        ),
        not_file_message=lambda _resolved: f"{label} is not a file: {text!r}",
        not_executable_message=lambda _resolved: f"{label} is not executable: {text!r}",
        access_fn=access_fn,
    )


def resolve_local_path(path_text: str | Path) -> Path:
    text = str(path_text).strip()
    if not text:
        raise ValueError("Path must not be empty.")
    if is_rejected_windows_path(text):
        raise ValueError(f"Windows-style and /mnt/<drive> paths are not supported: {text}")
    return Path(text).expanduser().resolve()


def is_subpath(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def safe_is_subpath(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def resolved_path_text(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def iter_existing_dirs(*candidates: Path | None) -> list[Path]:
    rows: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if not resolved.exists() or not resolved.is_dir() or resolved in seen:
            continue
        seen.add(resolved)
        rows.append(resolved)
    return rows


def first_existing_named_file(search_dirs: list[Path], filenames: list[str]) -> str:
    for search_dir in search_dirs:
        for filename in filenames:
            candidate = search_dir / filename
            if candidate.exists():
                return resolved_path_text(candidate)
    return ""


def recent_file_candidates(
    search_dirs: list[Path],
    *,
    suffix: str,
    exclude: Path | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    seen_files: set[Path] = set()
    for search_dir in search_dirs:
        try:
            files = sorted(
                (item for item in search_dir.glob(f"*{suffix}") if item.is_file()),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            continue
        for item in files:
            try:
                resolved = item.resolve()
            except OSError:
                resolved = item
            if exclude is not None and resolved == exclude:
                continue
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            candidates.append(item)
    return candidates


def require_subpath(path: Path, root: Path, *, label: str = "Path") -> Path:
    resolved_path = path.expanduser().resolve()
    resolved_root = root.expanduser().resolve()
    if not is_subpath(resolved_path, resolved_root):
        raise ValueError(
            f"{label} must be under allowed root: {resolved_root}. got={resolved_path}"
        )
    return resolved_path


def ensure_directory(path_text: str | Path, *, label: str = "Directory") -> Path:
    path = resolve_local_path(path_text)
    if not path.exists():
        raise ValueError(f"{label} not found: {path}")
    if not path.is_dir():
        raise ValueError(f"{label} is not a directory: {path}")
    return path


def validate_job_dir(
    job_dir_text: str, allowed_root_text: str, *, label: str = "Job directory"
) -> Path:
    job_dir = ensure_directory(job_dir_text, label=label)
    allowed_root = ensure_directory(allowed_root_text, label="Allowed root")
    return require_subpath(job_dir, allowed_root, label=label)


def resolve_artifact_path(path_text: str, base_dir: str | Path) -> Path | None:
    raw = str(path_text).strip()
    if not raw:
        return None
    base = Path(base_dir).expanduser().resolve()
    candidate = Path(raw)
    candidates = (
        [candidate] if candidate.is_absolute() else [base / candidate, base / candidate.name]
    )
    seen: set[Path] = set()
    for item in candidates:
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved
    return None
