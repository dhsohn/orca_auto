from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.queue.publication import QUEUE_SUBMISSION_INTENT_KEY
from orca_auto.core.utils import mapping_or_empty as _coerce_mapping
from orca_auto.core.utils import normalize_text as _normalize_text
from orca_auto.flow.contracts.workflow import (
    INTERACTION_COMPLEX_SP_ROLE,
    INTERACTION_CONFIG_FINGERPRINT_KEY,
    INTERACTION_FRAGMENT_ROLE,
    is_valid_interaction_stage_contract,
)
from orca_auto.flow.orca_stage_validation import validate_workflow_orca_route
from orca_auto.flow.orchestration.charge_spin import manifest_with_charge_spin
from orca_auto.flow.orchestration.stage_views import WorkflowStageView, WorkflowTaskView

from . import stages as _restart_stages
from .orca_input import rematerialize_orca_restart_input

_RESTARTABLE_STAGE_STATUSES = frozenset(
    {
        "failed",
        "cancelled",
        "cancel_failed",
        "submission_failed",
    }
)
_ACTIVE_STAGE_STATUSES = frozenset(
    {
        "queued",
        "running",
        "submitted",
        "cancel_requested",
    }
)
_STALE_STAGE_METADATA_KEYS = frozenset(
    {
        "analyzer_status",
        "cancel_requested",
        "child_job_id",
        "completed_at",
        "latest_known_path",
        "orca_attempts",
        "orca_current_attempt_number",
        "orca_final_result",
        "orca_latest_attempt_number",
        "orca_latest_attempt_status",
        "optimized_xyz_path",
        "queue_id",
        "queue_status",
        "reason",
        "run_id",
        QUEUE_SUBMISSION_INTENT_KEY,
        "state_status",
        "submission_error_detail",
        "submission_status",
        "submitted_at",
    }
)
_STALE_TASK_PAYLOAD_KEYS = frozenset(
    {
        "last_out_path",
        "optimized_xyz_path",
        "orca_latest_attempt_inp",
        "orca_latest_attempt_out",
    }
)
_REMATERIALIZED_ENGINES = frozenset({"crest", "xtb"})
_REMATERIALIZED_TASK_PAYLOAD_KEYS = frozenset(
    {"job_dir", "selected_input_xyz", "secondary_input_xyz"}
)


_RESTART_STAGE_CONTEXT = _restart_stages.RestartStageContext(
    active_stage_statuses=_ACTIVE_STAGE_STATUSES,
    rematerialized_engines=_REMATERIALIZED_ENGINES,
    rematerialized_task_payload_keys=_REMATERIALIZED_TASK_PAYLOAD_KEYS,
    restartable_stage_statuses=_RESTARTABLE_STAGE_STATUSES,
    stale_stage_metadata_keys=_STALE_STAGE_METADATA_KEYS,
    stale_task_payload_keys=_STALE_TASK_PAYLOAD_KEYS,
)


def _stage_task(stage: dict[str, Any]) -> dict[str, Any]:
    return WorkflowStageView(stage).ensure_task().raw


def _stage_metadata(stage: dict[str, Any]) -> dict[str, Any]:
    return WorkflowStageView(stage).metadata()


def _task_metadata(task: dict[str, Any]) -> dict[str, Any]:
    return WorkflowTaskView(task).metadata()


def _task_payload(task: dict[str, Any]) -> dict[str, Any]:
    return WorkflowTaskView(task).payload()


def _task_engine(task: dict[str, Any]) -> str:
    return _RESTART_STAGE_CONTEXT.task_engine(WorkflowTaskView(task))


def _set_stage_manifest_overrides(stage: dict[str, Any], overrides: dict[str, Any]) -> None:
    task = _stage_task(stage)
    containers = (_task_payload(task), _task_metadata(task), _stage_metadata(stage))
    for container in containers:
        if overrides:
            container["job_manifest_overrides"] = dict(overrides)
        else:
            container.pop("job_manifest_overrides", None)


def _apply_resource_request(task: dict[str, Any], resources: dict[str, int]) -> None:
    WorkflowTaskView(task).update_resource_request(resources)


def _apply_priority(task: dict[str, Any], priority: int | None) -> None:
    if priority is None:
        return
    task_view = WorkflowTaskView(task)
    enqueue_payload = task_view.enqueue_payload()
    task_view.update_enqueue_payload({"priority": priority})
    argv = enqueue_payload.get("command_argv")
    updated_argv = False
    if isinstance(argv, list) and "--priority" in argv:
        index = argv.index("--priority")
        if index + 1 < len(argv):
            argv[index + 1] = str(priority)
            updated_argv = True
    elif isinstance(argv, list):
        for index, part in enumerate(argv):
            if isinstance(part, str) and part.startswith("priority="):
                argv[index] = f"priority={priority}"
                updated_argv = True
                break
    if updated_argv and isinstance(argv, list) and isinstance(enqueue_payload.get("command"), str):
        enqueue_payload["command"] = " ".join(str(part) for part in argv)


