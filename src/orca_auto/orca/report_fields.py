"""Pure projection of the ORCA-owned fields in a machine results bundle."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orca_auto.core.utils import copy_dict_or_empty as _dict
from orca_auto.core.utils import normalize_text


def report_result_fields(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the summary and results shared by publication and verification."""
    status = _dict(payload.get("status"))
    engine = _dict(payload.get("engine_payload"))
    final = _dict(engine.get("final_result"))
    attempts = engine.get("attempts")
    common = {
        "reason": normalize_text(final.get("reason") or status.get("reason") or ""),
        "analyzer_status": normalize_text(final.get("analyzer_status") or ""),
        "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
    }
    summary = {"status": normalize_text(status.get("state") or ""), **common}
    results = {
        "run_id": normalize_text(engine.get("run_id") or ""),
        **common,
        "resumed": bool(final.get("resumed", False)),
        "skipped_execution": bool(final.get("skipped_execution", False)),
        "runner_error": normalize_text(final.get("runner_error") or ""),
    }
    return summary, results
