from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.utils import (
    mapping_or_empty as _coerce_mapping,
)
from orca_auto.core.utils import (
    normalize_text as _normalize_text,
)
from orca_auto.flow.orchestration.charge_spin import manifest_with_charge_spin
from orca_auto.flow.orchestration.stage_views import WorkflowStageView, WorkflowTaskView
from orca_auto.flow.orchestration.workflow_builders import (
    _REACTION_TS_SEARCH_CREST_MANIFEST_DEFAULTS,
    _merge_manifest_defaults,
)
from orca_auto.flow.xyz_utils import load_xyz_atom_sequence
from orca_auto.orca.report.interaction_energy import (
    validate_fragment_electronic_states,
    validate_fragment_partition,
)

from ..manifest import (
    interaction_energy_config_fingerprint as _interaction_energy_config_fingerprint,
)
from ..manifest import (
    load_flow_manifest as _load_flow_manifest,
)
from ..manifest import (
    manifest_mapping as _manifest_mapping,
)
from ..manifest import (
    normalize_interaction_energy_block as _normalize_interaction_energy_block,
)
from ..manifest import (
    normalize_rmsd_dedup_block as _normalize_rmsd_dedup_block,
)
from ..manifest import optional_positive_float as _optional_positive_float
from ..manifest import (
    resolve_endpoint_pairing_manifest as _resolve_endpoint_pairing_manifest,
)
from ..manifest import (
    resolve_engine_manifest_with_presence as _resolve_engine_manifest,
)
from ..manifest import (
    validate_conformer_postprocessing_template as _validate_conformer_postprocessing_template,
)
from ..manifest import (
    validate_interaction_energy_state_balance as _validate_interaction_energy_state_balance,
)
from .orca_input import rematerialize_orca_restart_input
from .stage_ops import (
    _REMATERIALIZED_ENGINES,
    _stage_metadata,
    _stage_task,
    _task_engine,
    _task_metadata,
    _task_payload,
)

_INTERACTION_ROLE_PREFIX = "interaction_"
_INTERACTION_COMPLEX_ROLE = "interaction_complex_sp"
_INTERACTION_FRAGMENT_ROLE = "interaction_fragment"
_INTERACTION_CONFIG_FINGERPRINT_KEY = "interaction_config_fingerprint"


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


def _interaction_source_atom_sequence(workspace: Path, payload: dict[str, Any]) -> tuple[str, ...]:
    """Load the immutable copied complex input used by interaction fragments."""
    request = _coerce_mapping(_coerce_mapping(payload.get("metadata")).get("request"))
    raw_artifacts = request.get("source_artifacts")
    candidates: list[Path] = []
    if isinstance(raw_artifacts, list):
        for raw in raw_artifacts:
            artifact = _coerce_mapping(raw)
            if _normalize_text(artifact.get("kind")) != "input_xyz":
                continue
            path_text = _normalize_text(artifact.get("path"))
            if path_text:
                candidates.append(Path(path_text))
    candidates.extend((workspace / "input.xyz", workspace / "inputs" / "input.xyz"))
    resolved_workspace = workspace.expanduser().resolve()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
            resolved.relative_to(resolved_workspace)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return load_xyz_atom_sequence(resolved)
    raise ValueError(
        "interaction_energy restart cannot validate fragments without the copied input XYZ"
    )


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


def _request_parameters(payload: dict[str, Any]) -> dict[str, Any]:
    # Create the request/parameters structure when absent (as with metadata):
    # an older or hand-edited workflow.json may have no request block, and a
    # flow.yaml restart must still be able to establish charge/multiplicity
    # (and other parameters) so the electronic state reaches rematerialized and
    # later-appended engine stages instead of defaulting to neutral singlet.
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        payload["metadata"] = metadata
    request = metadata.get("request")
    if not isinstance(request, dict):
        request = {}
        metadata["request"] = request
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


def _manifest_electronic_state(manifest: dict[str, Any]) -> tuple[int | None, int | None]:
    """(charge, multiplicity) the flow manifest states, ``None`` when absent."""
    orca_manifest = _manifest_mapping(manifest.get("orca"))
    charge = _optional_int(
        manifest.get("charge") if "charge" in manifest else orca_manifest.get("charge")
    )
    raw_multiplicity = (
        manifest.get("multiplicity")
        if "multiplicity" in manifest
        else orca_manifest.get("multiplicity")
    )
    return charge, _positive_int(raw_multiplicity)