def _merge_stage_charge_spin(stage: dict[str, Any], settings: dict[str, Any]) -> None:
    """Update only the electronic state in a stage's existing overrides.

    Used when the flow manifest states charge/multiplicity without an engine
    section: replacing the overrides with a section that was never written
    would drop the stage's original manifest keys, so the new charge/uhf are
    merged in (and stale ones removed when the new state is neutral).
    """
    task = _stage_task(stage)
    existing = dict(_coerce_mapping(_task_payload(task).get("job_manifest_overrides")))
    existing.pop("charge", None)
    existing.pop("uhf", None)
    merged = manifest_with_charge_spin(
        charge=settings.get("charge", 0),
        multiplicity=settings.get("multiplicity", 1),
        manifest_overrides=existing,
    )
    _set_stage_manifest_overrides(stage, merged or {})


def _apply_flow_restart_settings(
    stage: dict[str, Any],
    settings: dict[str, Any],
    *,
    restart_allowed_root: Path,
    workflow_stages: list[dict[str, Any]],
    created_restart_dirs: list[Path] | None = None,
) -> None:
    if not settings.get("applied"):
        return
    task = _stage_task(stage)
    stage_view = WorkflowStageView(stage)
    task_view = WorkflowTaskView(task)
    engine = _task_engine(task)
    stage_metadata = _stage_metadata(stage)
    interaction_role = _normalize_text(stage_metadata.get("role"))

    expected_interaction_fingerprint = _normalize_text(
        settings.get("interaction_energy_fingerprint")
    )
    if engine == "orca" and is_valid_interaction_stage_contract(
        stage,
        workflow_stages,
        expected_config_fingerprint=expected_interaction_fingerprint,
    ):
        interaction_cfg = settings.get("interaction_energy")
        if not isinstance(interaction_cfg, dict):
            raise ValueError("disabled interaction-energy stages must be retired before restart")
        expected_fingerprint = _normalize_text(settings.get("interaction_energy_fingerprint"))
        if (
            not expected_fingerprint
            or _normalize_text(stage_metadata.get(INTERACTION_CONFIG_FINGERPRINT_KEY))
            != expected_fingerprint
        ):
            raise ValueError(
                "interaction-energy restart config generation does not match the stage"
            )

        interaction_resources = dict(_coerce_mapping(settings.get("resources")))
        for key in ("max_cores", "max_memory_gb"):
            if isinstance(interaction_cfg.get(key), int):
                interaction_resources[key] = int(interaction_cfg[key])
        _apply_resource_request(task, interaction_resources)
        interaction_priority = (
            int(interaction_cfg["priority"])
            if isinstance(interaction_cfg.get("priority"), int)
            else settings.get("priority")
        )
        _apply_priority(
            task,
            interaction_priority if isinstance(interaction_priority, int) else None,
        )

        charge = int(settings.get("charge", 0))
        multiplicity = int(settings.get("multiplicity", 1))
        if interaction_role == INTERACTION_FRAGMENT_ROLE:
            fragment_index = stage_metadata.get("fragment_index")
            fragments = interaction_cfg.get("fragments")
            if (
                not isinstance(fragment_index, int)
                or isinstance(fragment_index, bool)
                or not isinstance(fragments, list)
                or fragment_index < 0
                or fragment_index >= len(fragments)
                or not isinstance(fragments[fragment_index], dict)
            ):
                raise ValueError("interaction fragment restart has no matching current descriptor")
            fragment = fragments[fragment_index]
            charge = int(fragment["charge"])
            multiplicity = int(fragment["multiplicity"])
            stage_metadata.update(
                {
                    "fragment_label": fragment["label"],
                    "fragment_charge": charge,
                    "fragment_multiplicity": multiplicity,
                    "fragment_atom_indices": list(fragment["atom_indices"]),
                }
            )
        elif interaction_role != INTERACTION_COMPLEX_SP_ROLE:
            raise ValueError(f"unknown interaction-energy stage role: {interaction_role}")

        interaction_settings = dict(settings)
        interaction_settings.update(
            {
                "orca_input_updates": True,
                "orca_route_line_present": True,
                "orca_route_line": interaction_cfg["sp_route_line"],
                "orca_charge": charge,
                "orca_multiplicity": multiplicity,
                "resources": interaction_resources,
            }
        )
        task_view.update_enqueue_payload(
            {
                key: int(interaction_resources[key])
                for key in ("max_cores", "max_memory_gb")
                if key in interaction_resources
            }
        )
        rematerialize_orca_restart_input(
            stage,
            interaction_settings,
            allowed_root=restart_allowed_root,
            created_restart_dirs=created_restart_dirs,
        )
        return

    _apply_resource_request(task, _coerce_mapping(settings.get("resources")))
    _apply_priority(
        task,
        settings.get("priority") if isinstance(settings.get("priority"), int) else None,
    )

    electronic_state_present = bool(settings.get("electronic_state_present"))
    if engine == "crest":
        crest_mode = _normalize_text(settings.get("crest_mode"))
        if crest_mode:
            task_view.set_payload_field("mode", crest_mode)
            task_view.set_metadata_field("mode", crest_mode)
            stage_view.set_metadata_field("mode", crest_mode)
        if bool(settings.get("crest_present")):
            _set_stage_manifest_overrides(stage, _coerce_mapping(settings.get("crest_overrides")))
        elif electronic_state_present:
            _merge_stage_charge_spin(stage, settings)
    elif engine == "xtb":
        max_handoff_retries = settings.get("max_xtb_handoff_retries")
        if isinstance(max_handoff_retries, int) and not isinstance(
            max_handoff_retries,
            bool,
        ):
            task_view.set_payload_field("max_handoff_retries", max_handoff_retries)
            task_view.set_metadata_field("max_handoff_retries", max_handoff_retries)
            stage_view.set_metadata_field("max_handoff_retries", max_handoff_retries)
        if bool(settings.get("xtb_present")):
            _set_stage_manifest_overrides(stage, _coerce_mapping(settings.get("xtb_overrides")))
        elif electronic_state_present:
            _merge_stage_charge_spin(stage, settings)
    elif engine == "orca":
        resources = _coerce_mapping(settings.get("resources"))
        task_view.update_enqueue_payload(
            {key: int(resources[key]) for key in ("max_cores", "max_memory_gb") if key in resources}
        )
        orca_settings = dict(settings)
        route_setting = (
            "orca_optts_route_line" if task_view.kind() == "optts_freq" else "orca_route_line"
        )
        route_line_present = bool(settings.get(f"{route_setting}_present"))
        route_line = _normalize_text(settings.get(route_setting))
        if route_line_present:
            validate_workflow_orca_route(
                task_kind=task_view.kind(),
                route_line=route_line,
            )
        orca_settings.update(
            {
                "orca_route_line_present": route_line_present,
                "orca_route_line": route_line,
                "orca_input_updates": route_line_present
                or bool(settings.get("electronic_state_present"))
                or bool(resources),
            }
        )
        if resources:
            orca_settings["resources"] = dict(task_view.resource_request())
        rematerialize_orca_restart_input(
            stage,
            orca_settings,
            allowed_root=restart_allowed_root,
            created_restart_dirs=created_restart_dirs,
        )


