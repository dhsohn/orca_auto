from __future__ import annotations

from pathlib import Path

from orca_auto.core.utils.coercion import coerce_int_mapping

from ..contracts.orca import OrcaArtifactContract
from . import _orca_contract_status as _contract_status
from . import _orca_local_lookup as _local_lookup
from . import _orca_path_helpers as _path_helpers
from . import _orca_tracking
from ._orca_contract_assembly import (
    OrcaContractLoaderDeps,
    contract_from_orca_payload_impl,
    load_orca_artifact_contract_impl,
)


def _contract_loader_deps() -> OrcaContractLoaderDeps:
    return OrcaContractLoaderDeps(
        path_type=Path,
        tracked_runtime_context_fn=_orca_tracking.tracked_runtime_context_impl,
        tracked_artifact_context_fn=_orca_tracking.tracked_artifact_context_impl,
        find_queue_entry_fn=_local_lookup.find_queue_entry_impl,
        queue_entry_metadata_value_fn=_local_lookup.queue_entry_metadata_value_impl,
        resolve_candidate_path_fn=_path_helpers.resolve_candidate_path_impl,
        direct_dir_target_fn=_path_helpers.direct_dir_target_impl,
        load_json_dict_fn=_local_lookup.load_json_dict_impl,
        status_from_payloads_fn=_contract_status.status_from_payloads_impl,
        resolve_artifact_path_fn=_path_helpers.resolve_artifact_path_impl,
        derive_selected_input_xyz_fn=_path_helpers.derive_selected_input_xyz_impl,
        prefer_orca_optimized_xyz_fn=_path_helpers.prefer_orca_optimized_xyz_impl,
        coerce_resource_dict_fn=coerce_int_mapping,
        attempt_count_fn=_contract_status.attempt_count_impl,
        max_retries_fn=_contract_status.max_retries_impl,
        coerce_attempts_fn=_contract_status.coerce_attempts_impl,
        final_result_payload_fn=_contract_status.final_result_payload_impl,
        contract_cls=OrcaArtifactContract,
    )


def load_orca_artifact_contract(
    *,
    target: str,
    orca_allowed_root: str | Path | None = None,
    queue_id: str = "",
    run_id: str = "",
    reaction_dir: str = "",
) -> OrcaArtifactContract:
    deps = _contract_loader_deps()
    allowed_root = Path(orca_allowed_root).expanduser().resolve() if orca_allowed_root else None
    payload = _orca_tracking.load_orca_contract_payload_impl(
        index_root=allowed_root,
        target=target,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
    )
    if payload is not None:
        return contract_from_orca_payload_impl(
            payload=payload,
            target=target,
            queue_id=queue_id,
            run_id=run_id,
            reaction_dir=reaction_dir,
            deps=deps,
        )

    return load_orca_artifact_contract_impl(
        target=target,
        orca_allowed_root=allowed_root,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
        deps=deps,
    )


__all__ = [
    "load_orca_artifact_contract",
]
