from __future__ import annotations

import math
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from orca_auto.core.utils import normalize_bool, normalize_text

FLOW_MANIFEST_FILENAMES = ("flow.yaml",)


def manifest_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if normalize_text(key)}


def optional_positive_float(
    mapping: dict[str, Any],
    key: str,
    *,
    label: str | None = None,
) -> float | None:
    """Return one optional positive finite numeric field, rejecting malformed values."""
    raw = mapping.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    field = label or key
    if isinstance(raw, bool):
        raise ValueError(f"{field} must be a positive finite number, not a boolean")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive finite number. got={raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be a positive finite number. got={raw!r}")
    return value


# The interaction-energy fan-out materializes one single point per fragment per
# retained representative; this hard cap bounds that fan-out regardless of what a
# manifest declares. It is also enforced against remote uploads (see the bot's
# _REMOTE_WORKFLOW_COUNT_LIMITS) and against the materialized stage count.
INTERACTION_ENERGY_MAX_FRAGMENTS_CAP = 8
DEFAULT_INTERACTION_SP_ROUTE_LINE = "! r2scan-3c TightSCF"
DEFAULT_RMSD_THRESHOLD_ANGSTROM = 0.25
# A finite default energy window is mandatory: heavy-atom RMSD with no energy
# guard would silently merge distinct minima that differ only in hydrogen/rotamer
# positions. Merging requires BOTH low RMSD and a small energy gap.
DEFAULT_RMSD_ENERGY_WINDOW_KCAL = 0.1


def _require_int(value: Any, *, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, not a boolean")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer. got={value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field} must be >= {minimum}. got={parsed}")
    return parsed


def normalize_interaction_energy_block(raw: Any) -> dict[str, Any] | None:
    """Validate and normalize the ``interaction_energy:`` manifest block.

    Returns ``None`` when absent or disabled (the feature is a no-op). Raises
    ``ValueError`` on any malformed value so a bad configuration fails closed at
    admission instead of silently producing nothing or an unsafe stage.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("interaction_energy must be a mapping")
    block = manifest_mapping(raw)
    if not normalize_bool(block.get("enabled"), default=False):
        return None

    raw_fragments = block.get("fragments")
    if not isinstance(raw_fragments, list) or not raw_fragments:
        raise ValueError(
            "interaction_energy.fragments must be a non-empty list when the feature is enabled"
        )
    max_fragments = _require_int(
        block.get("max_fragments", INTERACTION_ENERGY_MAX_FRAGMENTS_CAP),
        field="interaction_energy.max_fragments",
        minimum=1,
    )
    if max_fragments > INTERACTION_ENERGY_MAX_FRAGMENTS_CAP:
        raise ValueError(
            f"interaction_energy.max_fragments={max_fragments} exceeds the "
            f"{INTERACTION_ENERGY_MAX_FRAGMENTS_CAP}-fragment ceiling"
        )
    if len(raw_fragments) > max_fragments:
        raise ValueError(
            f"interaction_energy.fragments has {len(raw_fragments)} fragments; "
            f"max_fragments is {max_fragments}"
        )

    fragments: list[dict[str, Any]] = []
    for position, fragment in enumerate(raw_fragments):
        if not isinstance(fragment, dict):
            raise ValueError(f"interaction_energy.fragments[{position}] must be a mapping")
        raw_indices = fragment.get("atom_indices")
        if not isinstance(raw_indices, list) or not raw_indices:
            raise ValueError(
                f"interaction_energy.fragments[{position}].atom_indices must be a non-empty list"
            )
        indices = [
            _require_int(
                index,
                field=f"interaction_energy.fragments[{position}].atom_indices",
                minimum=0,
            )
            for index in raw_indices
        ]
        label = normalize_text(fragment.get("label")) or f"fragment_{position + 1}"
        fragments.append(
            {
                "atom_indices": indices,
                "charge": _require_int(
                    fragment.get("charge", 0),
                    field=f"interaction_energy.fragments[{position}].charge",
                ),
                "multiplicity": _require_int(
                    fragment.get("multiplicity", 1),
                    field=f"interaction_energy.fragments[{position}].multiplicity",
                    minimum=1,
                ),
                "label": label,
            }
        )

    normalized: dict[str, Any] = {
        "enabled": True,
        "fragments": fragments,
        "sp_route_line": normalize_text(block.get("sp_route_line"))
        or DEFAULT_INTERACTION_SP_ROUTE_LINE,
        "max_fragments": max_fragments,
    }
    for key, minimum in (("priority", None), ("max_cores", 1), ("max_memory_gb", 1)):
        if block.get(key) is not None and normalize_text(block.get(key)) != "":
            normalized[key] = _require_int(
                block.get(key), field=f"interaction_energy.{key}", minimum=minimum
            )
    return normalized


def normalize_rmsd_dedup_block(raw: Any) -> dict[str, Any] | None:
    """Validate and normalize the ``rmsd_dedup:`` manifest block.

    Returns ``None`` when absent or disabled. Raises ``ValueError`` on malformed
    values. The energy window always resolves to a finite positive default so
    structure-dropping never runs without an energy guard.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("rmsd_dedup must be a mapping")
    block = manifest_mapping(raw)
    if not normalize_bool(block.get("enabled"), default=False):
        return None
    threshold = optional_positive_float(
        block, "rmsd_threshold_angstrom", label="rmsd_dedup.rmsd_threshold_angstrom"
    )
    energy_window = optional_positive_float(
        block, "energy_window_kcal", label="rmsd_dedup.energy_window_kcal"
    )
    return {
        "enabled": True,
        "rmsd_threshold_angstrom": (
            threshold if threshold is not None else DEFAULT_RMSD_THRESHOLD_ANGSTROM
        ),
        "energy_window_kcal": (
            energy_window if energy_window is not None else DEFAULT_RMSD_ENERGY_WINDOW_KCAL
        ),
        "heavy_atoms_only": normalize_bool(block.get("heavy_atoms_only"), default=True),
    }


def load_flow_manifest(
    directory: Path,
    *,
    filenames: tuple[str, ...] = FLOW_MANIFEST_FILENAMES,
    description: str = "Workflow manifest",
) -> dict[str, Any]:
    for name in filenames:
        candidate = directory / name
        if not candidate.is_file():
            continue
        try:
            parsed = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(_invalid_manifest_yaml_message(candidate, description, exc)) from exc
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise ValueError(f"{description} must contain a mapping: {candidate}")
        return dict(parsed)
    return {}


def _invalid_manifest_yaml_message(path: Path, description: str, exc: yaml.YAMLError) -> str:
    message = f"Invalid {description}: {path}"
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        message = f"{message} (line {mark.line + 1}, column {mark.column + 1})"
    problem = normalize_text(getattr(exc, "problem", None))
    if problem:
        message = f"{message}: {problem}"
    return message


def _reject_windows_manifest_path_syntax(text: str, *, field_name: str) -> None:
    windows = PureWindowsPath(text)
    if "\\" in text or bool(windows.drive):
        raise ValueError(
            f"{field_name} must use Linux/POSIX path syntax, not Windows path syntax: {text!r}"
        )


def manifest_allows_external_inputs(manifest: dict[str, Any]) -> bool:
    return normalize_bool(manifest.get("allow_external_inputs"), default=False)


def _require_manifest_path_inside_base(
    *,
    base_dir: Path,
    target: Path,
    raw_text: str,
    field_name: str,
) -> None:
    base_resolved = base_dir.expanduser().resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must resolve inside workflow_dir unless "
            f"allow_external_inputs: true is set: {raw_text!r}"
        ) from exc


