from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.config import engines as _config_engines
from orca_auto.core.engine_scratch import SCRATCH_RUNTIME_HOME_DIR_NAME
from orca_auto.core.paths import validate_configured_executable_path
from orca_auto.core.utils.persistence import fsync_directory

_INTEGER_TEXT_RE = re.compile(r"[-+]?\d+")
_RUNTIME_ENV_KEYS = ("PATH", "LD_LIBRARY_PATH", "XTBPATH", "XTBHOME")
_XTB_SOLVENTS = frozenset(
    {
        "acetone",
        "acetonitrile",
        "aniline",
        "benzene",
        "benzaldehyde",
        "ch2cl2",
        "chcl3",
        "chloroform",
        "cs2",
        "dmf",
        "dmso",
        "dioxane",
        "dichlormethane",
        "ether",
        "ethanol",
        "ethylacetate",
        "furane",
        "hexadecane",
        "hexane",
        "h2o",
        "methanol",
        "nitromethane",
        "nhexan",
        "n-hexan",
        "nhexane",
        "n-hexane",
        "octanol",
        "phenol",
        "thf",
        "toluene",
        "water",
        "woctanol",
    }
)


def resolve_configured_executable(
    cfg: Any,
    *,
    path_attr: str,
    executable_name: str,
    display_name: str,
) -> str:
    configured = str(getattr(cfg.paths, path_attr, "")).strip()
    if configured:
        path = validate_configured_executable_path(
            configured,
            label=f"Configured {display_name} executable",
            display_name=display_name,
        )
        return str(path)

    discovered = shutil.which(executable_name)
    if discovered:
        return discovered
    raise ValueError(f"{display_name} executable not configured and not found on PATH.")


