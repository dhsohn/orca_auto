from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.flow.geometry_validation import (
    DEFAULT_BOND_SCALE,
    DEFAULT_MAX_SPURIOUS_BOND_CHANGES,
    DEFAULT_REACTING_BOND_STRETCH_SCALE,
    GeometryValidationError,
    validate_ts_guess_geometry,
)
from orca_auto.flow.hessian_utils import HessianConversionError, parse_xtb_hessian
from orca_auto.flow.xyz_utils import (
    FINITE_NUMBER_PATTERN,
    load_output_xyz_frames,
    load_xyz_atom_sequence,
    load_xyz_frames,
)

_NUMBER_END = r"(?![A-Za-z0-9_.])"

_TRIAL_RE = re.compile(
    rf"run\s+(\d+)\s+barrier:\s*({FINITE_NUMBER_PATTERN})\s+"
    rf"dE:\s*({FINITE_NUMBER_PATTERN})\s+product-end path RMSD:\s*"
    rf"({FINITE_NUMBER_PATTERN}){_NUMBER_END}"
)
_FORWARD_BARRIER_RE = re.compile(
    rf"forward\s+barrier\s+\(kcal\)\s*:\s*({FINITE_NUMBER_PATTERN}){_NUMBER_END}",
    re.IGNORECASE,
)
_BACKWARD_BARRIER_RE = re.compile(
    rf"backward\s+barrier\s+\(kcal\)\s*:\s*({FINITE_NUMBER_PATTERN}){_NUMBER_END}",
    re.IGNORECASE,
)
_REACTION_ENERGY_RE = re.compile(
    rf"reaction energy\s+\(kcal\)\s*:\s*({FINITE_NUMBER_PATTERN}){_NUMBER_END}",
    re.IGNORECASE,
)
_TS_FILE_RE = re.compile(r"estimated TS on file\s+(\S+)", re.IGNORECASE)
_POINT_COUNT_RE = re.compile(r"path\s+(\d+)\s+taken with\s+(\d+)\s+points", re.IGNORECASE)


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


def _positive_finite_float(value: Any, *, field_name: str) -> float:
    parsed = _safe_float(value)
    if parsed is None or parsed <= 0:
        raise ValueError(f"{field_name} must be a positive finite number")
    return parsed


def _nonnegative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a nonnegative integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{field_name} must be a nonnegative integer")
        parsed = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[-+]?\d+", value.strip()):
        parsed = int(value.strip())
    else:
        raise ValueError(f"{field_name} must be a nonnegative integer")
    if parsed < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")
    return parsed