def _apply_orca_request_parameters(params: dict[str, Any], manifest: dict[str, Any]) -> None:
    orca_manifest = _manifest_mapping(manifest.get("orca"))
    route_line = _normalize_text(manifest.get("orca_route_line") or orca_manifest.get("route_line"))
    if route_line:
        params["orca_route_line"] = route_line
    charge, multiplicity = _manifest_electronic_state(manifest)
    if charge is not None:
        params["charge"] = charge
    if multiplicity is not None:
        params["multiplicity"] = multiplicity


def _manifest_orca_route_line(manifest: dict[str, Any]) -> str:
    orca_manifest = _manifest_mapping(manifest.get("orca"))
    return _normalize_text(manifest.get("orca_route_line") or orca_manifest.get("route_line"))


def _update_request_parameters(
    payload: dict[str, Any],
    *,
    template_name: str,
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
    if "boltzmann_temperature_k" in manifest:
        temperature = _optional_positive_float(manifest, "boltzmann_temperature_k")
        if temperature is None:
            params.pop("boltzmann_temperature_k", None)
        else:
            params["boltzmann_temperature_k"] = temperature
    if "interaction_energy" in manifest:
        interaction_energy = _normalize_interaction_energy_block(manifest.get("interaction_energy"))
        if interaction_energy is None:
            params.pop("interaction_energy", None)
        else:
            params["interaction_energy"] = interaction_energy
    if "rmsd_dedup" in manifest:
        rmsd_dedup = _normalize_rmsd_dedup_block(manifest.get("rmsd_dedup"))
        if rmsd_dedup is None:
            params.pop("rmsd_dedup", None)
        else:
            params["rmsd_dedup"] = rmsd_dedup
    # Revalidate the complete effective durable state, not only keys changed by
    # this restart manifest. This also closes legacy/manual payload injection.
    interaction_energy = _normalize_interaction_energy_block(params.get("interaction_energy"))
    rmsd_dedup = _normalize_rmsd_dedup_block(params.get("rmsd_dedup"))
    _validate_conformer_postprocessing_template(
        template_name,
        interaction_energy=interaction_energy,
        rmsd_dedup=rmsd_dedup,
    )
    if interaction_energy is None:
        params.pop("interaction_energy", None)
    else:
        params["interaction_energy"] = interaction_energy
    if rmsd_dedup is None:
        params.pop("rmsd_dedup", None)
    else:
        params["rmsd_dedup"] = rmsd_dedup


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
        template_name=template_name,
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
    # The request parameters now hold the effective charge/multiplicity
    # (manifest-updated or pre-existing). The stage overrides written back by
    # _apply_flow_restart_settings REPLACE whatever a stage carried, so they
    # must re-inject charge/uhf themselves — otherwise restarting a charged
    # or open-shell workflow strips the electronic state from every
    # rematerialized CREST/xTB stage and screens on the neutral-singlet
    # surface. Only the stage-facing overrides are enriched: the request's
    # crest/xtb manifests keep user-manifest semantics (the stage builders
    # inject on append).
    # _update_request_parameters ran just above and, when the manifest states
    # charge/multiplicity, wrote them into the (now always-present) params. Fall
    # back to the manifest values directly too, so an older workflow.json whose
    # params never carried an electronic state still restarts on the right one.
    params = _request_parameters(payload)
    manifest_charge, manifest_multiplicity = _manifest_electronic_state(manifest)
    charge = params.get("charge", manifest_charge if manifest_charge is not None else 0)
    multiplicity = params.get(
        "multiplicity", manifest_multiplicity if manifest_multiplicity is not None else 1
    )
    interaction_cfg = params.get("interaction_energy")
    interaction_cfg = interaction_cfg if isinstance(interaction_cfg, dict) else None
    rmsd_cfg = params.get("rmsd_dedup")
    rmsd_cfg = rmsd_cfg if isinstance(rmsd_cfg, dict) else None
    if interaction_cfg is not None:
        _validate_interaction_energy_state_balance(
            interaction_cfg,
            complex_charge=int(charge),
            complex_multiplicity=int(multiplicity),
        )
        atom_symbols = _interaction_source_atom_sequence(workspace, payload)
        fragments = interaction_cfg.get("fragments")
        if not isinstance(fragments, list):
            raise ValueError("interaction_energy restart fragments are unavailable")
        partition_reason = validate_fragment_partition(
            [fragment["atom_indices"] for fragment in fragments], len(atom_symbols)
        )
        if partition_reason:
            raise ValueError(
                f"interaction_energy restart fragments do not partition input.xyz: "
                f"{partition_reason}"
            )
        state_reason = validate_fragment_electronic_states(atom_symbols, fragments)
        if state_reason:
            raise ValueError(
                f"interaction_energy restart fragment state is impossible: {state_reason}"
            )
    interaction_fingerprint = (
        _interaction_energy_config_fingerprint(
            interaction_cfg,
            complex_charge=int(charge),
            complex_multiplicity=int(multiplicity),
            rmsd_dedup=rmsd_cfg,
        )
        if interaction_cfg is not None
        else ""
    )
    if interaction_cfg is not None:
        for raw_stage in payload.get("stages", []):
            if not isinstance(raw_stage, dict):
                continue
            metadata = _stage_metadata(raw_stage)
            role = _normalize_text(metadata.get("role"))
            if not role.startswith(_INTERACTION_ROLE_PREFIX):
                continue
            if (
                _normalize_text(metadata.get(_INTERACTION_CONFIG_FINGERPRINT_KEY))
                != interaction_fingerprint
            ):
                raise ValueError(
                    "interaction_energy scientific settings cannot change after its stages "
                    "were materialized; disable the feature or start a new workflow"
                )
    route_line = _manifest_orca_route_line(manifest)
    return {
        "applied": True,
        "resources": resources,
        "priority": priority,
        # An electronic-state-only flow.yaml (no crest:/xtb: sections) must
        # still reach the engine stages: the apply/rematerialize gates key off
        # this flag too, merging charge/uhf into each stage's existing
        # overrides instead of replacing them with an engine section that was
        # never written.
        "electronic_state_present": (
            manifest_charge is not None or manifest_multiplicity is not None
        ),
        "charge": charge,
        "multiplicity": multiplicity,
        "interaction_energy": interaction_cfg,
        "interaction_energy_fingerprint": interaction_fingerprint,
        "interaction_energy_disabled": (
            "interaction_energy" in manifest and interaction_cfg is None
        ),
        "orca_charge": manifest_charge,
        "orca_multiplicity": manifest_multiplicity,
        "orca_route_line_present": bool(route_line),
        "orca_route_line": route_line or _normalize_text(params.get("orca_route_line")),
        "orca_input_updates": bool(route_line)
        or bool(manifest_charge is not None or manifest_multiplicity is not None)
        or bool(resources),
        "crest_present": crest_present,
        "crest_mode": crest_mode,
        "crest_overrides": manifest_with_charge_spin(
            charge=charge,
            multiplicity=multiplicity,
            manifest_overrides=crest_overrides,
        )
        or {},
        "xtb_present": xtb_present,
        "xtb_overrides": manifest_with_charge_spin(
            charge=charge,
            multiplicity=multiplicity,
            manifest_overrides=xtb_manifest,
        )
        or {},
        "endpoint_pairing": endpoint_pairing,
    }


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

    if engine == "orca" and interaction_role.startswith(_INTERACTION_ROLE_PREFIX):
        interaction_cfg = settings.get("interaction_energy")
        if not isinstance(interaction_cfg, dict):
            raise ValueError("disabled interaction-energy stages must be retired before restart")
        expected_fingerprint = _normalize_text(settings.get("interaction_energy_fingerprint"))
        if (
            not expected_fingerprint
            or _normalize_text(stage_metadata.get(_INTERACTION_CONFIG_FINGERPRINT_KEY))
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
        if interaction_role == _INTERACTION_FRAGMENT_ROLE:
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
        elif interaction_role != _INTERACTION_COMPLEX_ROLE:
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
