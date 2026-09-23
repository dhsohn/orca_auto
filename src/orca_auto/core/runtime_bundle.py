"""Identity and byte verification for a prepared, read-only wheel runtime."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any

RUNTIME_MANIFEST_NAME = "orca_auto_runtime.json"
PROCESS_RUNTIME_BUILD_ENV = "ORCA_AUTO_PROCESS_RUNTIME_BUILD"


def content_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime_build_id(identity: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def runtime_inventory(root: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == RUNTIME_MANIFEST_NAME:
            continue
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError(f"runtime symlink escapes its installation: {relative}")
            records[relative] = {"link": str(path.readlink())}
        elif path.is_dir():
            records[relative] = {"directory": True, "mode": mode}
        elif path.is_file():
            records[relative] = {"sha256": content_sha256(path), "mode": mode}
        else:
            raise ValueError(f"unsupported runtime file: {relative}")
        if not path.is_symlink() and mode & 0o222:
            raise ValueError(f"runtime path is writable: {relative}")
    return records


def verify_runtime_bundle(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    manifest_path = root / RUNTIME_MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read runtime manifest: {root}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("state") != "ready"
    ):
        raise ValueError(f"runtime preparation is incomplete or unsupported: {root}")
    identity = manifest.get("identity")
    if (
        not isinstance(identity, dict)
        or not isinstance(identity.get("version"), str)
        or not identity["version"]
        or manifest.get("build_id") != runtime_build_id(identity)
    ):
        raise ValueError(f"runtime build identity is invalid: {root}")
    if manifest.get("runtime_root") != str(root):
        raise ValueError("prepared runtimes cannot be moved or renamed")
    if root.stat().st_mode & 0o222 or manifest_path.stat().st_mode & 0o222:
        raise ValueError(f"runtime root or manifest is writable: {root}")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected or runtime_inventory(root) != expected:
        raise ValueError(f"runtime installed files differ from the prepared build: {root}")
    return manifest


def runtime_root_for_import_source(source: Path) -> Path | None:
    for parent in source.resolve(strict=True).parents:
        if parent.name == ".venv":
            root = parent.parent
            if (root / RUNTIME_MANIFEST_NAME).exists():
                return root
            return None
    return None
