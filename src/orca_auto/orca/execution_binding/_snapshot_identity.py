"""Identity of the files, executable and generation directory pinned by a snapshot."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.engine_process import require_confined_regular_file
from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.core.queue.engine.snapshot_intent import SNAPSHOT_INTENT_TOKEN_KEY
from orca_auto.core.queue.generation import is_visible_generation_name

from ._constants import MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES


def _file_identity(path: Path) -> dict[str, Any]:
    identity = _engine_runner.executable_identity(path)
    return {
        "path": identity["path"],
        "sha256": identity["sha256"],
        "size_bytes": identity["size_bytes"],
    }


def orca_execution_provenance(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the durable execution identity attached to ORCA run artifacts."""

    execution_dir_identity = snapshot.get("execution_dir_identity")
    if not isinstance(execution_dir_identity, Mapping):
        raise ValueError("ORCA execution snapshot has no generation directory identity")
    return {
        "execution_dir": str(snapshot.get("execution_dir") or ""),
        "execution_dir_identity": {
            "device": int(execution_dir_identity.get("device", -1)),
            "inode": int(execution_dir_identity.get("inode", -1)),
        },
        "generation_owner_token": str(snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY) or ""),
        "source_selected_inp": str(snapshot.get("source_selected_inp") or ""),
        "bound_selected_identity": dict(snapshot.get("bound_selected_identity") or {}),
        "materialized_inputs": dict(snapshot.get("materialized_inputs") or {}),
        "runtime_mutable_input_roles": list(snapshot.get("runtime_mutable_input_roles") or []),
        "executable_identity": dict(
            (snapshot.get("executable_identities") or {}).get("orca") or {}
        ),
    }


def verify_orca_snapshot_executable(
    snapshot: Mapping[str, Any],
    *,
    expected_executable: str | Path | None = None,
) -> str:
    """Verify and return the executable identity pinned by one snapshot."""

    executable_identities = snapshot.get("executable_identities")
    if not isinstance(executable_identities, Mapping):
        raise ValueError("Queued ORCA execution snapshot has no executable identities")
    identity = executable_identities.get("orca")
    if not isinstance(identity, dict):
        raise ValueError("Queued ORCA execution snapshot has no ORCA executable identity")
    if expected_executable is None:
        return _engine_runner.verify_executable_identity(identity)
    current = _engine_runner.executable_identity(expected_executable)
    if current != identity:
        raise ValueError("ORCA crash recovery executable does not match the submitted identity")
    return str(current["path"])


def orca_execution_snapshot_generation_dir(
    job_dir: str | Path,
    snapshot: Any,
) -> Path:
    """Return the exact visible generation owned by one ORCA submission."""

    if not isinstance(snapshot, Mapping):
        raise ValueError("ORCA execution snapshot must be an object")
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    execution_text = str(snapshot.get("execution_dir") or "").strip()
    raw_execution_dir = Path(execution_text).expanduser()
    if not execution_text or not raw_execution_dir.is_absolute():
        raise ValueError("Queued ORCA execution directory is invalid")
    execution_dir = raw_execution_dir.resolve()
    if (
        raw_execution_dir.is_symlink()
        or not execution_dir.is_dir()
        or execution_dir.parent != resolved_job_dir
        or not is_visible_generation_name(execution_dir.name)
        or str(snapshot.get("generation_name") or "") != execution_dir.name
    ):
        raise ValueError("Queued ORCA execution directory escapes its job directory")
    raw_identity = snapshot.get("execution_dir_identity")
    if not isinstance(raw_identity, Mapping):
        raise ValueError("Queued ORCA generation has no directory identity")
    details = execution_dir.stat()
    if (int(details.st_dev), int(details.st_ino)) != (
        int(raw_identity.get("device", -1)),
        int(raw_identity.get("inode", -1)),
    ):
        raise ValueError("Queued ORCA generation directory identity changed")
    return execution_dir


def _verify_identity(identity: Any, *, root: Path, label: str) -> Path:
    if not isinstance(identity, Mapping):
        raise ValueError(f"Queued ORCA execution snapshot has no {label} identity")
    path = require_confined_regular_file(
        root,
        Path(str(identity.get("path") or "")).expanduser(),
        label=f"Queued ORCA {label} snapshot",
    )
    current = _file_identity(path)
    if current != dict(identity):
        raise ValueError(f"Queued ORCA {label} snapshot is corrupt")
    return path


def _verified_identity_payload(
    identity: Any,
    *,
    root: Path,
    label: str,
) -> tuple[Path, bytes]:
    """Read identity-bound input bytes once and verify those exact bytes."""

    if not isinstance(identity, Mapping):
        raise ValueError(f"Queued ORCA execution snapshot has no {label} identity")
    path = require_confined_regular_file(
        root,
        Path(str(identity.get("path") or "")).expanduser(),
        label=f"Queued ORCA {label} snapshot",
    )
    payload = read_stable_regular_file(
        path,
        # The bound input may be larger than either source file after a
        # same-stem XYZ dependency is inlined. Construction already caps this
        # rewritten artifact against the aggregate snapshot budget.
        max_bytes=MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES,
        require_single_link=True,
    )
    current = {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    if current != dict(identity):
        raise ValueError(f"Queued ORCA {label} snapshot is corrupt")
    return path, payload
