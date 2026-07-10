from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from orca_auto.core.utils.persistence import load_json_mapping_file

_TRIAL_RE = re.compile(
    r"run\s+(\d+)\s+barrier:\s*([-+]?\d+(?:\.\d+)?)\s+dE:\s*([-+]?\d+(?:\.\d+)?)\s+product-end path RMSD:\s*([-+]?\d+(?:\.\d+)?)"
)
_FORWARD_BARRIER_RE = re.compile(
    r"forward\s+barrier\s+\(kcal\)\s*:\s*([-+]?\d+(?:\.\d+)?)", re.IGNORECASE
)
_BACKWARD_BARRIER_RE = re.compile(
    r"backward\s+barrier\s+\(kcal\)\s*:\s*([-+]?\d+(?:\.\d+)?)", re.IGNORECASE
)
_REACTION_ENERGY_RE = re.compile(
    r"reaction energy\s+\(kcal\)\s*:\s*([-+]?\d+(?:\.\d+)?)", re.IGNORECASE
)
_TS_FILE_RE = re.compile(r"estimated TS on file\s+(\S+)", re.IGNORECASE)
_POINT_COUNT_RE = re.compile(r"path\s+(\d+)\s+taken with\s+(\d+)\s+points", re.IGNORECASE)
_COMMENT_ENERGY_RE = re.compile(r"energy\s*[:=]\s*([-+]?\d+(?:\.\d+)?)", re.IGNORECASE)


def _resolve_existing_path(job_dir: Path, path_text: str) -> str:
    candidate = Path(path_text).expanduser()
    if not candidate.is_absolute():
        candidate = job_dir / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return ""
    if not resolved.exists() or not resolved.is_file():
        return ""
    return str(resolved)


def _safe_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_xtbout_json(job_dir: Path) -> dict[str, Any]:
    return load_json_mapping_file(job_dir / "xtbout.json") or {}


def _parse_candidate_comment_energy(candidate_xyz: Path) -> float | None:
    try:
        lines = candidate_xyz.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    match = _COMMENT_ENERGY_RE.search(lines[1])
    if not match:
        return None
    return _safe_float(match.group(1))


def _extract_sp_energy(job_dir: Path, candidate_xyz: Path) -> tuple[float | None, str]:
    xtbout = _load_xtbout_json(job_dir)
    for key in ("total energy", "electronic energy"):
        value = xtbout.get(key)
        if isinstance(value, (int, float)):
            return float(value), f"xtbout.json:{key}"
    if isinstance(xtbout.get("total energy"), str):
        value = _safe_float(xtbout["total energy"])
        if value is not None:
            return value, "xtbout.json:total energy"
    comment_energy = _parse_candidate_comment_energy(candidate_xyz)
    if comment_energy is not None:
        return comment_energy, "candidate_comment"
    return None, ""


def _path_trial_from_match(match: re.Match[str]) -> dict[str, Any]:
    return {
        "trial_index": int(match.group(1)),
        "barrier_kcal": float(match.group(2)),
        "delta_e_kcal": float(match.group(3)),
        "product_end_rmsd": float(match.group(4)),
    }


def _apply_path_search_stdout_line(
    job_dir: Path, line: str, summary: dict[str, Any], trials: list[dict[str, Any]]
) -> None:
    if match := _TRIAL_RE.search(line):
        trials.append(_path_trial_from_match(match))
        return
    if match := _FORWARD_BARRIER_RE.search(line):
        summary["forward_barrier_kcal"] = float(match.group(1))
        return
    if match := _BACKWARD_BARRIER_RE.search(line):
        summary["backward_barrier_kcal"] = float(match.group(1))
        return
    if match := _REACTION_ENERGY_RE.search(line):
        summary["reaction_energy_kcal"] = float(match.group(1))
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
    path = Path(stdout_log)
    if not path.exists():
        return {}

    summary: dict[str, Any] = {}
    trials: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
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


def _collect_path_search_candidates(
    job_dir: Path, stdout_log: str
) -> tuple[int, tuple[str, ...], tuple[dict[str, Any], ...], dict[str, Any]]:
    summary = _parse_path_search_stdout(job_dir, stdout_log)
    details: list[dict[str, Any]] = []

    ts_guess = summary.get("ts_guess_path")
    if ts_guess:
        details.append(
            {
                "rank": 1,
                "kind": "ts_guess",
                "path": ts_guess,
                "score": 1000.0,
                "selected": True,
            }
        )

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
) -> tuple[int, tuple[str, ...], tuple[dict[str, Any], ...], dict[str, Any]]:
    optimized_geometry = _resolve_existing_path(job_dir, "xtbopt.xyz")
    summary = {
        "canonical_result_path": optimized_geometry,
        "optimization_log_path": _resolve_existing_path(job_dir, "xtbopt.log"),
        "optimization_ok": (job_dir / ".xtboptok").exists(),
    }
    if not optimized_geometry:
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
    if isinstance(xtbout.get("total energy"), (int, float)):
        summary["total_energy"] = float(xtbout["total energy"])
    if isinstance(xtbout.get("electronic energy"), (int, float)):
        summary["electronic_energy"] = float(xtbout["electronic energy"])
    if not result_json:
        return 0, (), (), summary
    detail = {
        "rank": 1,
        "kind": "single_point_result",
        "path": result_json,
        "score": 1000.0,
        "selected": True,
    }
    if "total_energy" in summary:
        detail["total_energy"] = summary["total_energy"]
        detail["score"] = round(-float(summary["total_energy"]), 6)
    return 1, (result_json,), (detail,), summary


def _collect_hessian_candidates(
    job_dir: Path,
) -> tuple[int, tuple[str, ...], tuple[dict[str, Any], ...], dict[str, Any]]:
    hessian_path = _resolve_existing_path(job_dir, "hessian")
    summary = {
        "canonical_result_path": hessian_path,
        "vibspectrum_path": _resolve_existing_path(job_dir, "vibspectrum"),
    }
    if not hessian_path:
        return 0, (), (), summary
    detail = {
        "rank": 1,
        "kind": "hessian",
        "path": hessian_path,
        "score": 1000.0,
        "selected": True,
    }
    return 1, (hessian_path,), (detail,), summary