def resolve_manifest_file_value(
    base_dir: Path,
    value: Any,
    *,
    allow_external_inputs: bool = False,
    field_name: str = "manifest file path",
) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    _reject_windows_manifest_path_syntax(text, field_name=field_name)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    resolved = candidate.resolve()
    if not allow_external_inputs:
        _require_manifest_path_inside_base(
            base_dir=base_dir,
            target=resolved,
            raw_text=text,
            field_name=field_name,
        )
    return str(resolved)


def resolve_engine_manifest(base_dir: Path, manifest: dict[str, Any], key: str) -> dict[str, Any]:
    raw = manifest.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{key} section must be a mapping")
    section = manifest_mapping(raw)
    if not section:
        return {}
    resolved = dict(section)
    if "xcontrol_file" in resolved:
        resolved["xcontrol_file"] = resolve_manifest_file_value(
            base_dir,
            resolved.get("xcontrol_file"),
            allow_external_inputs=manifest_allows_external_inputs(manifest),
            field_name=f"{key}.xcontrol_file",
        )
    return resolved


def resolve_engine_manifest_with_presence(
    base_dir: Path,
    manifest: dict[str, Any],
    key: str,
) -> tuple[bool, dict[str, Any]]:
    if key not in manifest or manifest.get(key) is None:
        return False, {}
    return True, resolve_engine_manifest(base_dir, manifest, key)


def resolve_endpoint_pairing_manifest(
    manifest: dict[str, Any],
    xtb_manifest: dict[str, Any],
) -> dict[str, Any]:
    xtb_section = manifest_mapping(xtb_manifest.pop("endpoint_pairing", None))
    top_level = manifest_mapping(manifest.get("endpoint_pairing"))
    resolved = dict(xtb_section)
    resolved.update(top_level)
    return resolved


__all__ = [
    "DEFAULT_INTERACTION_SP_ROUTE_LINE",
    "DEFAULT_RMSD_ENERGY_WINDOW_KCAL",
    "DEFAULT_RMSD_THRESHOLD_ANGSTROM",
    "FLOW_MANIFEST_FILENAMES",
    "INTERACTION_ENERGY_MAX_FRAGMENTS_CAP",
    "load_flow_manifest",
    "manifest_allows_external_inputs",
    "manifest_mapping",
    "normalize_interaction_energy_block",
    "normalize_rmsd_dedup_block",
    "optional_positive_float",
    "resolve_endpoint_pairing_manifest",
    "resolve_engine_manifest",
    "resolve_engine_manifest_with_presence",
    "resolve_manifest_file_value",
]
