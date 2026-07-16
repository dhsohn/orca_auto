from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from orca_auto.core.config.files import load_bounded_yaml_data
from orca_auto.core.utils import normalize_bool, normalize_text
from orca_auto.orca.completion_rules import IRC_ROUTE_RE, OPT_ROUTE_RE, TS_ROUTE_RE
from orca_auto.orca.job_type import FREQ_RE
from orca_auto.orca.report.interaction_energy import (
    interaction_electronic_state_mismatch_reason,
)

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
# remote_admission._REMOTE_WORKFLOW_COUNT_LIMITS) and against the materialized
# stage count.
INTERACTION_ENERGY_MAX_FRAGMENTS_CAP = 8
INTERACTION_ENERGY_MAX_MULTIPLICITY = 100
INTERACTION_ENERGY_RMSD_GROUPING_VERSION = 2
MAX_CREST_CANDIDATES = 32
DEFAULT_INTERACTION_SP_ROUTE_LINE = "! r2scan-3c TightSCF"
DEFAULT_RMSD_THRESHOLD_ANGSTROM = 0.25
# A finite default energy window is mandatory: geometry-only threshold grouping
# can collapse distinct minima that happen to superpose closely.
DEFAULT_RMSD_ENERGY_WINDOW_KCAL = 0.1

_INTERACTION_ENERGY_KEYS = frozenset(
    {
        "enabled",
        "fragments",
        "sp_route_line",
        "max_fragments",
        "priority",
        "max_cores",
        "max_memory_gb",
    }
)
_INTERACTION_FRAGMENT_KEYS = frozenset({"atom_indices", "charge", "multiplicity", "label"})
_RMSD_DEDUP_KEYS = frozenset(
    {"enabled", "rmsd_threshold_angstrom", "energy_window_kcal", "heavy_atoms_only"}
)
_INTEGER_TEXT_RE = re.compile(r"\A[+-]?\d+\Z")
_MAX_INTERACTION_LABEL_LENGTH = 80
_MAX_INTERACTION_ROUTE_LENGTH = 500
_INTERACTION_NON_SP_ROUTE_TOKENS = frozenset({"ENGRAD", "MD", "NUMGRAD"})


