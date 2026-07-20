from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from orca_auto.core.paths.workflow import directory_is_workflow_scaffold
from orca_auto.core.queue.generation import is_visible_generation_name


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


def relative_reaches_reserved_generation(root: Path, relative: Path | None) -> bool:
    """True when *relative* (under *root*) crosses an ORCA execution generation.

    Workflow workspaces share the generation name shape but carry a
    ``workflow.json`` and sit either directly under root (direct API
    submissions) or inside a scaffold (``flow.yaml``); those are legitimate
    scan/submission surfaces. Every other generation-named component —
    including execution generations inside standalone ORCA job dirs or
    inside a workspace's stage job dirs — stays reserved, even if a
    ``workflow.json`` file was planted there.
    """

    if relative is None:
        return False
    current = root
    for component in relative.parts:
        parent = current
        current = current / component
        if not is_visible_generation_name(component):
            continue
        is_workspace = (current / "workflow.json").is_file() and (
            parent == root or directory_is_workflow_scaffold(parent)
        )
        if not is_workspace:
            return True
    return False


def should_exclude_from_production_runs_scan(
    path: str | Path,
    runs_root: str | Path,
) -> bool:
    """Fail-closed production scan filter for unsafe or reserved-generation paths."""

    try:
        lexical_root = _lexical_absolute(runs_root, label="runs_root")
        lexical_path = _lexical_absolute(path, label="path")
        lexical_relative = _relative_if_inside(lexical_path, lexical_root)
        if relative_reaches_reserved_generation(lexical_root, lexical_relative):
            return True
        resolved_root = _resolved(lexical_root, label="runs_root")
        resolved_path = _resolved(lexical_path, label="path")
        resolved_relative = _relative_if_inside(resolved_path, resolved_root)
        # A path that is lexically under runs_root but resolves outside it is
        # escaping through a symlink; fail closed rather than scan it as a
        # production artifact.
        if lexical_relative is not None and resolved_relative is None:
            return True
        return relative_reaches_reserved_generation(resolved_root, resolved_relative)
    except ValueError:
        return True


def iter_production_runs_artifacts(
    runs_root: str | Path,
    filename: str,
) -> Iterator[Path]:
    """Yield named artifacts while pruning reserved execution-generation subtrees.

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
    "iter_production_runs_artifacts",
    "relative_reaches_reserved_generation",
    "should_exclude_from_production_runs_scan",
]
