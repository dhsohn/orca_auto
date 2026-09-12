from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.config.bounded_yaml import YAML_CONFIG_LOAD_EXCEPTIONS
from orca_auto.core.utils import normalize_text
from orca_auto.flow.orchestration.services import (
    OrchestrationServices,
    resolve_orchestration_services,
)


def _runtime_paths_for_engine(
    config_path: str,
    *,
    engine: str,
    services: OrchestrationServices | None = None,
) -> dict[str, Path]:
    o = resolve_orchestration_services(services)
    return o.engines.engine_runtime_paths(config_path, engine=engine)


def submission_target_impl(stage: dict[str, Any]) -> str:
    stage_metadata = stage.get("metadata")
    if isinstance(stage_metadata, dict):
        queue_id = normalize_text(stage_metadata.get("queue_id"))
        if queue_id:
            return queue_id
    task = stage.get("task")
    if isinstance(task, dict):
        submission_result = task.get("submission_result")
        if isinstance(submission_result, dict):
            parsed = submission_result.get("parsed_stdout")
            if isinstance(parsed, dict):
                for key in ("job_id", "queue_id"):
                    value = normalize_text(parsed.get(key))
                    if value:
                        return value
    return ""


def load_config_root_impl(
    config_path: str | None,
    *,
    engine: str = "orca",
    services: OrchestrationServices | None = None,
) -> Path | None:
    text = normalize_text(config_path)
    if not text:
        return None
    try:
        return _runtime_paths_for_engine(text, engine=engine, services=services)["allowed_root"]
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        return None


def required_stage_budget(params: dict[str, Any], key: str) -> Any:
    """Fail closed when a persisted stage budget is missing.

    Creation always records the stage budgets in the durable payload; a
    payload without one is corrupt or hand-edited, and guessing a budget
    would silently change how far a stored workflow expands.
    """

    value = params.get(key)
    if value is None:
        raise ValueError(
            f"workflow payload is missing parameters.{key}; refusing to guess a stage budget"
        )
    return value


__all__ = [
    "load_config_root_impl",
    "submission_target_impl",
]