def _load_xtbout_json(job_dir: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(
            read_stable_regular_file(job_dir / "xtbout.json").decode(
                "utf-8",
                errors="strict",
            )
        )
    except (OSError, UnicodeError, ValueError):
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


def _path_trial_from_match(match: re.Match[str]) -> dict[str, Any] | None:
    values = tuple(_safe_float(match.group(index)) for index in range(2, 5))
    if any(value is None for value in values):
        return None
    barrier_kcal, delta_e_kcal, product_end_rmsd = values
    return {
        "trial_index": int(match.group(1)),
        "barrier_kcal": barrier_kcal,
        "delta_e_kcal": delta_e_kcal,
        "product_end_rmsd": product_end_rmsd,
    }


def _apply_path_search_stdout_line(
    job_dir: Path, line: str, summary: dict[str, Any], trials: list[dict[str, Any]]
) -> None:
    if match := _TRIAL_RE.search(line):
        trial = _path_trial_from_match(match)
        if trial is not None:
            trials.append(trial)
        return
    if match := _FORWARD_BARRIER_RE.search(line):
        value = _safe_float(match.group(1))
        if value is not None:
            summary["forward_barrier_kcal"] = value
        return
    if match := _BACKWARD_BARRIER_RE.search(line):
        value = _safe_float(match.group(1))
        if value is not None:
            summary["backward_barrier_kcal"] = value
        return
    if match := _REACTION_ENERGY_RE.search(line):
        value = _safe_float(match.group(1))
        if value is not None:
            summary["reaction_energy_kcal"] = value
        return
    if match := _TS_FILE_RE.search(line):
        ts_guess_path = _resolve_existing_path(job_dir, match.group(1))
        if ts_guess_path:
            summary["ts_guess_path"] = ts_guess_path
        return
    if match := _POINT_COUNT_RE.search(line):
        summary["selected_path_index"] = int(match.group(1))
        summary["selected_path_point_count"] = int(match.group(2))


def _parse_path_search_stdout(job_dir: Path, stdout_log: str) -> dict[str, Any]:
    path = Path(stdout_log).expanduser().resolve()
    if not path.is_relative_to(job_dir.expanduser().resolve()) or not path.exists():
        return {}

    summary: dict[str, Any] = {}
    trials: list[dict[str, Any]] = []
    try:
        stdout_lines = read_stable_regular_file(path).decode("utf-8", errors="ignore").splitlines()
    except (OSError, ValueError):
        return {}
    for line in stdout_lines:
        _apply_path_search_stdout_line(job_dir, line, summary, trials)

    if trials:
        summary["path_trials"] = trials
    full_path = _resolve_existing_path(job_dir, "xtbpath.xyz")
    if full_path:
        summary["path_file"] = full_path
    selected_path = _resolve_existing_path(job_dir, "xtbpath_0.xyz")
    if selected_path:
        summary["selected_path_file"] = selected_path
    return summary


def _ts_guess_validation_fields(
    ts_guess_path: str,
    input_summary: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """Judge the TS guess against its endpoints; None when validation cannot run."""
    ts_frames = load_output_xyz_frames(ts_guess_path)
    if len(ts_frames) != 1:
        return {
            "geometry_valid": False,
            "geometry_validation": {
                "valid": False,
                "error": "TS guess must contain exactly one valid finite XYZ frame",
            },
        }
    reactant_xyz = str(input_summary.get("reactant_xyz", "")).strip()
    product_xyz = str(input_summary.get("product_xyz", "")).strip()
    if not reactant_xyz or not product_xyz:
        return None
    try:
        ts_atoms = tuple(line.split()[0].casefold() for line in ts_frames[0].atom_lines)
        reactant_atoms = tuple(atom.casefold() for atom in load_xyz_atom_sequence(reactant_xyz))
        product_atoms = tuple(atom.casefold() for atom in load_xyz_atom_sequence(product_xyz))
    except (OSError, ValueError) as exc:
        return {
            "geometry_valid": False,
            "geometry_validation": {"valid": False, "error": str(exc)},
        }
    if ts_atoms != reactant_atoms or ts_atoms != product_atoms:
        return {
            "geometry_valid": False,
            "geometry_validation": {
                "valid": False,
                "error": "TS guess atom count or element order does not match both endpoints",
            },
        }
    options_raw = manifest.get("ts_guess_validation")
    options = options_raw if isinstance(options_raw, dict) else {}
    try:
        bond_scale = _positive_finite_float(
            options.get("bond_scale", DEFAULT_BOND_SCALE),
            field_name="ts_guess_validation.bond_scale",
        )
        max_spurious_bond_changes = _nonnegative_int(
            options.get("max_spurious_bond_changes", DEFAULT_MAX_SPURIOUS_BOND_CHANGES),
            field_name="ts_guess_validation.max_spurious_bond_changes",
        )
        reacting_bond_stretch_scale = _positive_finite_float(
            options.get("reacting_bond_stretch_scale", DEFAULT_REACTING_BOND_STRETCH_SCALE),
            field_name="ts_guess_validation.reacting_bond_stretch_scale",
        )
        verdict = validate_ts_guess_geometry(
            ts_guess_xyz=ts_guess_path,
            reactant_xyz=reactant_xyz,
            product_xyz=product_xyz,
            bond_scale=bond_scale,
            max_spurious_bond_changes=max_spurious_bond_changes,
            reacting_bond_stretch_scale=reacting_bond_stretch_scale,
        )
    except (GeometryValidationError, OSError, TypeError, ValueError) as exc:
        return {
            "geometry_valid": False,
            "geometry_validation": {"valid": False, "error": str(exc)},
        }
    return {"geometry_valid": verdict.valid, "geometry_validation": verdict.to_metadata()}


def _collect_path_search_candidates(
    job_dir: Path,
    stdout_log: str,
    *,
    input_summary: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
) -> tuple[int, tuple[str, ...], tuple[dict[str, Any], ...], dict[str, Any]]:
    summary = _parse_path_search_stdout(job_dir, stdout_log)
    details: list[dict[str, Any]] = []

    ts_guess = summary.get("ts_guess_path")
    if ts_guess:
        detail: dict[str, Any] = {
            "rank": 1,
            "kind": "ts_guess",
            "path": ts_guess,
            "score": 1000.0,
            "selected": True,
        }
        validation_fields = _ts_guess_validation_fields(
            str(ts_guess), dict(input_summary or {}), dict(manifest or {})
        )
        if validation_fields is not None:
            detail.update(validation_fields)
            if "geometry_valid" in validation_fields:
                summary["ts_guess_geometry_valid"] = validation_fields["geometry_valid"]
        details.append(detail)

    selected_path_file = summary.get("selected_path_file")
    if selected_path_file:
        details.append(
            {
                "rank": 2,
                "kind": "selected_path",
                "path": selected_path_file,
                "score": 900.0,
                "selected": True,
                "selected_path_index": summary.get("selected_path_index"),
                "selected_path_point_count": summary.get("selected_path_point_count"),
            }
        )

    ordered_paths = [item["path"] for item in details if item.get("selected")]
    if not ordered_paths and summary.get("path_file"):
        ordered_paths = [str(summary["path_file"])]
    if ordered_paths:
        summary["selected_candidate_paths"] = list(ordered_paths)
    return len(details), tuple(ordered_paths), tuple(details), summary


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


def _collect_hessian_candidates(
    job_dir: Path,
    *,
    selected_input_xyz: str | Path | None = None,
) -> tuple[int, tuple[str, ...], tuple[dict[str, Any], ...], dict[str, Any]]:
    hessian_path = _resolve_existing_path(job_dir, "hessian")
    summary = {
        "canonical_result_path": hessian_path,
        "vibspectrum_path": _resolve_existing_path(job_dir, "vibspectrum"),
    }
    if not hessian_path:
        return 0, (), (), summary
    try:
        matrix = parse_xtb_hessian(hessian_path)
        if selected_input_xyz is not None:
            frames = load_xyz_frames(selected_input_xyz)
            if len(frames) != 1 or len(matrix) != 3 * len(frames[0].atom_lines):
                raise HessianConversionError(
                    "xTB Hessian dimension does not match its selected XYZ input"
                )
    except (HessianConversionError, OSError, ValueError) as exc:
        summary["result_validation_error"] = str(exc)
        summary["canonical_result_path"] = ""
        return 0, (), (), summary
    detail = {
        "rank": 1,
        "kind": "hessian",
        "path": hessian_path,
        "score": 1000.0,
        "selected": True,
    }
    return 1, (hessian_path,), (detail,), summary