def _stage_should_rematerialize(stage: dict[str, Any], settings: dict[str, Any]) -> bool:
    if not settings.get("applied"):
        return False
    task = _stage_task(stage)
    engine = _task_engine(task)
    if engine not in _REMATERIALIZED_ENGINES:
        return False
    has_common_updates = (
        bool(_coerce_mapping(settings.get("resources")))
        or isinstance(settings.get("priority"), int)
        # A stated electronic state changes the job manifest, so the stage
        # must rebuild its job dir even without resource/priority updates.
        or bool(settings.get("electronic_state_present"))
    )
    if engine == "crest":
        return (
            has_common_updates
            or bool(settings.get("crest_present"))
            or bool(_normalize_text(settings.get("crest_mode")))
        )
    if engine == "xtb":
        return has_common_updates or bool(settings.get("xtb_present"))
    return False


def _stage_needs_restart(stage: dict[str, Any]) -> bool:
    return _restart_stages.stage_needs_restart(stage, context=_RESTART_STAGE_CONTEXT)


def _active_stage_rows(payload: dict[str, Any]) -> list[dict[str, str]]:
    return _restart_stages.active_stage_rows(payload, context=_RESTART_STAGE_CONTEXT)


def _active_restart_error(workflow_id: str, rows: list[dict[str, str]]) -> ValueError:
    return _restart_stages.active_restart_error(workflow_id, rows)


def _clear_phase_notification_state(
    metadata: dict[str, Any], restarted_stages: list[dict[str, str]]
) -> None:
    _restart_stages.clear_phase_notification_state(
        metadata,
        restarted_stages,
    )


def _reset_stage_for_restart(
    stage: dict[str, Any],
    *,
    rematerialize: bool = False,
) -> dict[str, str]:
    return _restart_stages.reset_stage_for_restart(
        stage,
        rematerialize=rematerialize,
        context=_RESTART_STAGE_CONTEXT,
    )
