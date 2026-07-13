from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite, sqrt
from pathlib import Path
from typing import Any

from orca_auto.core.utils.coercion import (
    coerce_mapping as _coerce_mapping,
)
from orca_auto.core.utils.coercion import (
    normalize_text as _normalize_text,
)

from . import endpoint_pairing_selection as _selection
from .contracts import WorkflowStageInput
from .orchestration.charge_spin import strict_int
from .xyz_utils import XYZFrame, load_output_xyz_frames


@dataclass(frozen=True)
class _EndpointPairingDeps:
    EndpointPair: Any
    _distance_rmsd: Any
    _rank_gap: Any


def _endpoint_pairing_deps() -> _EndpointPairingDeps:
    return _EndpointPairingDeps(
        EndpointPair=EndpointPair,
        _distance_rmsd=_distance_rmsd,
        _rank_gap=_rank_gap,
    )


_TRUE_BOOLEAN_TEXT = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSE_BOOLEAN_TEXT = frozenset({"0", "false", "no", "off", "disabled"})
_DISABLED_MODE_TEXT = _FALSE_BOOLEAN_TEXT | frozenset({"none"})
_SUPPORTED_MODE_TEXT = _TRUE_BOOLEAN_TEXT | _DISABLED_MODE_TEXT
_SUPPORTED_POLICY_KEYS = frozenset(
    {
        "alignment_atoms",
        "atoms",
        "comparison_atoms",
        "enabled",
        "exclude_atoms",
        "excluded_atoms",
        "fallback_to_ranked",
        "max_distance_rmsd",
        "max_pairs",
        "max_rank_gap",
        "max_reaction_center_rmsd",
        "max_rmsd",
        "mobile_atoms",
        "mode",
        "moving_atoms",
        "rank_weight",
        "reaction_center_atoms",
        "rmsd_atoms",
    }
)


def _as_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_BOOLEAN_TEXT:
            return True
        if normalized in _FALSE_BOOLEAN_TEXT:
            return False
    raise ValueError(f"endpoint_pairing.{field_name} must be a boolean")


def _as_nonnegative_int(value: Any, *, field_name: str, default: int = 0) -> int:
    if value is None or _normalize_text(value) == "":
        return strict_int(default, field=f"endpoint_pairing.{field_name}", minimum=0)
    return strict_int(value, field=f"endpoint_pairing.{field_name}", minimum=0)


def _as_optional_float(
    value: Any,
    *,
    field_name: str,
    minimum: float | None = None,
) -> float | None:
    if value is None or _normalize_text(value) == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"endpoint_pairing.{field_name} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"endpoint_pairing.{field_name} must be a finite number") from exc
    if not isfinite(parsed):
        raise ValueError(f"endpoint_pairing.{field_name} must be a finite number")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"endpoint_pairing.{field_name} must be >= {minimum:g}")
    return parsed


def _coerce_atom_indices(value: Any, *, field_name: str) -> tuple[int, ...]:
    if value is None or _normalize_text(value) == "":
        return ()
    raw_items: list[Any]
    if isinstance(value, str):
        raw_items = [item for item in value.replace(",", " ").split() if item]
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raise ValueError(f"endpoint_pairing.{field_name} must be a list of atom indices")

    indices: list[int] = []
    seen: set[int] = set()
    for raw in raw_items:
        index = strict_int(raw, field=f"endpoint_pairing.{field_name} atom index", minimum=1)
        if index in seen:
            continue
        indices.append(index)
        seen.add(index)
    return tuple(indices)


