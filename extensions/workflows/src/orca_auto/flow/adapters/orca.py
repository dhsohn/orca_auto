from __future__ import annotations

from pathlib import Path

from ..contracts.orca import OrcaArtifactContract
from . import _orca_tracking
from ._orca_contract_assembly import (
    contract_from_orca_payload_impl,
    load_orca_artifact_contract_impl,
)


def load_orca_artifact_contract(
    *,
    target: str,
    orca_allowed_root: str | Path | None = None,
    queue_id: str = "",
    run_id: str = "",
    reaction_dir: str = "",
) -> OrcaArtifactContract:
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
        )

    return load_orca_artifact_contract_impl(
        target=target,
        orca_allowed_root=allowed_root,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
    )


__all__ = [
    "load_orca_artifact_contract",
]
