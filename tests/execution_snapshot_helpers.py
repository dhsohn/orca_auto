from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner
from orca_auto.core.queue.engine.input_snapshot import snapshot_input_file, snapshot_input_payload


def stage_execution_snapshot(
    job_dir: Path,
    selected_input: Path,
    *,
    engine: str,
    manifest: dict[str, Any],
    resource_request: dict[str, int],
    identity: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    selected_descriptor = snapshot_input_file(job_dir, selected_input, role="selected")
    selected_snapshot = Path(selected_descriptor["snapshot_path"])
    normalized_manifest = {**manifest, "resources": dict(resource_request)}
    executable_names = ("xtb",) if engine == "xtb" else ("crest", "xtb")
    executable_identities: dict[str, dict[str, Any]] = {}
    for executable_name in executable_names:
        executable_path = job_dir / f".{executable_name}_test_executable"
        if not executable_path.exists():
            executable_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable_path.chmod(0o700)
        executable_identities[executable_name] = engine_runner.executable_identity(executable_path)
        normalized_manifest[f"_orca_auto_{executable_name}_executable"] = executable_identities[
            executable_name
        ]["path"]
    manifest_descriptor = snapshot_input_payload(
        job_dir,
        json.dumps(
            normalized_manifest,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
        role="manifest",
        suffix=".json",
        source_path=job_dir / f"{engine}_job.yaml",
    )
    snapshot = {
        "version": 1,
        "manifest": normalized_manifest,
        "input_snapshots": {
            "selected": selected_descriptor,
            "manifest": manifest_descriptor,
        },
        "selected_input_xyz": str(selected_snapshot),
        "resource_request": dict(resource_request),
        "manifest_path": manifest_descriptor["snapshot_path"],
        "executable_identities": executable_identities,
        **identity,
    }
    if selected_input != selected_snapshot:
        selected_input.unlink()
        selected_input.symlink_to(selected_snapshot)
    return selected_snapshot, snapshot


__all__ = ["stage_execution_snapshot"]