@dataclass(frozen=True)
class EndpointPairingPolicy:
    enabled: bool = False
    comparison_atoms: tuple[int, ...] = ()
    excluded_atoms: tuple[int, ...] = ()
    max_distance_rmsd: float | None = None
    max_rank_gap: int = 0
    max_pairs: int = 0
    rank_weight: float = 0.01
    fallback_to_ranked: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(
        cls,
        value: Any,
        *,
        default_max_pairs: int = 0,
    ) -> EndpointPairingPolicy:
        default_limit = strict_int(
            default_max_pairs,
            field="endpoint_pairing.default_max_pairs",
            minimum=0,
        )
        if value is None or value == "":
            return cls(enabled=False, max_pairs=default_limit)
        if isinstance(value, bool):
            return cls(enabled=value, max_pairs=default_limit)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in _FALSE_BOOLEAN_TEXT or not text:
                return cls(enabled=False, max_pairs=default_limit)
            if text in _TRUE_BOOLEAN_TEXT:
                return cls(enabled=True, max_pairs=default_limit, raw={"enabled": value})
            raise ValueError("endpoint_pairing shorthand must be a boolean")

        if not isinstance(value, dict):
            raise ValueError("endpoint_pairing must be a mapping or boolean shorthand")

        raw = _coerce_mapping(value)
        if not raw:
            return cls(enabled=False, max_pairs=default_limit)
        unknown_keys = sorted(set(raw) - _SUPPORTED_POLICY_KEYS)
        if unknown_keys:
            raise ValueError(f"endpoint_pairing has unsupported key(s): {', '.join(unknown_keys)}")
        enabled = _as_bool(raw["enabled"], field_name="enabled") if "enabled" in raw else True
        mode = ""
        if "mode" in raw:
            raw_mode = raw["mode"]
            if not isinstance(raw_mode, str):
                raise ValueError("endpoint_pairing.mode must be a supported string mode")
            mode = raw_mode.strip().lower()
            if mode not in _SUPPORTED_MODE_TEXT:
                raise ValueError(f"endpoint_pairing.mode is unsupported: {raw_mode!r}")
        if mode in _DISABLED_MODE_TEXT:
            return cls(enabled=False, max_pairs=default_limit, raw=raw)
        atoms = (
            _coerce_atom_indices(raw.get("comparison_atoms"), field_name="comparison_atoms")
            or _coerce_atom_indices(raw.get("alignment_atoms"), field_name="alignment_atoms")
            or _coerce_atom_indices(raw.get("rmsd_atoms"), field_name="rmsd_atoms")
            or _coerce_atom_indices(raw.get("atoms"), field_name="atoms")
        )
        excluded_atoms = (
            _coerce_atom_indices(raw.get("moving_atoms"), field_name="moving_atoms")
            or _coerce_atom_indices(raw.get("mobile_atoms"), field_name="mobile_atoms")
            or _coerce_atom_indices(raw.get("exclude_atoms"), field_name="exclude_atoms")
            or _coerce_atom_indices(raw.get("excluded_atoms"), field_name="excluded_atoms")
            or _coerce_atom_indices(
                raw.get("reaction_center_atoms"), field_name="reaction_center_atoms"
            )
        )
        max_distance_rmsd = (
            _as_optional_float(
                raw.get("max_distance_rmsd"),
                field_name="max_distance_rmsd",
                minimum=0.0,
            )
            if "max_distance_rmsd" in raw
            else _as_optional_float(
                raw.get("max_reaction_center_rmsd"),
                field_name="max_reaction_center_rmsd",
                minimum=0.0,
            )
            if "max_reaction_center_rmsd" in raw
            else _as_optional_float(raw.get("max_rmsd"), field_name="max_rmsd", minimum=0.0)
        )
        max_pairs = _as_nonnegative_int(
            raw.get("max_pairs"), field_name="max_pairs", default=default_limit
        )
        rank_weight = _as_optional_float(
            raw.get("rank_weight"), field_name="rank_weight", minimum=0.0
        )
        fallback_default = not bool(atoms or excluded_atoms or max_distance_rmsd is not None)
        return cls(
            enabled=enabled,
            comparison_atoms=atoms,
            excluded_atoms=excluded_atoms,
            max_distance_rmsd=max_distance_rmsd,
            max_rank_gap=_as_nonnegative_int(
                raw.get("max_rank_gap"), field_name="max_rank_gap", default=0
            ),
            max_pairs=max_pairs,
            rank_weight=rank_weight if rank_weight is not None else 0.01,
            fallback_to_ranked=(
                _as_bool(raw["fallback_to_ranked"], field_name="fallback_to_ranked")
                if "fallback_to_ranked" in raw
                else fallback_default
            ),
            raw=raw,
        )

    def to_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "comparison_atoms": list(self.comparison_atoms),
            "excluded_atoms": list(self.excluded_atoms),
            "max_distance_rmsd": self.max_distance_rmsd,
            "max_rank_gap": self.max_rank_gap,
            "max_pairs": self.max_pairs,
            "rank_weight": self.rank_weight,
            "fallback_to_ranked": self.fallback_to_ranked,
        }