def executable_identity(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    flags = os.O_RDONLY
    flags |= os.O_NONBLOCK
    flags |= os.O_NOFOLLOW
    descriptor = os.open(resolved, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Engine executable is not a regular file: {resolved}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"Engine executable changed while it was hashed: {resolved}")
        return {
            "path": str(resolved),
            "sha256": digest.hexdigest(),
            "size_bytes": after.st_size,
        }
    finally:
        os.close(descriptor)


def verify_executable_identity(identity: Any) -> str:
    if not isinstance(identity, dict):
        raise ValueError("Queued execution snapshot has no executable identity")
    current = executable_identity(str(identity.get("path") or ""))
    if current != identity:
        raise ValueError("Queued engine executable no longer matches its submitted identity")
    return str(current["path"])


def confined_output_identity(root: str | Path, path: str | Path) -> dict[str, Any]:
    resolved_root = Path(root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"Engine output must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"Engine output must stay inside its job directory: {candidate}")
    flags = os.O_RDONLY
    flags |= os.O_NONBLOCK
    flags |= os.O_NOFOLLOW
    descriptor = os.open(resolved, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"Engine output must be a single-link regular file: {resolved}")
        digest = hashlib.sha256()
        total_bytes = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            total_bytes += len(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or total_bytes != after.st_size:
            raise ValueError(f"Engine output changed while it was hashed: {resolved}")
        os.fsync(descriptor)
        synced = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            synced.st_dev,
            synced.st_ino,
            synced.st_size,
            synced.st_mtime_ns,
        ):
            raise ValueError(f"Engine output changed while it was synchronized: {resolved}")
    finally:
        os.close(descriptor)
    fsync_directory(resolved.parent)
    return {
        "path": str(resolved),
        "sha256": digest.hexdigest(),
        "size_bytes": total_bytes,
    }


def verify_confined_output_identity(
    root: str | Path,
    identity: Any,
    *,
    path_override: str | Path | None = None,
) -> Path:
    if not isinstance(identity, dict):
        raise ValueError("Engine output has no content identity")
    current = confined_output_identity(
        root,
        path_override if path_override is not None else str(identity.get("path") or ""),
    )
    if (
        current["sha256"] != identity.get("sha256")
        or current["size_bytes"] != identity.get("size_bytes")
        or (path_override is None and current["path"] != identity.get("path"))
    ):
        raise ValueError("Engine output no longer matches its terminal content identity")
    return Path(current["path"])


def engine_runtime_identity(job_dir: str | Path) -> dict[str, Any]:
    """Capture the small, explicit environment used by xTB-family engines."""

    resolved_job_dir = Path(job_dir).expanduser().resolve()
    runtime_home = resolved_job_dir / ".orca_auto_runtime_home"
    environment = {
        key: os.environ[key]
        for key in _RUNTIME_ENV_KEYS
        if isinstance(os.environ.get(key), str) and os.environ[key]
    }
    environment.update(
        {
            "HOME": str(runtime_home),
            "XDG_CONFIG_HOME": str(runtime_home / ".config"),
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    external_parameter_roots = {
        key: value for key, value in environment.items() if key in {"XTBPATH", "XTBHOME"}
    }
    return {
        "environment": environment,
        "external_parameter_roots": external_parameter_roots,
        "external_parameter_content_bound": False,
    }


def rebase_engine_runtime_identity(
    runtime_identity: Any,
    job_dir: str | Path,
) -> dict[str, Any]:
    if not isinstance(runtime_identity, dict) or not isinstance(
        runtime_identity.get("environment"), dict
    ):
        raise ValueError("Engine child has no captured runtime identity")
    rebased = {
        **runtime_identity,
        "environment": dict(runtime_identity["environment"]),
    }
    runtime_home = Path(job_dir).expanduser().resolve() / ".orca_auto_runtime_home"
    rebased["environment"].update(
        {
            "HOME": str(runtime_home),
            "XDG_CONFIG_HOME": str(runtime_home / ".config"),
        }
    )
    return rebased


def verified_engine_runtime_environment(
    job_dir: str | Path,
    runtime_identity: Any,
) -> dict[str, str]:
    """Validate and materialize the captured clean engine environment."""

    if not isinstance(runtime_identity, dict):
        raise ValueError("Queued execution snapshot has no runtime identity")
    raw_environment = runtime_identity.get("environment")
    if not isinstance(raw_environment, dict):
        raise ValueError("Queued execution snapshot has no runtime environment")
    allowed_keys = {*_RUNTIME_ENV_KEYS, "HOME", "XDG_CONFIG_HOME", "LC_ALL", "LANG"}
    if set(raw_environment) - allowed_keys:
        raise ValueError("Queued execution snapshot has unsupported runtime environment fields")
    environment: dict[str, str] = {}
    for key, value in raw_environment.items():
        if not isinstance(key, str) or not isinstance(value, str) or "\x00" in value:
            raise ValueError("Queued runtime environment must contain plain strings")
        environment[key] = value
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    expected_home = resolved_job_dir / ".orca_auto_runtime_home"
    if Path(environment.get("HOME") or "").expanduser().resolve() != expected_home:
        raise ValueError("Queued runtime HOME does not match the engine job directory")
    if Path(environment.get("XDG_CONFIG_HOME") or "").expanduser().resolve() != (
        expected_home / ".config"
    ):
        raise ValueError("Queued runtime XDG_CONFIG_HOME does not match the engine job directory")
    from orca_auto.core.engine_process import ensure_confined_directory, recreate_confined_directory

    recreate_confined_directory(
        resolved_job_dir,
        expected_home,
        label="engine runtime home",
    )
    ensure_confined_directory(
        resolved_job_dir,
        expected_home / ".config",
        label="engine runtime configuration directory",
    )
    return environment


def verified_runtime_environment_for_job(
    job_dir: str | Path,
    *,
    manifest: dict[str, Any],
    execution_snapshot: dict[str, Any] | None,
    display_name: str,
) -> dict[str, str]:
    """Resolve the runtime identity for a job and materialize its environment.

    Queued submissions must match the identity captured in their immutable
    manifest; direct runs fall back to the manifest's recorded identity or a
    freshly captured one.
    """

    runtime_identity = (
        execution_snapshot.get("runtime_identity")
        if execution_snapshot is not None
        else manifest.get("_orca_auto_runtime_identity")
    )
    if runtime_identity is None:
        runtime_identity = engine_runtime_identity(job_dir)
    if execution_snapshot is not None and runtime_identity != manifest.get(
        "_orca_auto_runtime_identity"
    ):
        raise ValueError(f"Queued {display_name} manifest has a mismatched runtime identity")
    return verified_engine_runtime_environment(job_dir, runtime_identity)


def scratch_engine_runtime_environment(
    scratch_dir: str | Path,
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Move engine-owned HOME state into one private scratch workspace."""

    allowed_keys = {*_RUNTIME_ENV_KEYS, "HOME", "XDG_CONFIG_HOME", "LC_ALL", "LANG"}
    if set(environment) - allowed_keys:
        raise ValueError("Engine runtime environment has unsupported fields")
    rebased: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str) or "\x00" in value:
            raise ValueError("Engine runtime environment must contain plain strings")
        rebased[key] = value

    resolved_scratch_dir = Path(scratch_dir).expanduser().resolve()
    if not resolved_scratch_dir.is_dir():
        raise ValueError("Engine scratch workspace must be an existing directory")
    from orca_auto.core.engine_process import (
        ensure_confined_directory,
        recreate_confined_directory,
    )

    runtime_home = recreate_confined_directory(
        resolved_scratch_dir,
        resolved_scratch_dir / SCRATCH_RUNTIME_HOME_DIR_NAME,
        label="engine scratch runtime home",
    )
    runtime_config = ensure_confined_directory(
        resolved_scratch_dir,
        runtime_home / ".config",
        label="engine scratch runtime configuration directory",
    )
    runtime_home.chmod(0o700)
    runtime_config.chmod(0o700)
    rebased.update(
        {
            "HOME": str(runtime_home),
            "XDG_CONFIG_HOME": str(runtime_config),
        }
    )
    return rebased


def resource_actual_dict(resource_request: dict[str, int]) -> dict[str, int]:
    return _config_engines.resource_actual_from_request(resource_request)


def bool_flag(manifest: dict[str, Any], key: str) -> bool:
    if key not in manifest or manifest.get(key) is None:
        return False
    value = manifest.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"Manifest field {key!r} must be a boolean-compatible value.")


def manifest_int(
    manifest: dict[str, Any],
    key: str,
) -> int | None:
    value = manifest.get(key)
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"Manifest field {key!r} must be an integer, not a boolean.")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if not _INTEGER_TEXT_RE.fullmatch(stripped):
            raise ValueError(f"Manifest field {key!r} must be an integer-compatible value.")
        return int(stripped)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError(f"Manifest field {key!r} must be an integer-compatible value.")


def validated_solvent_option(manifest: dict[str, Any]) -> tuple[str, str] | None:
    """Validate the solvent_model/solvent pair; return it, or None when unset."""

    solvent_model = str(manifest.get("solvent_model", "")).strip().lower()
    solvent = str(manifest.get("solvent", "")).strip().lower()
    if solvent_model and solvent_model not in {"gbsa", "alpb"}:
        raise ValueError("Manifest field 'solvent_model' must be 'gbsa' or 'alpb'.")
    if solvent and not solvent_model:
        raise ValueError("Manifest field 'solvent' requires 'solvent_model'.")
    if solvent_model and not solvent:
        raise ValueError("Manifest field 'solvent_model' requires 'solvent'.")
    if solvent and solvent not in _XTB_SOLVENTS:
        raise ValueError("Manifest field 'solvent' must be one documented xTB solvent name.")
    if solvent:
        return solvent_model, solvent
    return None


def append_solvent_option(command: list[str], manifest: dict[str, Any]) -> None:
    option = validated_solvent_option(manifest)
    if option is not None:
        solvent_model, solvent = option
        command.extend([f"--{solvent_model}", solvent])


__all__ = [
    "append_solvent_option",
    "bool_flag",
    "executable_identity",
    "manifest_int",
    "resolve_configured_executable",
    "resource_actual_dict",
    "validated_solvent_option",
    "verify_executable_identity",
]
