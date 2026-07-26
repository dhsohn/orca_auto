"""Utility for discovering files to be indexed for DFT."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.paths import (
    iter_production_runs_artifacts,
    path_is_inside_workflow_workspace,
    should_exclude_from_production_runs_scan,
)
from orca_auto.core.queue.engine.input_snapshot import (
    MAX_INPUT_SNAPSHOT_BYTES,
    read_stable_regular_file,
    require_direct_generation_owner,
)
from orca_auto.core.queue.generation import is_visible_generation_name

logger = logging.getLogger(__name__)

_ORCA_EXTENSIONS = {".out"}


@dataclass
class DiscoveredTarget:
    """Discovered ORCA output file with job_state metadata."""

    path: Path
    run_state_status: str = ""


def discover_orca_targets(
    kb_path: Path,
    *,
    max_bytes: int,
) -> list[DiscoveredTarget]:
    """Return a list of ORCA output files to be indexed.

    Discovery is based on job_state.json:
    - Status is trusted only from job_state.status
    - A public state selecting its direct visible generation tracks that
      generation's latest .out; all other states track their own folder
    - Mirrored state inside a visible generation is never a separate job
    - Workflow workspaces under the same runs root are skipped; their jobs
      are reported through the workflow views instead
    """
    targets: dict[str, DiscoveredTarget] = {}

    for state_path in iter_production_runs_artifacts(kb_path, "job_state.json"):
        if should_exclude_from_production_runs_scan(state_path, kb_path):
            continue
        if path_is_inside_workflow_workspace(state_path.parent, kb_path):
            continue
        data = _load_report_json(state_path)
        if not isinstance(data, dict):
            continue

        status = _state_status(data)

        generation_dir = _selected_visible_generation_dir(
            state_path=state_path,
            data=data,
            runs_root=kb_path,
        )
        if generation_dir is None:
            continue
        # Visible generations are deliberately pruned from production scans so
        # their mirrored state is not indexed as another job. The public root
        # state is the sole authority for this exception.
        resolved = _find_latest_out_in_dir(
            generation_dir,
            require_direct_regular_files=True,
        )
        if resolved is None:
            continue
        _add_if_valid_target(
            resolved=resolved,
            max_bytes=max_bytes,
            targets=targets,
            run_state_status=status,
        )

    return sorted(targets.values(), key=lambda t: str(t.path))


def _selected_input_text(data: dict[str, Any]) -> str:
    selected = str(data.get("selected_inp") or "").strip()
    if selected:
        return selected
    input_payload = data.get("input")
    if isinstance(input_payload, dict):
        return str(input_payload.get("primary_path") or "").strip()
    return ""


def _execution_provenance(data: dict[str, Any]) -> dict[str, Any]:
    provenance = data.get("execution_provenance")
    if isinstance(provenance, dict):
        return provenance
    engine_payload = data.get("engine_payload")
    if isinstance(engine_payload, dict):
        provenance = engine_payload.get("execution_provenance")
        if isinstance(provenance, dict):
            return provenance
    return {}


def _matches_bound_selected_identity(
    selected: Path,
    identity: Any,
) -> bool:
    if not isinstance(identity, dict) or set(identity) != {
        "path",
        "sha256",
        "size_bytes",
    }:
        return False
    expected_path = identity.get("path")
    expected_sha256 = identity.get("sha256")
    expected_size = identity.get("size_bytes")
    if (
        not isinstance(expected_path, str)
        or expected_path != str(selected)
        or not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 0
        or expected_size > MAX_INPUT_SNAPSHOT_BYTES
    ):
        return False
    try:
        payload = read_stable_regular_file(
            selected,
            max_bytes=max(expected_size, 1),
            require_single_link=True,
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return len(payload) == expected_size and hashlib.sha256(payload).hexdigest() == expected_sha256


def _selected_visible_generation_dir(
    *,
    state_path: Path,
    data: dict[str, Any],
    runs_root: Path,
) -> Path | None:
    selected_text = _selected_input_text(data)
    provenance = _execution_provenance(data)
    execution_dir_text = str(provenance.get("execution_dir") or "").strip()
    raw_identity = provenance.get("execution_dir_identity")
    bound_selected_identity = provenance.get("bound_selected_identity")
    generation_owner_token = str(provenance.get("generation_owner_token") or "").strip()

    if (
        not selected_text
        or not execution_dir_text
        or not generation_owner_token
        or not isinstance(raw_identity, dict)
    ):
        return None

    device = raw_identity.get("device")
    inode = raw_identity.get("inode")
    if (
        isinstance(device, bool)
        or not isinstance(device, int)
        or device < 0
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or inode <= 0
    ):
        return None

    selected = Path(selected_text)
    raw_generation_dir = Path(execution_dir_text)
    if (
        not selected.is_absolute()
        or selected.suffix.lower() != ".inp"
        or not raw_generation_dir.is_absolute()
        or not is_visible_generation_name(raw_generation_dir.name)
    ):
        return None

    public_job_dir = state_path.parent

    try:
        if (
            raw_generation_dir.is_symlink()
            or not raw_generation_dir.is_dir()
            or selected.is_symlink()
            or not selected.is_file()
        ):
            return None
        resolved_root = runs_root.resolve(strict=True)
        resolved_job_dir = public_job_dir.resolve(strict=True)
        resolved_generation_dir = raw_generation_dir.resolve(strict=True)
        resolved_selected = selected.resolve(strict=True)
        generation_status = raw_generation_dir.lstat()
        job_status = resolved_job_dir.stat()
        resolved_job_dir.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None

    if (
        raw_generation_dir != resolved_generation_dir
        or resolved_generation_dir.parent != resolved_job_dir
        or selected != resolved_selected
        or resolved_selected.parent != resolved_generation_dir
        or (int(generation_status.st_dev), int(generation_status.st_ino)) != (device, inode)
        or not _matches_bound_selected_identity(resolved_selected, bound_selected_identity)
    ):
        return None

    try:
        require_direct_generation_owner(
            resolved_job_dir,
            namespace=resolved_generation_dir.name,
            expected_job_identity=(int(job_status.st_dev), int(job_status.st_ino)),
            expected_generation_identity=(device, inode),
            owner_token=generation_owner_token,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None

    # iter_production_runs_artifacts preserves the caller's lexical runs-root
    # alias, while runtime state stores resolved physical paths.  Rebase the
    # proven direct generation onto the public job path so a safely symlinked
    # runs root keeps its existing discovery behavior.
    public_generation_dir = public_job_dir / raw_generation_dir.name
    public_selected = public_generation_dir / selected.name
    try:
        if public_generation_dir.is_symlink() or public_selected.is_symlink():
            return None
        if (
            public_generation_dir.resolve(strict=True) != resolved_generation_dir
            or public_selected.resolve(strict=True) != resolved_selected
        ):
            return None
    except (OSError, RuntimeError):
        return None
    return public_generation_dir


def _find_latest_out_in_dir(
    directory: Path,
    *,
    require_direct_regular_files: bool = False,
) -> Path | None:
    if not directory.is_dir():
        return None
    resolved_directory: Path | None = None
    if require_direct_regular_files:
        try:
            if directory.is_symlink():
                return None
            resolved_directory = directory.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
    latest: tuple[float, Path] | None = None
    for candidate in directory.glob("*.out"):
        if require_direct_regular_files:
            try:
                if candidate.is_symlink() or candidate.resolve(strict=True).parent != (
                    resolved_directory
                ):
                    continue
            except (OSError, RuntimeError):
                continue
        if not candidate.is_file():
            continue
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if latest is None or mtime > latest[0]:
            latest = (mtime, candidate)
    return latest[1] if latest is not None else None


def _state_status(data: dict[str, Any]) -> str:
    status = data.get("status")
    if isinstance(status, dict):
        return str(status.get("state", "")).strip().lower()
    return str(status or "").strip().lower()


def _add_if_valid_target(
    *,
    resolved: Path,
    max_bytes: int,
    targets: dict[str, DiscoveredTarget],
    run_state_status: str = "",
) -> None:
    if resolved.suffix.lower() not in _ORCA_EXTENSIONS:
        return
    try:
        if resolved.stat().st_size > max_bytes:
            return
    except OSError:
        return
    targets[str(resolved)] = DiscoveredTarget(
        path=resolved,
        run_state_status=run_state_status,
    )


def _load_report_json(report_path: Path) -> dict[str, Any] | None:
    try:
        with open(report_path, encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "dft_job_report_parse_failed: path=%s error=%s",
            report_path,
            exc,
        )
        return None