@dataclass(frozen=True)
class EndpointPair:
    reactant: WorkflowStageInput
    product: WorkflowStageInput
    score: float
    metadata: dict[str, Any]


def _frame_for_input(item: WorkflowStageInput) -> XYZFrame | None:
    path_text = _normalize_text(item.artifact_path)
    if not path_text:
        return None
    frames = load_output_xyz_frames(Path(path_text).expanduser())
    if not frames:
        return None
    try:
        requested_index = int(item.metadata.get("source_frame_index", 0) or 0)
    except (TypeError, ValueError):
        requested_index = 0
    if requested_index > 0:
        for frame in frames:
            if frame.index == requested_index:
                return frame
        return None
    return frames[0]


def _frame_coordinates(frame: XYZFrame) -> tuple[tuple[float, float, float], ...]:
    coords: list[tuple[float, float, float]] = []
    for line in frame.atom_lines:
        tokens = line.split()
        coords.append((float(tokens[1]), float(tokens[2]), float(tokens[3])))
    return tuple(coords)


def _frame_atom_sequence(frame: XYZFrame) -> tuple[str, ...]:
    return tuple(line.split()[0].casefold() for line in frame.atom_lines)


def _comparison_indices(
    indices: tuple[int, ...],
    *,
    excluded_indices: tuple[int, ...],
    natoms: int,
) -> tuple[int, ...]:
    if indices:
        return tuple(index for index in indices if 1 <= index <= natoms)
    excluded = {index for index in excluded_indices if 1 <= index <= natoms}
    return tuple(index for index in range(1, natoms + 1) if index not in excluded)


def _distance(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sqrt((left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2 + (left[2] - right[2]) ** 2)


def _distance_fingerprint(
    coords: tuple[tuple[float, float, float], ...],
    indices: tuple[int, ...],
) -> tuple[float, ...]:
    distances: list[float] = []
    zero_based = [index - 1 for index in indices]
    for outer, left_index in enumerate(zero_based):
        for right_index in zero_based[outer + 1 :]:
            distances.append(_distance(coords[left_index], coords[right_index]))
    return tuple(distances)


def _distance_rmsd(
    reactant: WorkflowStageInput,
    product: WorkflowStageInput,
    *,
    atom_indices: tuple[int, ...],
    excluded_indices: tuple[int, ...],
) -> tuple[float | None, str, tuple[int, ...]]:
    reactant_frame = _frame_for_input(reactant)
    product_frame = _frame_for_input(product)
    if reactant_frame is None or product_frame is None:
        return None, "missing_or_invalid_xyz", ()
    if reactant_frame.natoms != product_frame.natoms:
        return None, "atom_count_mismatch", ()
    if _frame_atom_sequence(reactant_frame) != _frame_atom_sequence(product_frame):
        return None, "element_order_mismatch", ()

    indices = _comparison_indices(
        atom_indices,
        excluded_indices=excluded_indices,
        natoms=reactant_frame.natoms,
    )
    if len(indices) < 2:
        return None, "too_few_comparison_atoms", indices

    reactant_fp = _distance_fingerprint(_frame_coordinates(reactant_frame), indices)
    product_fp = _distance_fingerprint(_frame_coordinates(product_frame), indices)
    if not reactant_fp or len(reactant_fp) != len(product_fp):
        return None, "empty_distance_fingerprint", indices
    squared = [(left - right) ** 2 for left, right in zip(reactant_fp, product_fp, strict=True)]
    return sqrt(sum(squared) / len(squared)), "distance_fingerprint", indices


def _rank_gap(reactant: WorkflowStageInput, product: WorkflowStageInput) -> int:
    return abs(int(reactant.rank) - int(product.rank))


def select_endpoint_pairs(
    reactant_inputs: tuple[WorkflowStageInput, ...] | list[WorkflowStageInput],
    product_inputs: tuple[WorkflowStageInput, ...] | list[WorkflowStageInput],
    *,
    policy: EndpointPairingPolicy | None = None,
) -> tuple[EndpointPair, ...]:
    active_policy = policy or EndpointPairingPolicy()
    return _selection.select_endpoint_pairs(
        reactant_inputs,
        product_inputs,
        policy=active_policy,
        deps=_endpoint_pairing_deps(),
    )


__all__ = [
    "EndpointPair",
    "EndpointPairingPolicy",
    "select_endpoint_pairs",
]
