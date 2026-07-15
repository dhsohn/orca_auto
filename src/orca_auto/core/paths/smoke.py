from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from orca_auto.core.queue.generation import is_visible_generation_name

SMOKE_RESULTS_DIRNAME = ".orca_auto_smoke"


def _lexical_absolute(path: str | Path, *, label: str) -> Path:
    text = str(path).strip()
    if not text:
        raise ValueError(f"{label} must not be empty")
    try:
        return Path(os.path.abspath(Path(text).expanduser()))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"Could not safely normalize {label}: {text!r}") from exc


def _resolved(path: Path, *, label: str) -> Path:
    existing_prefix = path
    while True:
        try:
            existing_prefix.lstat()
        except FileNotFoundError:
            parent = existing_prefix.parent
            if parent == existing_prefix:
                break
            existing_prefix = parent
            continue
        except OSError as exc:
            raise ValueError(f"Could not safely inspect {label}: {path}") from exc
        try:
            existing_prefix.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Could not safely resolve {label}: {path}") from exc
        break

    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Could not safely resolve {label}: {path}") from exc


def _relative_if_inside(path: Path, root: Path) -> Path | None:
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def _relative_is_reserved(relative: Path | None) -> bool:
    return bool(
        relative is not None and relative.parts and relative.parts[0] == SMOKE_RESULTS_DIRNAME
    )


def _relative_is_visible_generation(relative: Path | None) -> bool:
    return bool(
        relative is not None
        and any(is_visible_generation_name(component) for component in relative.parts)
    )


def is_path_in_reserved_smoke_tree(path: str | Path, runs_root: str | Path) -> bool:
    """Return whether *path* belongs to ``<runs_root>/.orca_auto_smoke``.

    The reservation is relative to the supplied runs root.  A smoke case can
    therefore use its own nested runs root without excluding its jobs from its
    own queue, discovery, and indexing surfaces.

    Both lexical and symlink-resolved paths are checked.  A lexical path under
    the runs root that resolves outside it is rejected with ``ValueError`` so
    production discovery callers can fail closed instead of following an
    escaping symlink.
    """

    lexical_root = _lexical_absolute(runs_root, label="runs_root")
    lexical_path = _lexical_absolute(path, label="path")
    lexical_relative = _relative_if_inside(lexical_path, lexical_root)

    # Keep the reserved lexical namespace closed even when the directory is a
    # symlink to a location outside runs_root.
    if _relative_is_reserved(lexical_relative):
        return True

    resolved_root = _resolved(lexical_root, label="runs_root")
    resolved_path = _resolved(lexical_path, label="path")
    resolved_relative = _relative_if_inside(resolved_path, resolved_root)
    if _relative_is_reserved(resolved_relative):
        return True

    if lexical_relative is not None and resolved_relative is None:
        raise ValueError(
            "Path escapes runs_root through a symlink: "
            f"path={lexical_path} runs_root={lexical_root}"
        )
    return False


def should_exclude_from_production_runs_scan(
    path: str | Path,
    runs_root: str | Path,
) -> bool:
    """Fail-closed production scan filter for reserved or unsafe paths."""

    try:
        if is_path_in_reserved_smoke_tree(path, runs_root):
            return True
        lexical_root = _lexical_absolute(runs_root, label="runs_root")
        lexical_path = _lexical_absolute(path, label="path")
        lexical_relative = _relative_if_inside(lexical_path, lexical_root)
        if _relative_is_visible_generation(lexical_relative):
            return True
        resolved_root = _resolved(lexical_root, label="runs_root")
        resolved_path = _resolved(lexical_path, label="path")
        return _relative_is_visible_generation(_relative_if_inside(resolved_path, resolved_root))
    except ValueError:
        return True


def iter_production_runs_artifacts(
    runs_root: str | Path,
    filename: str,
) -> Iterator[Path]:
    """Yield named artifacts while pruning the reserved smoke subtree.

    Top-level symlink directories are not traversed.  Matching symlink files
    are passed through the fail-closed path filter before they can be opened by
    a caller.
    """

    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise ValueError(f"artifact filename must be one path segment: {filename!r}")

    root = _lexical_absolute(runs_root, label="runs_root")
    try:
        if not root.is_dir():
            return
        root_artifact = root / filename
        if (root_artifact.exists() or root_artifact.is_symlink()) and not (
            should_exclude_from_production_runs_scan(root_artifact, root)
        ):
            yield root_artifact
        children = tuple(root.iterdir())
    except OSError:
        return

    for child in children:
        if child.name == SMOKE_RESULTS_DIRNAME:
            continue
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            for artifact in child.rglob(filename):
                if should_exclude_from_production_runs_scan(artifact, root):
                    continue
                yield artifact
        except OSError:
            continue


__all__ = [
    "SMOKE_RESULTS_DIRNAME",
    "iter_production_runs_artifacts",
    "is_path_in_reserved_smoke_tree",
    "should_exclude_from_production_runs_scan",
]
