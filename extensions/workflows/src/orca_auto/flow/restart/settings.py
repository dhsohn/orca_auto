from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.utils import (
    mapping_or_empty as _coerce_mapping,
)
from orca_auto.core.utils import (
    normalize_text as _normalize_text,
)
from orca_auto.core.utils import (
    set_mapping_field as _set_mapping_field,
)
from orca_auto.flow.conformer_selection import orca_science_route_identity
from orca_auto.flow.contracts.workflow import (
    is_orca_stage_kind,
    is_valid_interaction_stage_contract,
    workflow_request_parameters,
)
from orca_auto.flow.orca_stage_validation import validate_workflow_orca_route
from orca_auto.flow.orchestration.charge_spin import manifest_with_charge_spin, strict_int
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
from ..manifest import require_crest_candidate_count as _require_crest_candidate_count
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


def _strict_optional_int(
    value: Any,
    *,
    field_name: str,
    minimum: int,
) -> int | None:
    if value in (None, ""):
        return None
    return strict_int(value, field=field_name, minimum=minimum)


def _resolved_resource_request(manifest: dict[str, Any]) -> dict[str, int]:
    resources = _manifest_mapping(manifest.get("resources"))
    resolved: dict[str, int] = {}
    max_cores = _strict_optional_int(
        resources.get("max_cores")
        if resources.get("max_cores") not in (None, "")
        else manifest.get("max_cores"),
        field_name="resources.max_cores",
        minimum=1,
    )
    max_memory_gb = _strict_optional_int(
        resources.get("max_memory_gb")
        if resources.get("max_memory_gb") not in (None, "")
        else manifest.get("max_memory_gb"),
        field_name="resources.max_memory_gb",
        minimum=1,
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
        parsed = _strict_optional_int(
            manifest.get(key),
            field_name=key,
            minimum=0 if key == "max_xtb_handoff_retries" else 1,
        )
        if parsed is not None:
            params[key] = (
                _require_crest_candidate_count(parsed) if key == "max_crest_candidates" else parsed
            )


def _manifest_electronic_state(manifest: dict[str, Any]) -> tuple[int | None, int | None]:
    """(charge, multiplicity) the flow manifest states, ``None`` when absent."""
    orca_manifest = _manifest_mapping(manifest.get("orca"))
    raw_charge = manifest.get("charge") if "charge" in manifest else orca_manifest.get("charge")
    charge = None if raw_charge in (None, "") else strict_int(raw_charge, field="workflow charge")
    raw_multiplicity = (
        manifest.get("multiplicity")
        if "multiplicity" in manifest
        else orca_manifest.get("multiplicity")
    )
    multiplicity = (
        None
        if raw_multiplicity in (None, "")
        else strict_int(raw_multiplicity, field="workflow multiplicity", minimum=1)
    )
    return charge, multiplicity


def _apply_orca_request_parameters(
    params: dict[str, Any],
    *,
    route_line: str,
    optts_route_line: str,
    charge: int | None,
    multiplicity: int | None,
) -> None:
    if route_line:
        params["orca_route_line"] = route_line
    if optts_route_line:
        params["orca_optts_route_line"] = optts_route_line
    if charge is not None:
        params["charge"] = charge
    if multiplicity is not None:
        params["multiplicity"] = multiplicity


def _manifest_orca_route_line(manifest: dict[str, Any]) -> str:
    orca_manifest = _manifest_mapping(manifest.get("orca"))
    candidates = (
        ("orca_route_line" in manifest, manifest.get("orca_route_line"), "orca_route_line"),
        ("route_line" in orca_manifest, orca_manifest.get("route_line"), "orca.route_line"),
    )
    for present, value, label in candidates:
        if not present:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a string. got={value!r}")
        if value.strip():
            return value.strip()
    return ""


def _manifest_orca_optts_route_line(manifest: dict[str, Any]) -> str:
    if "orca_optts_route_line" not in manifest:
        return ""
    value = manifest.get("orca_optts_route_line")
    if not isinstance(value, str):
        raise ValueError(f"orca_optts_route_line must be a string. got={value!r}")
    return value.strip()


def _validate_manifest_orca_routes(
    template_name: str,
    manifest: dict[str, Any],
) -> tuple[str, str]:
    route_line = _manifest_orca_route_line(manifest)
    task_kind = {
        "reaction_ts_search": "optts_freq",
        "conformer_screening": "opt",
        "scan_ts_search": "relaxed_scan",
    }.get(template_name, "")
    if route_line and task_kind:
        route_line = validate_workflow_orca_route(task_kind=task_kind, route_line=route_line)
    optts_route_line = ""
    if template_name == "scan_ts_search":
        optts_route_line = _manifest_orca_optts_route_line(manifest)
        if optts_route_line:
            optts_route_line = validate_workflow_orca_route(
                task_kind="optts_freq",
                route_line=optts_route_line,
            )
    return route_line, optts_route_line


def _durable_interaction_config_fingerprint(payload: dict[str, Any]) -> str:
    params = _request_parameters(payload)
    try:
        interaction_cfg = _normalize_interaction_energy_block(params.get("interaction_energy"))
        if interaction_cfg is None:
            return ""
        rmsd_cfg = _normalize_rmsd_dedup_block(params.get("rmsd_dedup"))
        charge = strict_int(params.get("charge", 0), field="charge")
        multiplicity = strict_int(
            params.get("multiplicity", 1),
            field="multiplicity",
            minimum=1,
        )
    except ValueError:
        return ""
    return _interaction_energy_config_fingerprint(
        interaction_cfg,
        complex_charge=charge,
        complex_multiplicity=multiplicity,
        rmsd_dedup=rmsd_cfg,
    )


_ELECTRONIC_STATE_ENGINES = ("crest", "xtb")


def _stage_manifest_overrides(raw_stage: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """The job manifest overrides a stage ran with (payload first, then metadata)."""
    for container in (task.get("payload"), task.get("metadata"), raw_stage.get("metadata")):
        if isinstance(container, dict):
            overrides = container.get("job_manifest_overrides")
            if isinstance(overrides, dict):
                return overrides
    return {}


def _completed_engine_stages_on_other_state(
    payload: dict[str, Any],
    *,
    charge: int | None,
    multiplicity: int | None,
) -> list[str]:
    """Completed CREST/xTB stages that ran on another electronic state.

    The state a stage actually screened on is the ``charge``/``uhf`` pair in
    its job manifest overrides (absent keys mean the neutral singlet), not the
    request parameters, which an older or hand-edited workflow.json may lack.
    """
    if charge is None and multiplicity is None:
        return []
    mismatched: list[str] = []
    for raw_stage in payload.get("stages", []):
        if not isinstance(raw_stage, dict):
            continue
        task = raw_stage.get("task")
        task = task if isinstance(task, dict) else {}
        if _normalize_text(task.get("engine")).lower() not in _ELECTRONIC_STATE_ENGINES:
            continue
        if "completed" not in {
            _normalize_text(raw_stage.get("status")).lower(),
            _normalize_text(task.get("status")).lower(),
        }:
            continue
        stage_id = _normalize_text(raw_stage.get("stage_id")) or "<unknown>"
        overrides = _stage_manifest_overrides(raw_stage, task)
        try:
            stage_charge = strict_int(overrides.get("charge", 0) or 0, field="stage charge")
            stage_uhf = strict_int(overrides.get("uhf", 0) or 0, field="stage uhf", minimum=0)
        except ValueError:
            mismatched.append(f"{stage_id}(state unreadable)")
            continue
        if charge is not None and stage_charge != charge:
            mismatched.append(f"{stage_id}(charge={stage_charge})")
        elif multiplicity is not None and stage_uhf != multiplicity - 1:
            mismatched.append(f"{stage_id}(multiplicity={stage_uhf + 1})")
    return mismatched


def _recorded_electronic_state(payload: dict[str, Any]) -> dict[str, int | None]:
    """The charge/multiplicity the request parameters record; None when absent."""
    params = workflow_request_parameters(payload)
    recorded: dict[str, int | None] = {}
    for key, minimum in (("charge", None), ("multiplicity", 1)):
        recorded[key] = (
            strict_int(params[key], field=key, minimum=minimum) if key in params else None
        )
    return recorded


def _completed_primary_orca_stage_ids(
    payload: dict[str, Any],
    *,
    expected_interaction_fingerprint: str,
) -> list[str]:
    completed: list[str] = []
    stages = [stage for stage in payload.get("stages", []) if isinstance(stage, dict)]
    for raw_stage in stages:
        if not isinstance(raw_stage, dict):
            continue
        if is_valid_interaction_stage_contract(
            raw_stage,
            stages,
            expected_config_fingerprint=expected_interaction_fingerprint,
        ):
            continue
        task = raw_stage.get("task")
        task = task if isinstance(task, dict) else {}
        if not (
            is_orca_stage_kind(raw_stage) or _normalize_text(task.get("engine")).lower() == "orca"
        ):
            continue
        if "completed" not in {
            _normalize_text(raw_stage.get("status")).lower(),
            _normalize_text(task.get("status")).lower(),
        }:
            continue
        completed.append(_normalize_text(raw_stage.get("stage_id")) or "<unknown>")
    return completed


def _changed_completed_orca_science_fields(
    payload: dict[str, Any],
    *,
    template_name: str,
    route_line: str,
    optts_route_line: str,
    manifest_charge: int | None,
    manifest_multiplicity: int | None,
) -> list[str]:
    params = workflow_request_parameters(payload)
    changed: list[str] = []
    route_updates = [("orca_route_line", route_line)]
    if template_name == "scan_ts_search":
        route_updates.append(("orca_optts_route_line", optts_route_line))
    for key, updated in route_updates:
        if not updated:
            continue
        current = params.get(key)
        if not isinstance(current, str) or not current.strip():
            changed.append(key)
            continue
        task_kind = (
            "optts_freq"
            if key == "orca_optts_route_line" or template_name == "reaction_ts_search"
            else "opt"
            if template_name == "conformer_screening"
            else "relaxed_scan"
        )
        try:
            current = validate_workflow_orca_route(task_kind=task_kind, route_line=current)
        except ValueError:
            changed.append(key)
            continue
        if orca_science_route_identity(current.splitlines()) != orca_science_route_identity(
            updated.splitlines()
        ):
            changed.append(key)
    for numeric_key, numeric_updated, minimum in (
        ("charge", manifest_charge, None),
        ("multiplicity", manifest_multiplicity, 1),
    ):
        if numeric_updated is None:
            continue
        try:
            current_value = strict_int(
                params.get(numeric_key),
                field=numeric_key,
                minimum=minimum,
            )
        except ValueError:
            changed.append(numeric_key)
            continue
        if current_value != numeric_updated:
            changed.append(numeric_key)
    return changed


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
    route_line: str,
    optts_route_line: str,
    manifest_charge: int | None,
    manifest_multiplicity: int | None,
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
    _apply_orca_request_parameters(
        params,
        route_line=route_line,
        optts_route_line=optts_route_line,
        charge=manifest_charge,
        multiplicity=manifest_multiplicity,
    )
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
    # this restart manifest.
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
        return {
            "applied": False,
            "persisted_interaction_energy_fingerprint": (
                _durable_interaction_config_fingerprint(payload)
            ),
        }
    return _flow_restart_settings_from_manifest(workspace, payload, manifest)


def _reject_science_changes_on_completed_stages(
    payload: dict[str, Any],
    *,
    template_name: str,
    route_line: str,
    optts_route_line: str,
    manifest_charge: int | None,
    manifest_multiplicity: int | None,
    persisted_interaction_fingerprint: str,
) -> dict[str, int | None]:
    """Refuse a manifest that changes the science behind completed stages.

    Returns the electronic state the payload recorded. It is read only after
    the ORCA science check so a corrupt recorded charge cannot pre-empt the
    science-change refusal.
    """
    completed_primary_orca = _completed_primary_orca_stage_ids(
        payload,
        expected_interaction_fingerprint=persisted_interaction_fingerprint,
    )
    changed_science = _changed_completed_orca_science_fields(
        payload,
        template_name=template_name,
        route_line=route_line,
        optts_route_line=optts_route_line,
        manifest_charge=manifest_charge,
        manifest_multiplicity=manifest_multiplicity,
    )
    if completed_primary_orca and changed_science:
        raise ValueError(
            "workflow ORCA scientific settings cannot change while completed primary "
            "ORCA stages retain prior results; start a new workflow or restore the "
            f"original settings. fields={changed_science!r}, "
            f"completed_stages={completed_primary_orca!r}"
        )
    # A completed CREST/xTB stage screened conformers on the electronic state
    # its job manifest carried; restarting only the ORCA stages on another
    # charge or multiplicity would feed conformers from one potential-energy
    # surface into ORCA on another, and nothing downstream could tell. Refuse,
    # as for completed ORCA stages. The comparison uses the stage's own
    # manifest state, so a manifest that restates the state an older payload
    # never recorded is accepted.
    previous_electronic_state = _recorded_electronic_state(payload)
    if manifest_charge is not None or manifest_multiplicity is not None:
        # Compare the pair the restart will actually run on: a stated field,
        # else the recorded one, else the neutral-singlet default the request
        # parameters fall back to. A manifest stating only one field must not
        # slip the other past a completed stage.
        effective_charge = (
            manifest_charge
            if manifest_charge is not None
            else previous_electronic_state["charge"]
            if previous_electronic_state["charge"] is not None
            else 0
        )
        effective_multiplicity = (
            manifest_multiplicity
            if manifest_multiplicity is not None
            else previous_electronic_state["multiplicity"]
            if previous_electronic_state["multiplicity"] is not None
            else 1
        )
    else:
        effective_charge = None
        effective_multiplicity = None
    mismatched_engine_stages = _completed_engine_stages_on_other_state(
        payload,
        charge=effective_charge,
        multiplicity=effective_multiplicity,
    )
    if mismatched_engine_stages:
        raise ValueError(
            "workflow electronic state cannot change while completed CREST/xTB stages "
            "retain prior results; start a new workflow or restore the original "
            f"charge/multiplicity. requested=(charge={effective_charge!r}, "
            f"multiplicity={effective_multiplicity!r}), "
            f"completed_stages={mismatched_engine_stages!r}"
        )
    return previous_electronic_state


def _electronic_state_change(
    previous_electronic_state: dict[str, int | None],
    *,
    manifest_charge: int | None,
    manifest_multiplicity: int | None,
    charge: Any,
    multiplicity: Any,
) -> dict[str, Any] | None:
    """Describe the manifest's charge/multiplicity change, or ``None`` when unchanged."""
    changed_electronic_fields = [
        key
        for key, stated in (("charge", manifest_charge), ("multiplicity", manifest_multiplicity))
        if stated is not None and previous_electronic_state[key] != stated
    ]
    if not changed_electronic_fields:
        return None
    return {
        # A previous value is None when the workflow never recorded it.
        "previous": dict(previous_electronic_state),
        "current": {
            "charge": strict_int(charge, field="charge"),
            "multiplicity": strict_int(multiplicity, field="multiplicity", minimum=1),
        },
        "fields": changed_electronic_fields,
    }


def _validate_interaction_energy_restart(
    workspace: Path,
    payload: dict[str, Any],
    *,
    interaction_cfg: dict[str, Any],
    charge: int,
    multiplicity: int,
) -> None:
    """Check the durable interaction-energy block against input.xyz and the complex state."""
    _validate_interaction_energy_state_balance(
        interaction_cfg,
        complex_charge=charge,
        complex_multiplicity=multiplicity,
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
            f"interaction_energy restart fragments do not partition input.xyz: {partition_reason}"
        )
    state_reason = validate_fragment_electronic_states(atom_symbols, fragments)
    if state_reason:
        raise ValueError(f"interaction_energy restart fragment state is impossible: {state_reason}")


def _reject_interaction_fingerprint_change(
    payload: dict[str, Any],
    *,
    persisted_interaction_fingerprint: str,
    interaction_fingerprint: str,
) -> None:
    """Refuse new interaction-energy science once fan-out stages were materialized."""
    workflow_stages = [
        raw_stage for raw_stage in payload.get("stages", []) if isinstance(raw_stage, dict)
    ]
    for raw_stage in workflow_stages:
        if not is_valid_interaction_stage_contract(
            raw_stage,
            workflow_stages,
            expected_config_fingerprint=persisted_interaction_fingerprint,
        ):
            continue
        if persisted_interaction_fingerprint != interaction_fingerprint:
            raise ValueError(
                "interaction_energy scientific settings cannot change after its stages "
                "were materialized; disable the feature or start a new workflow"
            )


def _optts_route_for_template(
    template_name: str,
    *,
    route_line: str,
    manifest_optts_route_line: str,
    params: dict[str, Any],
) -> tuple[str, bool]:
    """Resolve the OptTS route line and whether the manifest stated it."""
    if template_name == "reaction_ts_search":
        return route_line or _normalize_text(params.get("orca_route_line")), bool(route_line)
    if template_name == "scan_ts_search":
        return (
            manifest_optts_route_line or _normalize_text(params.get("orca_optts_route_line")),
            bool(manifest_optts_route_line),
        )
    return "", False


def _flow_restart_settings_from_manifest(
    workspace: Path,
    payload: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    template_name = _workflow_template_name(payload, manifest)
    route_line, manifest_optts_route_line = _validate_manifest_orca_routes(
        template_name,
        manifest,
    )
    manifest_charge, manifest_multiplicity = _manifest_electronic_state(manifest)
    persisted_interaction_fingerprint = _durable_interaction_config_fingerprint(payload)
    previous_electronic_state = _reject_science_changes_on_completed_stages(
        payload,
        template_name=template_name,
        route_line=route_line,
        optts_route_line=manifest_optts_route_line,
        manifest_charge=manifest_charge,
        manifest_multiplicity=manifest_multiplicity,
        persisted_interaction_fingerprint=persisted_interaction_fingerprint,
    )
    crest_present, crest_manifest = _resolve_engine_manifest(workspace, manifest, "crest")
    xtb_present, xtb_manifest = _resolve_engine_manifest(workspace, manifest, "xtb")
    endpoint_pairing = _resolve_endpoint_pairing_manifest(manifest, xtb_manifest)
    crest_overrides = _crest_manifest_with_defaults(
        template_name=template_name,
        crest_manifest=crest_manifest,
    )
    priority = (
        normalize_queue_priority(manifest.get("priority")) if "priority" in manifest else None
    )
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
        route_line=route_line,
        optts_route_line=manifest_optts_route_line,
        manifest_charge=manifest_charge,
        manifest_multiplicity=manifest_multiplicity,
    )
    # The request parameters now hold the effective charge/multiplicity
    # (manifest-updated or pre-existing). Per-stage restart application replaces
    # whatever overrides a stage carried, so they
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
    charge = params.get("charge", manifest_charge if manifest_charge is not None else 0)
    multiplicity = params.get(
        "multiplicity", manifest_multiplicity if manifest_multiplicity is not None else 1
    )
    electronic_state_change = _electronic_state_change(
        previous_electronic_state,
        manifest_charge=manifest_charge,
        manifest_multiplicity=manifest_multiplicity,
        charge=charge,
        multiplicity=multiplicity,
    )
    interaction_cfg = params.get("interaction_energy")
    interaction_cfg = interaction_cfg if isinstance(interaction_cfg, dict) else None
    rmsd_cfg = params.get("rmsd_dedup")
    rmsd_cfg = rmsd_cfg if isinstance(rmsd_cfg, dict) else None
    if interaction_cfg is not None:
        _validate_interaction_energy_restart(
            workspace,
            payload,
            interaction_cfg=interaction_cfg,
            charge=int(charge),
            multiplicity=int(multiplicity),
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
        _reject_interaction_fingerprint_change(
            payload,
            persisted_interaction_fingerprint=persisted_interaction_fingerprint,
            interaction_fingerprint=interaction_fingerprint,
        )
    optts_route_line, optts_route_line_present = _optts_route_for_template(
        template_name,
        route_line=route_line,
        manifest_optts_route_line=manifest_optts_route_line,
        params=params,
    )
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
        # Recorded in the restart summary, journal and response when the
        # manifest states a charge/multiplicity the request parameters did
        # not already record (previous is None when never recorded).
        "electronic_state_change": electronic_state_change,
        "charge": charge,
        "multiplicity": multiplicity,
        "interaction_energy": interaction_cfg,
        "interaction_energy_fingerprint": interaction_fingerprint,
        "persisted_interaction_energy_fingerprint": persisted_interaction_fingerprint,
        "interaction_energy_disabled": (
            "interaction_energy" in manifest and interaction_cfg is None
        ),
        "orca_charge": manifest_charge,
        "orca_multiplicity": manifest_multiplicity,
        "orca_route_line_present": bool(route_line),
        "orca_route_line": route_line or _normalize_text(params.get("orca_route_line")),
        "orca_optts_route_line_present": optts_route_line_present,
        "orca_optts_route_line": optts_route_line,
        "orca_input_updates": bool(route_line)
        or bool(manifest_optts_route_line)
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
        "max_xtb_handoff_retries": (
            params.get("max_xtb_handoff_retries") if "max_xtb_handoff_retries" in manifest else None
        ),
        "endpoint_pairing": endpoint_pairing,
    }
