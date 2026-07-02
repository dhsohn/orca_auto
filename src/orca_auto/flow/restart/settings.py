from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.utils import (
    mapping_or_empty as _coerce_mapping,
)
from orca_auto.core.utils import (
    normalize_text as _normalize_text,
)
from orca_auto.flow.orchestration.stage_views import WorkflowStageView, WorkflowTaskView
from orca_auto.flow.orchestration.workflow_builders import (
    _REACTION_TS_SEARCH_CREST_MANIFEST_DEFAULTS,
    _merge_manifest_defaults,
)

from ..manifest import (
    load_flow_manifest as _load_flow_manifest,
)
from ..manifest import (
    manifest_mapping as _manifest_mapping,
)
from ..manifest import (
    resolve_endpoint_pairing_manifest as _resolve_endpoint_pairing_manifest,
)
from ..manifest import (
    resolve_engine_manifest_with_presence as _resolve_engine_manifest,
)
from .stage_ops import (
    _REMATERIALIZED_ENGINES,
    _stage_metadata,
    _stage_task,
    _task_engine,
    _task_metadata,
    _task_payload,
)


def _positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolved_resource_request(manifest: dict[str, Any]) -> dict[str, int]:
    resources = _manifest_mapping(manifest.get("resources"))
    resolved: dict[str, int] = {}
    max_cores = _positive_int(resources.get("max_cores")) or _positive_int(
        manifest.get("max_cores")
    )
    max_memory_gb = _positive_int(resources.get("max_memory_gb")) or _positive_int(
        manifest.get("max_memory_gb")
    )
    if max_cores is not None:
        resolved["max_cores"] = max_cores
    if max_memory_gb is not None:
        resolved["max_memory_gb"] = max_memory_gb
    return resolved


def _flow_crest_mode(manifest: dict[str, Any], crest_manifest: dict[str, Any]) -> str:
    top_level = _normalize_text(manifest.get("crest_mode")).lower()
    if top_level:
        return "nci" if top_level == "nci" else "standard"
    section_mode = _normalize_text(crest_manifest.get("mode")).lower()
    if section_mode:
        return "nci" if section_mode == "nci" else "standard"
    return ""


def _workflow_template_name(payload: dict[str, Any], manifest: dict[str, Any]) -> str:
    return _normalize_text(payload.get("template_name") or manifest.get("workflow_type")).lower()


def _crest_manifest_with_defaults(
    *,
    template_name: str,
    crest_manifest: dict[str, Any],
) -> dict[str, Any]:
    defaults = (
        _REACTION_TS_SEARCH_CREST_MANIFEST_DEFAULTS if template_name == "reaction_ts_search" else {}
    )
    return _merge_manifest_defaults(defaults, crest_manifest)


def _set_mapping_field(parent: dict[str, Any], key: str, value: dict[str, Any]) -> None:
    if value:
        parent[key] = dict(value)
    else:
        parent.pop(key, None)


def _set_stage_manifest_overrides(stage: dict[str, Any], overrides: dict[str, Any]) -> None:
    task = _stage_task(stage)
    _set_mapping_field(_task_payload(task), "job_manifest_overrides", overrides)
    _set_mapping_field(_task_metadata(task), "job_manifest_overrides", overrides)
    _set_mapping_field(_stage_metadata(stage), "job_manifest_overrides", overrides)


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


def _request_parameters(payload: dict[str, Any]) -> dict[str, Any] | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        payload["metadata"] = metadata
    request = metadata.get("request")
    if not isinstance(request, dict):
        return None
    params = request.get("parameters")
    if not isinstance(params, dict):
        params = {}
        request["parameters"] = params
    return params


def _apply_restart_request_basics(
    params: dict[str, Any],
    *,
    resources: dict[str, int],
    priority: int | None,
    crest_mode: str,
) -> None:
    params.update(resources)
    if priority is not None:
        params["priority"] = priority
    if crest_mode:
        params["crest_mode"] = crest_mode


def _apply_restart_request_manifests(
    params: dict[str, Any],
    *,
    manifest: dict[str, Any],
    crest_present: bool,
    crest_overrides: dict[str, Any],
    xtb_present: bool,
    xtb_overrides: dict[str, Any],
    endpoint_pairing: dict[str, Any],
) -> None:
    if crest_present:
        _set_mapping_field(params, "crest_job_manifest", crest_overrides)
    if xtb_present:
        _set_mapping_field(params, "xtb_job_manifest", xtb_overrides)
    _set_mapping_field(params, "endpoint_pairing", endpoint_pairing)

    for key in (
        "max_crest_candidates",
        "max_xtb_stages",
        "max_xtb_handoff_retries",
        "max_orca_stages",
    ):
        parsed = _positive_int(manifest.get(key))
        if parsed is not None:
            params[key] = parsed


