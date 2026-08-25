from __future__ import annotations

from typing import Any

from orca_auto.core.utils import normalize_text
from orca_auto.flow.orchestration.services import (
    OrchestrationServices,
    resolve_orchestration_services,
)
from orca_auto.flow.orchestration.support import (
    reaction_ts_guess_error_impl,
    select_valid_ts_guess_inputs,
)


def xtb_handoff_status_impl(
    contract: Any,
    *,
    services: OrchestrationServices | None = None,
) -> dict[str, str]:
    resolved = resolve_orchestration_services(services)
    inputs = select_valid_ts_guess_inputs(resolved, contract)
    if inputs:
        return {
            "status": "ready",
            "reason": "",
            "message": "",
            "artifact_path": normalize_text(inputs[0].artifact_path),
        }
    error = reaction_ts_guess_error_impl(contract, services=resolved)
    return {
        "status": "failed",
        "reason": error["reason"],
        "message": error["message"],
        "artifact_path": "",
    }


def _empty_xtb_handoff() -> dict[str, str]:
    return {
        "status": "",
        "reason": "",
        "message": "",
        "artifact_path": "",
    }


__all__ = [
    "xtb_handoff_status_impl",
]
