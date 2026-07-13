from __future__ import annotations

import heapq
from typing import Any

from .contracts import WorkflowStageInput
from .manifest import MAX_CREST_CANDIDATES, require_int


def _candidate_rank(item: WorkflowStageInput) -> int:
    return require_int(item.rank, field="endpoint pairing candidate rank", minimum=1)


def _metric_requested(policy: Any) -> bool:
    return bool(
        policy.enabled
        and (
            policy.comparison_atoms or policy.excluded_atoms or policy.max_distance_rmsd is not None
        )
    )


def _pair_score(
    *,
    rank_gap: int,
    distance_rmsd: float | None,
    metric_reason: str,
    policy: Any,
) -> tuple[float, str]:
    if distance_rmsd is None:
        return float(rank_gap), "rank"
    return float(distance_rmsd) + (float(rank_gap) * policy.rank_weight), metric_reason


def _pair_metadata(
    *,
    reactant: WorkflowStageInput,
    product: WorkflowStageInput,
    policy: Any,
    score: float,
    strategy: str,
    distance_rmsd: float | None,
    rank_gap: int,
    comparison_atoms: tuple[int, ...],
    sequence: int,
) -> dict[str, Any]:
    return {
        "enabled": policy.enabled,
        "strategy": strategy,
        "pairing_score": round(score, 6),
        "distance_fingerprint_rmsd": round(distance_rmsd, 6) if distance_rmsd is not None else None,
        "rank_gap": rank_gap,
        "reactant_rank": _candidate_rank(reactant),
        "product_rank": _candidate_rank(product),
        "reactant_artifact_path": reactant.artifact_path,
        "product_artifact_path": product.artifact_path,
        "comparison_atoms": list(comparison_atoms),
        "excluded_atoms": list(policy.excluded_atoms),
        "candidate_pair_order": sequence,
    }


def _candidate_pair(
    *,
    reactant: WorkflowStageInput,
    product: WorkflowStageInput,
    policy: Any,
    sequence: int,
    deps: Any,
) -> Any | None:
    rank_gap = deps._rank_gap(reactant, product)
    if policy.enabled and policy.max_rank_gap and rank_gap > policy.max_rank_gap:
        return None

    distance_rmsd: float | None = None
    metric_reason = "not_requested"
    comparison_atoms: tuple[int, ...] = ()
    if _metric_requested(policy):
        distance_rmsd, metric_reason, comparison_atoms = deps._distance_rmsd(
            reactant,
            product,
            atom_indices=policy.comparison_atoms,
            excluded_indices=policy.excluded_atoms,
        )
        if metric_reason in {"atom_count_mismatch", "element_order_mismatch"}:
            return None
        if distance_rmsd is None and not policy.fallback_to_ranked:
            return None
        if (
            distance_rmsd is not None
            and policy.max_distance_rmsd is not None
            and distance_rmsd > policy.max_distance_rmsd
        ):
            return None

    score, strategy = _pair_score(
        rank_gap=rank_gap,
        distance_rmsd=distance_rmsd,
        metric_reason=metric_reason,
        policy=policy,
    )
    return deps.EndpointPair(
        reactant=reactant,
        product=product,
        score=score,
        metadata=_pair_metadata(
            reactant=reactant,
            product=product,
            policy=policy,
            score=score,
            strategy=strategy,
            distance_rmsd=distance_rmsd,
            rank_gap=rank_gap,
            comparison_atoms=comparison_atoms,
            sequence=sequence,
        ),
    )


def _pair_sort_key(item: Any) -> tuple[float, int, int, int, int]:
    return (
        item.score,
        _candidate_rank(item.reactant) + _candidate_rank(item.product),
        _candidate_rank(item.reactant),
        _candidate_rank(item.product),
        int(item.metadata.get("candidate_pair_order", 0)),
    )


def select_endpoint_pairs(
    reactant_inputs: tuple[WorkflowStageInput, ...] | list[WorkflowStageInput],
    product_inputs: tuple[WorkflowStageInput, ...] | list[WorkflowStageInput],
    *,
    policy: Any,
    deps: Any,
) -> tuple[Any, ...]:
    if len(reactant_inputs) > MAX_CREST_CANDIDATES or len(product_inputs) > MAX_CREST_CANDIDATES:
        raise ValueError(
            f"endpoint pairing inputs exceed the per-side candidate limit of {MAX_CREST_CANDIDATES}"
        )

    def candidate_pairs() -> Any:
        sequence = 0
        for reactant in reactant_inputs:
            for product in product_inputs:
                sequence += 1
                pair = _candidate_pair(
                    reactant=reactant,
                    product=product,
                    policy=policy,
                    sequence=sequence,
                    deps=deps,
                )
                if pair is not None:
                    yield pair

    # Rank-gap ordering is also the deterministic, balanced fallback when
    # geometry pairing is disabled.  A capped Cartesian product must not spend
    # every slot on the first reactant conformer.
    if policy.max_pairs:
        return tuple(heapq.nsmallest(policy.max_pairs, candidate_pairs(), key=_pair_sort_key))
    return tuple(sorted(candidate_pairs(), key=_pair_sort_key))
