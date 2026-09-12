from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.flow.xyz_utils import (
    load_output_xyz_frames,
    load_xyz_atom_sequence,
)


def _resolve_existing_path(job_dir: Path, path_text: str) -> str:
    candidate = Path(path_text).expanduser()
    if not candidate.is_absolute():
        candidate = job_dir / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return ""
    if (
        not resolved.is_relative_to(job_dir.expanduser().resolve())
        or not resolved.exists()
        or not resolved.is_file()
    ):
        return ""
    return str(resolved)


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _load_xtbout_json(job_dir: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(
            read_stable_regular_file(job_dir / "xtbout.json").decode(
                "utf-8",
                errors="strict",
            )
        )
    except (OSError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _extract_sp_energy(job_dir: Path, candidate_xyz: Path) -> tuple[float | None, str]:
    del candidate_xyz
    xtbout = _load_xtbout_json(job_dir)
    for key in ("total energy", "electronic energy"):
        value = _safe_float(xtbout.get(key))
        if value is not None:
            return value, f"xtbout.json:{key}"
    return None, ""


def _collect_opt_candidates(
    job_dir: Path,
    *,
    selected_input_xyz: str | Path | None = None,
) -> tuple[int, tuple[str, ...], tuple[dict[str, Any], ...], dict[str, Any]]:
    optimized_geometry = _resolve_existing_path(job_dir, "xtbopt.xyz")
    summary = {
        "canonical_result_path": optimized_geometry,
        "optimization_log_path": _resolve_existing_path(job_dir, "xtbopt.log"),
        "optimization_ok": (job_dir / ".xtboptok").exists(),
    }
    optimized_frames = load_output_xyz_frames(optimized_geometry) if optimized_geometry else ()
    validation_error = ""
    if not summary["optimization_ok"]:
        validation_error = "xTB optimization did not emit the .xtboptok success marker"
    elif len(optimized_frames) != 1:
        validation_error = "xtbopt.xyz must contain exactly one valid finite XYZ frame"
    elif selected_input_xyz is not None:
        try:
            expected_atoms = tuple(
                atom.casefold() for atom in load_xyz_atom_sequence(selected_input_xyz)
            )
        except (OSError, ValueError) as exc:
            validation_error = f"selected xTB input is invalid: {exc}"
        else:
            optimized_atoms = tuple(
                line.split()[0].casefold() for line in optimized_frames[0].atom_lines
            )
            if optimized_atoms != expected_atoms:
                validation_error = (
                    "xtbopt.xyz atom count or element order does not match the selected input"
                )
    if not optimized_geometry or validation_error:
        if optimized_geometry:
            summary["result_validation_error"] = validation_error
            summary["canonical_result_path"] = ""
        return 0, (), (), summary
    detail = {
        "rank": 1,
        "kind": "optimized_geometry",
        "path": optimized_geometry,
        "score": 1000.0,
        "selected": True,
    }
    return 1, (optimized_geometry,), (detail,), summary


def _collect_sp_candidates(
    job_dir: Path,
) -> tuple[int, tuple[str, ...], tuple[dict[str, Any], ...], dict[str, Any]]:
    result_json = _resolve_existing_path(job_dir, "xtbout.json")
    xtbout = _load_xtbout_json(job_dir)
    summary: dict[str, Any] = {
        "canonical_result_path": result_json,
        "charges_path": _resolve_existing_path(job_dir, "charges"),
        "wbo_path": _resolve_existing_path(job_dir, "wbo"),
        "topology_path": _resolve_existing_path(job_dir, "xtbtopo.mol"),
    }
    for source_key, summary_key in (
        ("total energy", "total_energy"),
        ("electronic energy", "electronic_energy"),
    ):
        parsed = _safe_float(xtbout.get(source_key))
        if parsed is not None:
            summary[summary_key] = parsed
    finite_energy = summary.get("total_energy", summary.get("electronic_energy"))
    if not result_json or finite_energy is None:
        if result_json:
            summary["result_validation_error"] = "xtbout.json has no finite energy"
            summary["canonical_result_path"] = ""
        return 0, (), (), summary
    detail = {
        "rank": 1,
        "kind": "single_point_result",
        "path": result_json,
        "score": 1000.0,
        "selected": True,
    }
    detail["total_energy"] = finite_energy
    detail["score"] = round(-float(finite_energy), 6)
    return 1, (result_json,), (detail,), summary