def _apply_orca_request_parameters(params: dict[str, Any], manifest: dict[str, Any]) -> None:
    orca_manifest = _manifest_mapping(manifest.get("orca"))
    route_line = _normalize_text(manifest.get("orca_route_line") or orca_manifest.get("route_line"))
    if route_line:
        params["orca_route_line"] = route_line
    charge = _optional_int(
        manifest.get("charge") if "charge" in manifest else orca_manifest.get("charge")
    )
    if charge is not None:
        params["charge"] = charge
    raw_multiplicity = (
        manifest.get("multiplicity")
        if "multiplicity" in manifest
        else orca_manifest.get("multiplicity")
    )
    multiplicity = _positive_int(raw_multiplicity)
    if multiplicity is not None:
        params["multiplicity"] = multiplicity


def _update_request_parameters(
    payload: dict[str, Any],
    *,
    manifest: dict[str, Any],
    resources: dict[str, int],
    priority: int | None,
    crest_mode: str,
    crest_present: bool,
    crest_overrides: dict[str, Any],
    xtb_present: bool,
    xtb_overrides: dict[str, Any],
    endpoint_pairing: dict[str, Any],
) -> None:
    params = _request_parameters(payload)
    if params is None:
        return

    _apply_restart_request_basics(
        params,
        resources=resources,
        priority=priority,
        crest_mode=crest_mode,
    )
    _apply_restart_request_manifests(
        params,
        manifest=manifest,
        crest_present=crest_present,
        crest_overrides=crest_overrides,
        xtb_present=xtb_present,
        xtb_overrides=xtb_overrides,
        endpoint_pairing=endpoint_pairing,
    )
    _apply_orca_request_parameters(params, manifest)


def _flow_restart_settings(workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_flow_manifest(workspace)
    if not manifest:
        return {"applied": False}
    return _flow_restart_settings_from_manifest(workspace, payload, manifest)


def _flow_restart_settings_from_manifest(
    workspace: Path,
    payload: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    template_name = _workflow_template_name(payload, manifest)
    crest_present, crest_manifest = _resolve_engine_manifest(workspace, manifest, "crest")
    xtb_present, xtb_manifest = _resolve_engine_manifest(workspace, manifest, "xtb")
    endpoint_pairing = _resolve_endpoint_pairing_manifest(manifest, xtb_manifest)
    crest_overrides = _crest_manifest_with_defaults(
        template_name=template_name,
        crest_manifest=crest_manifest,
    )
    priority = _positive_int(manifest.get("priority"))
    resources = _resolved_resource_request(manifest)
    crest_mode = _flow_crest_mode(manifest, crest_manifest)
    _update_request_parameters(
        payload,
        manifest=manifest,
        resources=resources,
        priority=priority,
        crest_mode=crest_mode,
        crest_present=crest_present,
        crest_overrides=crest_overrides,
        xtb_present=xtb_present,
        xtb_overrides=xtb_manifest,
        endpoint_pairing=endpoint_pairing,
    )
    return {
        "applied": True,
        "resources": resources,
        "priority": priority,
        "crest_present": crest_present,
        "crest_mode": crest_mode,
        "crest_overrides": crest_overrides,
        "xtb_present": xtb_present,
        "xtb_overrides": xtb_manifest,
        "endpoint_pairing": endpoint_pairing,
    }


def _apply_flow_restart_settings(stage: dict[str, Any], settings: dict[str, Any]) -> None:
    if not settings.get("applied"):
        return
    task = _stage_task(stage)
    stage_view = WorkflowStageView(stage)
    task_view = WorkflowTaskView(task)
    engine = _task_engine(task)
    _apply_resource_request(task, _coerce_mapping(settings.get("resources")))
    _apply_priority(
        task,
        settings.get("priority") if isinstance(settings.get("priority"), int) else None,
    )

    if engine == "crest":
        crest_mode = _normalize_text(settings.get("crest_mode"))
        if crest_mode:
            task_view.set_payload_field("mode", crest_mode)
            task_view.set_metadata_field("mode", crest_mode)
            stage_view.set_metadata_field("mode", crest_mode)
        if bool(settings.get("crest_present")):
            _set_stage_manifest_overrides(stage, _coerce_mapping(settings.get("crest_overrides")))
    elif engine == "xtb" and bool(settings.get("xtb_present")):
        _set_stage_manifest_overrides(stage, _coerce_mapping(settings.get("xtb_overrides")))


def _stage_should_rematerialize(stage: dict[str, Any], settings: dict[str, Any]) -> bool:
    if not settings.get("applied"):
        return False
    task = _stage_task(stage)
    engine = _task_engine(task)
    if engine not in _REMATERIALIZED_ENGINES:
        return False
    has_common_updates = bool(_coerce_mapping(settings.get("resources"))) or isinstance(
        settings.get("priority"),
        int,
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
