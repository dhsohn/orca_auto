from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from math import dist, isfinite, sqrt
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
from .manifest import MAX_CREST_CANDIDATES, require_int
from .xyz_utils import XYZFrame, load_output_xyz_frames


@dataclass(frozen=True)
class _EndpointPairingDeps:
    EndpointPair: Any
    _distance_rmsd: Any
    _rank_gap: Any


def _endpoint_pairing_deps(
    inputs: tuple[WorkflowStageInput, ...] = (),
) -> _EndpointPairingDeps:
    cached_frames: dict[int, XYZFrame | None] = {}
    inputs_by_path: dict[str, list[WorkflowStageInput]] = {}
    for item in inputs:
        path_text = _normalize_text(item.artifact_path)
        if path_text:
            inputs_by_path.setdefault(path_text, []).append(item)
        else:
            cached_frames[id(item)] = None
    for path_text, path_inputs in inputs_by_path.items():
        frames = load_output_xyz_frames(Path(path_text).expanduser())
        for item in path_inputs:
            cached_frames[id(item)] = _frame_for_input(item, frames=frames)

    def cached_distance_rmsd(*args: Any, **kwargs: Any) -> Any:
        return _distance_rmsd(
            *args,
            **kwargs,
            frame_for_input_fn=lambda item: cached_frames.get(id(item)),
        )

    return _EndpointPairingDeps(
        EndpointPair=EndpointPair,
        _distance_rmsd=cached_distance_rmsd,
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
MAX_ENDPOINT_PAIRING_COMPARISON_ATOMS = 256
_COMPARISON_ATOM_KEYS = ("comparison_atoms", "alignment_atoms", "rmsd_atoms", "atoms")
_EXCLUDED_ATOM_KEYS = (
    "moving_atoms",
    "mobile_atoms",
    "exclude_atoms",
    "excluded_atoms",
    "reaction_center_atoms",
)
_DISTANCE_LIMIT_KEYS = ("max_distance_rmsd", "max_reaction_center_rmsd", "max_rmsd")


def _reject_conflicting_aliases(
    raw: dict[str, Any],
    keys: tuple[str, ...],
    *,
    label: str,
) -> None:
    present = [key for key in keys if key in raw]
    if len(present) > 1:
        raise ValueError(
            f"endpoint_pairing.{label} aliases are mutually exclusive: {', '.join(present)}"
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
        return require_int(default, field=f"endpoint_pairing.{field_name}", minimum=0)
    return require_int(value, field=f"endpoint_pairing.{field_name}", minimum=0)


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
        index = require_int(raw, field=f"endpoint_pairing.{field_name} atom index", minimum=1)
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
        default_limit = require_int(
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
        _reject_conflicting_aliases(raw, _COMPARISON_ATOM_KEYS, label="comparison_atoms")
        _reject_conflicting_aliases(raw, _EXCLUDED_ATOM_KEYS, label="excluded_atoms")
        _reject_conflicting_aliases(raw, _DISTANCE_LIMIT_KEYS, label="max_distance_rmsd")
        if "enabled" in raw and "mode" in raw:
            raise ValueError(
                "endpoint_pairing.enabled and endpoint_pairing.mode are mutually exclusive"
            )
        if any(key in raw for key in _COMPARISON_ATOM_KEYS) and any(
            key in raw for key in _EXCLUDED_ATOM_KEYS
        ):
            raise ValueError(
                "endpoint_pairing comparison and exclusion atom selectors are mutually exclusive"
            )
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


def _frame_for_input(
    item: WorkflowStageInput,
    *,
    frames: Sequence[XYZFrame] | None = None,
) -> XYZFrame | None:
    path_text = _normalize_text(item.artifact_path)
    if not path_text:
        return None
    if frames is None:
        frames = load_output_xyz_frames(Path(path_text).expanduser())
    if not frames:
        return None
    raw_requested_index = item.metadata.get("source_frame_index", 0)
    requested_index = require_int(
        0 if raw_requested_index in (None, "") else raw_requested_index,
        field="endpoint pairing source_frame_index",
        minimum=0,
    )
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


def _validated_comparison_indices(
    indices: tuple[int, ...],
    *,
    excluded_indices: tuple[int, ...],
    natoms: int,
) -> tuple[int, ...]:
    invalid = tuple(index for index in (*indices, *excluded_indices) if index > natoms)
    if invalid:
        raise ValueError(
            "endpoint pairing atom indices must be within the endpoint geometry; "
            f"natoms={natoms}, invalid={list(invalid)!r}"
        )
    selected = _comparison_indices(
        indices,
        excluded_indices=excluded_indices,
        natoms=natoms,
    )
    if len(selected) < 2:
        raise ValueError("endpoint pairing requires at least 2 effective comparison atoms")
    if len(selected) > MAX_ENDPOINT_PAIRING_COMPARISON_ATOMS:
        raise ValueError(
            "endpoint pairing comparison atom count exceeds the limit of "
            f"{MAX_ENDPOINT_PAIRING_COMPARISON_ATOMS}"
        )
    return selected


def validate_endpoint_pairing_atom_budget(
    policy: EndpointPairingPolicy,
    *atom_counts: int,
) -> None:
    """Reject metric policies whose effective comparison set is too expensive."""

    metric_requested = policy.enabled and bool(
        policy.comparison_atoms or policy.excluded_atoms or policy.max_distance_rmsd is not None
    )
    if not metric_requested:
        return
    for atom_count in atom_counts:
        _validated_comparison_indices(
            policy.comparison_atoms,
            excluded_indices=policy.excluded_atoms,
            natoms=atom_count,
        )


def _distance(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return dist(left, right)


def _distance_rmsd(
    reactant: WorkflowStageInput,
    product: WorkflowStageInput,
    *,
    atom_indices: tuple[int, ...],
    excluded_indices: tuple[int, ...],
    frame_for_input_fn: Any = _frame_for_input,
) -> tuple[float | None, str, tuple[int, ...]]:
    reactant_frame = frame_for_input_fn(reactant)
    product_frame = frame_for_input_fn(product)
    if reactant_frame is None or product_frame is None:
        return None, "missing_or_invalid_xyz", ()
    if reactant_frame.natoms != product_frame.natoms:
        return None, "atom_count_mismatch", ()
    if _frame_atom_sequence(reactant_frame) != _frame_atom_sequence(product_frame):
        return None, "element_order_mismatch", ()

    indices = _validated_comparison_indices(
        atom_indices,
        excluded_indices=excluded_indices,
        natoms=reactant_frame.natoms,
    )

    reactant_coords = _frame_coordinates(reactant_frame)
    product_coords = _frame_coordinates(product_frame)
    zero_based = tuple(index - 1 for index in indices)
    squared_scale = 0.0
    scaled_squared_sum = 1.0
    distance_count = 0
    for outer, left_index in enumerate(zero_based):
        for right_index in zero_based[outer + 1 :]:
            reactant_distance = _distance(
                reactant_coords[left_index],
                reactant_coords[right_index],
            )
            product_distance = _distance(
                product_coords[left_index],
                product_coords[right_index],
            )
            delta = abs(reactant_distance - product_distance)
            if (
                not isfinite(reactant_distance)
                or not isfinite(product_distance)
                or not isfinite(delta)
            ):
                return None, "nonfinite_distance_metric", indices
            if delta:
                if squared_scale < delta:
                    scaled_squared_sum = 1.0 + scaled_squared_sum * (squared_scale / delta) ** 2
                    squared_scale = delta
                else:
                    scaled_squared_sum += (delta / squared_scale) ** 2
            distance_count += 1
    if distance_count == 0:
        return None, "empty_distance_fingerprint", indices
    return (
        squared_scale * sqrt(scaled_squared_sum / distance_count),
        "distance_fingerprint",
        indices,
    )


def _rank_gap(reactant: WorkflowStageInput, product: WorkflowStageInput) -> int:
    return abs(
        require_int(reactant.rank, field="reactant candidate rank", minimum=1)
        - require_int(product.rank, field="product candidate rank", minimum=1)
    )


def select_endpoint_pairs(
    reactant_inputs: tuple[WorkflowStageInput, ...] | list[WorkflowStageInput],
    product_inputs: tuple[WorkflowStageInput, ...] | list[WorkflowStageInput],
    *,
    policy: EndpointPairingPolicy | None = None,
) -> tuple[EndpointPair, ...]:
    active_policy = policy or EndpointPairingPolicy()
    if len(reactant_inputs) > MAX_CREST_CANDIDATES or len(product_inputs) > MAX_CREST_CANDIDATES:
        raise ValueError(
            f"endpoint pairing inputs exceed the per-side candidate limit of {MAX_CREST_CANDIDATES}"
        )
    metric_requested = active_policy.enabled and bool(
        active_policy.comparison_atoms
        or active_policy.excluded_atoms
        or active_policy.max_distance_rmsd is not None
    )
    inputs = tuple(reactant_inputs) + tuple(product_inputs) if metric_requested else ()
    return _selection.select_endpoint_pairs(
        reactant_inputs,
        product_inputs,
        policy=active_policy,
        deps=_endpoint_pairing_deps(inputs),
    )


__all__ = [
    "EndpointPair",
    "EndpointPairingPolicy",
    "MAX_ENDPOINT_PAIRING_COMPARISON_ATOMS",
    "select_endpoint_pairs",
    "validate_endpoint_pairing_atom_budget",
]
