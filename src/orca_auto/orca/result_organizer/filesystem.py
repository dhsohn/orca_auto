from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from typing import Any

from .models import OrganizePlan


def check_conflict(
    plan: OrganizePlan,
    index: dict[str, dict[str, Any]],
) -> str | None:
    existing = index.get(plan.run_id)
    if existing is not None:
        if existing.get("organized_path") == plan.target_rel_path:
            return "already_organized"
        return "index_conflict"
    if plan.target_abs_path.exists():
        return "path_occupied"
    return None


def _verify_copytree(source: Path, target: Path) -> None:
    """Verify that all source files exist in the target after copytree."""
    for src_file in source.rglob("*"):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(source)
        dst_file = target / rel
        if not dst_file.exists():
            raise RuntimeError(f"Cross-device copy verification failed: missing {dst_file}")
        if dst_file.stat().st_size != src_file.stat().st_size:
            raise RuntimeError(
                f"Cross-device copy verification failed: size mismatch for {rel} "
                f"(source={src_file.stat().st_size}, target={dst_file.stat().st_size})"
            )


def _cross_device_move(source: Path, target: Path) -> None:
    """Copy, verify, then remove for a safe cross-device move."""
    shutil.copytree(str(source), str(target))
    _fsync_directory(target.parent)
    _verify_copytree(source, target)
    shutil.rmtree(str(source))


def execute_move(plan: OrganizePlan) -> None:
    plan.target_abs_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(str(plan.source_dir), str(plan.target_abs_path))
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _cross_device_move(plan.source_dir, plan.target_abs_path)


def rollback_move(plan: OrganizePlan) -> None:
    if not plan.target_abs_path.exists():
        return
    if plan.source_dir.exists():
        raise RuntimeError(f"Rollback blocked: source already exists: {plan.source_dir}")
    plan.source_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(str(plan.target_abs_path), str(plan.source_dir))
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _cross_device_move(plan.target_abs_path, plan.source_dir)


def _fsync_directory(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