def _reject_unknown_keys(mapping: dict[str, Any], *, allowed: frozenset[str], field: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ValueError(f"{field} has unknown key(s): {', '.join(unknown)}")


def _require_bool(value: Any, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise ValueError(f"{field} must be a boolean")


def require_int(value: Any, *, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, not a boolean")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{field} must be an integer. got={value!r}")
        parsed = int(value)
    elif isinstance(value, str) and _INTEGER_TEXT_RE.fullmatch(value.strip()):
        parsed = int(value.strip())
    else:
        raise ValueError(f"{field} must be an integer. got={value!r}")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field} must be >= {minimum}. got={parsed}")
    return parsed


_require_int = require_int


def require_crest_candidate_count(value: Any, *, field: str = "max_crest_candidates") -> int:
    """Return one bounded CREST handoff count for local and durable workflows."""

    parsed = require_int(value, field=field, minimum=1)
    if parsed > MAX_CREST_CANDIDATES:
        raise ValueError(f"{field} must be <= {MAX_CREST_CANDIDATES}. got={parsed}")
    return parsed


def _require_interaction_text(
    value: Any,
    *,
    field: str,
    maximum_length: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not text and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    if len(text) > maximum_length:
        raise ValueError(f"{field} must be at most {maximum_length} characters")
    if text and not text.isprintable():
        raise ValueError(f"{field} must be a single printable line")
    return text


def _require_single_point_route(value: str) -> str:
    tokens = {token.upper() for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", value)}
    forbidden = sorted(
        token
        for token in tokens
        if token in _INTERACTION_NON_SP_ROUTE_TOKENS
        or token.startswith("NEB")
        or token.startswith("ZOOM-NEB")
        or token.startswith("GOAT")
    )
    for label, pattern in (
        ("optimization", OPT_ROUTE_RE),
        ("transition-state search", TS_ROUTE_RE),
        ("frequency analysis", FREQ_RE),
        ("IRC", IRC_ROUTE_RE),
    ):
        if pattern.search(value):
            forbidden.append(label)
    if forbidden:
        raise ValueError(
            "interaction_energy.sp_route_line must request a single-point energy only; "
            f"forbidden run type(s): {', '.join(dict.fromkeys(forbidden))}"
        )
    return value


def interaction_energy_config_fingerprint(
    block: dict[str, Any],
    *,
    complex_charge: int,
    complex_multiplicity: int,
    rmsd_dedup: dict[str, Any] | None = None,
) -> str:
    """Stable identity for the scientific inputs of one interaction-energy generation."""
    rmsd_grouping = rmsd_dedup or {}
    scientific = {
        "sp_route_line": block.get("sp_route_line"),
        "fragments": block.get("fragments"),
        "complex_charge": complex_charge,
        "complex_multiplicity": complex_multiplicity,
        # Interaction fan-out always uses representative grouping, including
        # the documented hidden safe defaults when public RMSD reporting is off.
        "rmsd_grouping": {
            "version": INTERACTION_ENERGY_RMSD_GROUPING_VERSION,
            "rmsd_threshold_angstrom": rmsd_grouping.get(
                "rmsd_threshold_angstrom", DEFAULT_RMSD_THRESHOLD_ANGSTROM
            ),
            "energy_window_kcal": rmsd_grouping.get(
                "energy_window_kcal", DEFAULT_RMSD_ENERGY_WINDOW_KCAL
            ),
            "heavy_atoms_only": rmsd_grouping.get("heavy_atoms_only", False),
        },
    }
    encoded = json.dumps(
        scientific,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_interaction_energy_state_balance(
    block: dict[str, Any] | None,
    *,
    complex_charge: int,
    complex_multiplicity: int,
) -> None:
    """Reject fragment states outside the complex charge/spin manifold."""
    if block is None:
        return
    fragments = block.get("fragments")
    if not isinstance(fragments, list):
        raise ValueError("interaction_energy.fragments must be a list")
    fragment_states = [
        (int(fragment.get("charge", 0)), int(fragment.get("multiplicity", 1)))
        for fragment in fragments
        if isinstance(fragment, dict)
    ]
    reason = interaction_electronic_state_mismatch_reason(
        complex_charge,
        complex_multiplicity,
        fragment_states,
    )
    if reason:
        raise ValueError(
            f"interaction_energy fragment electronic states are incompatible: {reason}"
        )


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
    _reject_unknown_keys(block, allowed=_INTERACTION_ENERGY_KEYS, field="interaction_energy")
    if not _require_bool(block.get("enabled"), field="interaction_energy.enabled", default=False):
        return None

    raw_fragments = block.get("fragments")
    if not isinstance(raw_fragments, list) or len(raw_fragments) < 2:
        raise ValueError(
            "interaction_energy.fragments must contain at least two fragments when enabled"
        )
    max_fragments = _require_int(
        block.get("max_fragments", INTERACTION_ENERGY_MAX_FRAGMENTS_CAP),
        field="interaction_energy.max_fragments",
        minimum=2,
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
        fragment = manifest_mapping(fragment)
        _reject_unknown_keys(
            fragment,
            allowed=_INTERACTION_FRAGMENT_KEYS,
            field=f"interaction_energy.fragments[{position}]",
        )
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
        raw_label = fragment.get("label")
        label = (
            _require_interaction_text(
                raw_label,
                field=f"interaction_energy.fragments[{position}].label",
                maximum_length=_MAX_INTERACTION_LABEL_LENGTH,
            )
            if raw_label is not None
            else f"fragment_{position + 1}"
        )
        multiplicity = _require_int(
            fragment.get("multiplicity", 1),
            field=f"interaction_energy.fragments[{position}].multiplicity",
            minimum=1,
        )
        if multiplicity > INTERACTION_ENERGY_MAX_MULTIPLICITY:
            raise ValueError(
                f"interaction_energy.fragments[{position}].multiplicity exceeds the "
                f"supported ceiling {INTERACTION_ENERGY_MAX_MULTIPLICITY}"
            )
        fragments.append(
            {
                "atom_indices": indices,
                "charge": _require_int(
                    fragment.get("charge", 0),
                    field=f"interaction_energy.fragments[{position}].charge",
                ),
                "multiplicity": multiplicity,
                "label": label,
            }
        )

    all_indices = [index for fragment in fragments for index in fragment["atom_indices"]]
    if len(all_indices) != len(set(all_indices)):
        raise ValueError("interaction_energy fragment atom_indices must be disjoint")
    ordered_indices = sorted(all_indices)
    if any(index != expected for expected, index in enumerate(ordered_indices)):
        raise ValueError(
            "interaction_energy fragment atom_indices must form a contiguous partition from 0"
        )

    raw_route = block.get("sp_route_line")
    sp_route_line = _require_single_point_route(
        _require_interaction_text(
            raw_route,
            field="interaction_energy.sp_route_line",
            maximum_length=_MAX_INTERACTION_ROUTE_LENGTH,
        )
        if raw_route is not None
        else DEFAULT_INTERACTION_SP_ROUTE_LINE
    )
    normalized: dict[str, Any] = {
        "enabled": True,
        "fragments": fragments,
        "sp_route_line": sp_route_line,
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
    _reject_unknown_keys(block, allowed=_RMSD_DEDUP_KEYS, field="rmsd_dedup")
    if not _require_bool(block.get("enabled"), field="rmsd_dedup.enabled", default=False):
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
        # All-atom RMSD is the fail-closed default: ignoring H/D/T can collapse
        # mirror minima whose stereochemistry is defined by an isotope or proton.
        "heavy_atoms_only": _require_bool(
            block.get("heavy_atoms_only"),
            field="rmsd_dedup.heavy_atoms_only",
            default=False,
        ),
    }


def validate_conformer_postprocessing_template(
    template_name: Any,
    *,
    interaction_energy: dict[str, Any] | None,
    rmsd_dedup: dict[str, Any] | None,
) -> None:
    """Reject conformer-only post-processing on every other workflow template."""
    template = normalize_text(template_name).lower()
    if template != "conformer_screening" and (
        interaction_energy is not None or rmsd_dedup is not None
    ):
        raise ValueError(
            "interaction_energy and rmsd_dedup are supported only for conformer_screening"
        )


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
            parsed = load_bounded_yaml_data(candidate)
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
    "INTERACTION_ENERGY_MAX_MULTIPLICITY",
    "INTERACTION_ENERGY_RMSD_GROUPING_VERSION",
    "MAX_CREST_CANDIDATES",
    "interaction_energy_config_fingerprint",
    "load_flow_manifest",
    "manifest_allows_external_inputs",
    "manifest_mapping",
    "normalize_interaction_energy_block",
    "normalize_rmsd_dedup_block",
    "optional_positive_float",
    "require_crest_candidate_count",
    "resolve_endpoint_pairing_manifest",
    "resolve_engine_manifest",
    "resolve_engine_manifest_with_presence",
    "resolve_manifest_file_value",
    "validate_conformer_postprocessing_template",
    "validate_interaction_energy_state_balance",
]
