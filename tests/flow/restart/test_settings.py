from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orca_auto.flow.manifest import interaction_energy_config_fingerprint
from orca_auto.flow.restart import restart_failed_workflow
from orca_auto.flow.restart import settings as restart_settings
from tests.flow.restart_helpers import _write_workflow


@pytest.mark.parametrize(
    "manifest",
    [
        {"charge": -0.5, "multiplicity": 2},
        {"charge": 0, "multiplicity": 2.5},
        {"orca": {"charge": True, "multiplicity": 1}},
    ],
)
def test_restart_manifest_electronic_state_rejects_lossy_integer_values(
    manifest: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match=r"workflow (?:charge|multiplicity) must be an integer"):
        restart_settings._manifest_electronic_state(manifest)


@pytest.mark.parametrize(
    "manifest",
    [
        {"resources": {"max_cores": 1.5}},
        {"resources": {"max_memory_gb": True}},
        {"max_cores": 0},
    ],
)
def test_restart_manifest_rejects_lossy_resource_limits(
    manifest: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match=r"resources\.max_(?:cores|memory_gb) must"):
        restart_settings._resolved_resource_request(manifest)


@pytest.mark.parametrize(
    "manifest",
    [
        {"workflow_type": "reaction_ts_search", "max_crest_candidates": 2.5},
        {"workflow_type": "reaction_ts_search", "max_crest_candidates": 33},
        {"workflow_type": "reaction_ts_search", "max_xtb_handoff_retries": True},
        {"workflow_type": "reaction_ts_search", "max_orca_stages": 0},
    ],
)
def test_restart_manifest_rejects_lossy_workflow_caps(
    tmp_path: Path,
    manifest: dict[str, object],
) -> None:
    payload = {
        "template_name": "reaction_ts_search",
        "metadata": {"request": {"parameters": {"charge": 0, "multiplicity": 1}}},
        "stages": [],
    }

    with pytest.raises(ValueError, match=r"max_.* must"):
        restart_settings._flow_restart_settings_from_manifest(tmp_path, payload, manifest)


@pytest.mark.parametrize(
    ("template_name", "manifest"),
    [
        ("reaction_ts_search", {"orca": {"route_line": "! Opt r2scan-3c"}}),
        ("reaction_ts_search", {"orca": {"route_line": "! ScanTS Freq r2scan-3c"}}),
        ("reaction_ts_search", {"orca": {"route_line": "! NEB-TS Freq r2scan-3c"}}),
        ("conformer_screening", {"orca": {"route_line": "! SP r2scan-3c"}}),
        ("scan_ts_search", {"orca": {"route_line": "! SP r2scan-3c"}}),
        ("scan_ts_search", {"orca_optts_route_line": "! Opt r2scan-3c"}),
    ],
)
def test_restart_manifest_rejects_orca_route_role_mismatch(
    tmp_path: Path,
    template_name: str,
    manifest: dict[str, object],
) -> None:
    payload = {
        "template_name": template_name,
        "metadata": {"request": {"parameters": {"charge": 0, "multiplicity": 1}}},
        "stages": [],
    }
    manifest = {"workflow_type": template_name, **manifest}

    with pytest.raises(ValueError, match="route-role mismatch"):
        restart_settings._flow_restart_settings_from_manifest(tmp_path, payload, manifest)


@pytest.mark.parametrize("frequency_keyword", ("Freq", "NumFreq", "AnFreq"))
def test_restart_manifest_accepts_exact_optts_with_supported_frequency_keyword(
    tmp_path: Path,
    frequency_keyword: str,
) -> None:
    payload: dict[str, Any] = {
        "template_name": "reaction_ts_search",
        "metadata": {"request": {"parameters": {"charge": 0, "multiplicity": 1}}},
        "stages": [],
    }
    route_line = f"! OptTS {frequency_keyword} r2scan-3c"

    settings = restart_settings._flow_restart_settings_from_manifest(
        tmp_path,
        payload,
        {
            "workflow_type": "reaction_ts_search",
            "orca": {"route_line": route_line},
        },
    )

    assert settings["orca_route_line"] == route_line


@pytest.mark.parametrize(
    ("template_name", "manifest"),
    [
        ("reaction_ts_search", {"orca_route_line": ["! OptTS", "! Freq"]}),
        ("conformer_screening", {"orca": {"route_line": {"method": "HF"}}}),
        ("scan_ts_search", {"orca_optts_route_line": ["! OptTS Freq"]}),
    ],
)
def test_restart_manifest_rejects_structured_orca_route_fields(
    tmp_path: Path,
    template_name: str,
    manifest: dict[str, object],
) -> None:
    payload = {
        "template_name": template_name,
        "metadata": {"request": {"parameters": {"charge": 0, "multiplicity": 1}}},
        "stages": [],
    }

    with pytest.raises(ValueError, match="route_line must be a string"):
        restart_settings._flow_restart_settings_from_manifest(tmp_path, payload, manifest)


@pytest.mark.parametrize(
    ("template_name", "parameters", "manifest", "changed_field"),
    [
        (
            "reaction_ts_search",
            {
                "orca_route_line": "! OLD OptTS Freq",
                "charge": 0,
                "multiplicity": 1,
            },
            {"orca": {"route_line": "! NEW OptTS Freq"}},
            "orca_route_line",
        ),
        (
            "conformer_screening",
            {"orca_route_line": "! Opt HF", "charge": 0, "multiplicity": 1},
            {"charge": -1},
            "charge",
        ),
        (
            "scan_ts_search",
            {
                "orca_route_line": "! Opt HF",
                "orca_optts_route_line": "! OptTS Freq HF",
                "charge": 0,
                "multiplicity": 1,
            },
            {"multiplicity": 2},
            "multiplicity",
        ),
        (
            "scan_ts_search",
            {
                "orca_route_line": "! Opt HF",
                "orca_optts_route_line": "! OptTS Freq HF",
                "charge": 0,
                "multiplicity": 1,
            },
            {"orca_optts_route_line": "! OptTS Freq PBE0"},
            "orca_optts_route_line",
        ),
    ],
)
def test_restart_rejects_scientific_change_after_primary_orca_completion(
    tmp_path: Path,
    template_name: str,
    parameters: dict[str, object],
    manifest: dict[str, object],
    changed_field: str,
) -> None:
    payload: dict[str, Any] = {
        "template_name": template_name,
        "metadata": {"request": {"parameters": dict(parameters)}},
        "stages": [
            {
                "stage_id": "orca_completed_01",
                "stage_kind": "orca_stage",
                "status": "completed",
                "task": {"engine": "orca", "status": "completed"},
                "metadata": {},
            }
        ],
    }
    original = json.loads(json.dumps(payload))

    with pytest.raises(ValueError, match=rf"fields=.*{changed_field}"):
        restart_settings._flow_restart_settings_from_manifest(tmp_path, payload, manifest)

    assert payload == original


def test_restart_allows_unchanged_science_and_non_scientific_updates_after_completion(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "template_name": "conformer_screening",
        "metadata": {
            "request": {
                "parameters": {
                    "orca_route_line": "# note # ! Opt # hidden # HF",
                    "charge": "0",
                    "multiplicity": "1",
                }
            }
        },
        "stages": [
            {
                "stage_id": "orca_completed_01",
                "stage_kind": "orca_stage",
                "status": "completed",
                "task": {"engine": "orca", "status": "completed"},
                "metadata": {},
            }
        ],
    }
    manifest = {
        "orca": {"route_line": "! Opt HF", "charge": 0, "multiplicity": 1},
        "resources": {"max_cores": 12, "max_memory_gb": 48},
        "priority": 4,
    }

    settings = restart_settings._flow_restart_settings_from_manifest(
        tmp_path,
        payload,
        manifest,
    )

    parameters = payload["metadata"]["request"]["parameters"]
    assert settings["orca_route_line"] == "! Opt HF"
    assert parameters["orca_route_line"] == "! Opt HF"
    assert parameters["max_cores"] == 12
    assert parameters["max_memory_gb"] == 48
    assert parameters["priority"] == 4


def test_restart_treats_route_case_as_same_science_after_completion(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "template_name": "conformer_screening",
        "metadata": {
            "request": {
                "parameters": {
                    "orca_route_line": "! Opt HF",
                    "charge": 0,
                    "multiplicity": 1,
                }
            }
        },
        "stages": [
            {
                "stage_id": "orca_completed_case_only",
                "stage_kind": "orca_stage",
                "status": "completed",
                "task": {
                    "engine": "orca",
                    "task_kind": "opt",
                    "status": "completed",
                },
                "metadata": {},
            }
        ],
    }

    settings = restart_settings._flow_restart_settings_from_manifest(
        tmp_path,
        payload,
        {
            "orca": {"route_line": "! opt hf"},
            "resources": {"max_cores": 12},
        },
    )

    parameters = payload["metadata"]["request"]["parameters"]
    assert settings["orca_route_line"].lower() == "! opt hf"
    assert parameters["orca_route_line"].lower() == "! opt hf"
    assert parameters["max_cores"] == 12


def test_restart_allows_unquoted_pal_resource_change_after_orca_completion(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "template_name": "conformer_screening",
        "metadata": {
            "request": {
                "parameters": {
                    "orca_route_line": "! HF Opt PAL4",
                    "charge": 0,
                    "multiplicity": 1,
                }
            }
        },
        "stages": [
            {
                "stage_id": "orca_completed_pal_resource_change",
                "stage_kind": "orca_stage",
                "status": "completed",
                "task": {
                    "engine": "orca",
                    "task_kind": "opt",
                    "status": "completed",
                },
                "metadata": {},
            }
        ],
    }

    settings = restart_settings._flow_restart_settings_from_manifest(
        tmp_path,
        payload,
        {"orca": {"route_line": "! HF Opt PAL8"}},
    )

    assert settings["orca_route_line"] == "! HF Opt PAL8"
    parameters = payload["metadata"]["request"]["parameters"]
    assert parameters["orca_route_line"] == "! HF Opt PAL8"


def test_restart_does_not_let_primary_task_kind_spoof_interaction_role(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "template_name": "conformer_screening",
        "metadata": {
            "request": {
                "parameters": {
                    "orca_route_line": "! Opt HF",
                    "charge": 0,
                    "multiplicity": 1,
                }
            }
        },
        "stages": [
            {
                "stage_id": "orca_primary_with_spoofed_role",
                "stage_kind": "orca_stage",
                "status": "completed",
                "task": {
                    "engine": "orca",
                    "task_kind": "opt",
                    "status": "completed",
                },
                "metadata": {"role": "interaction_fragment"},
            }
        ],
    }
    original = json.loads(json.dumps(payload))

    with pytest.raises(ValueError, match="scientific settings cannot change"):
        restart_settings._flow_restart_settings_from_manifest(
            tmp_path,
            payload,
            {"orca": {"route_line": "! Opt PBE0"}},
        )

    assert payload == original


def test_wrong_interaction_fingerprint_cannot_bypass_completed_primary_science_guard(
    tmp_path: Path,
) -> None:
    interaction = {
        "enabled": True,
        "sp_route_line": "! HF TightSCF",
        "max_fragments": 2,
        "fragments": [
            {"atom_indices": [0], "charge": 0, "multiplicity": 1, "label": "a"},
            {"atom_indices": [1], "charge": 0, "multiplicity": 1, "label": "b"},
        ],
    }
    payload: dict[str, Any] = {
        "template_name": "conformer_screening",
        "metadata": {
            "request": {
                "parameters": {
                    "orca_route_line": "! Opt HF",
                    "charge": 0,
                    "multiplicity": 1,
                    "interaction_energy": interaction,
                }
            }
        },
        "stages": [
            {
                "stage_id": "orca_parent",
                "stage_kind": "orca_stage",
                "status": "planned",
                "task": {"engine": "orca", "task_kind": "opt", "status": "planned"},
                "metadata": {},
            },
            {
                "stage_id": "completed_primary_sp",
                "stage_kind": "orca_stage",
                "status": "completed",
                "task": {"engine": "orca", "task_kind": "sp", "status": "completed"},
                "metadata": {
                    "role": "interaction_complex_sp",
                    "parent_stage_id": "orca_parent",
                    "interaction_config_fingerprint": "b" * 64,
                },
            },
        ],
    }

    with pytest.raises(ValueError, match="scientific settings cannot change"):
        restart_settings._flow_restart_settings_from_manifest(
            tmp_path,
            payload,
            {"orca": {"route_line": "! Opt PBE0"}},
        )


@pytest.mark.parametrize("template_name", ["reaction_ts_search", "scan_ts_search"])
@pytest.mark.parametrize("feature", ["interaction_energy", "rmsd_dedup"])
def test_restart_rejects_conformer_postprocessing_on_unsupported_template(
    tmp_path: Path,
    template_name: str,
    feature: str,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / f"wf_{template_name}_{feature}"
    workspace.mkdir(parents=True)
    if feature == "interaction_energy":
        feature_yaml = """
interaction_energy:
  enabled: true
  fragments:
    - atom_indices: [0]
      charge: 0
      multiplicity: 2
      label: atom_a
    - atom_indices: [1]
      charge: 0
      multiplicity: 2
      label: atom_b
"""
    else:
        feature_yaml = """
rmsd_dedup:
  enabled: true
"""
    (workspace / "flow.yaml").write_text(
        f"workflow_type: {template_name}\n{feature_yaml.lstrip()}", encoding="utf-8"
    )
    original: dict[str, object] = {
        "workflow_id": workspace.name,
        "template_name": template_name,
        "status": "failed",
        "stages": [],
        "metadata": {"request": {"parameters": {"charge": 0, "multiplicity": 1}}},
    }
    _write_workflow(workspace, original)

    with pytest.raises(ValueError, match="supported only for conformer_screening"):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    assert json.loads((workspace / "workflow.json").read_text(encoding="utf-8")) == original


def test_restart_rejects_rmsd_grouping_change_after_interaction_fanout(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_ie_rmsd_change"
    workspace.mkdir(parents=True)
    (workspace / "input.xyz").write_text("2\ncomplex\nC 0 0 0\nO 1.2 0 0\n", encoding="utf-8")
    interaction = {
        "enabled": True,
        "sp_route_line": "! HF TightSCF",
        "max_fragments": 2,
        "fragments": [
            {"atom_indices": [0], "charge": 0, "multiplicity": 1, "label": "a"},
            {"atom_indices": [1], "charge": 0, "multiplicity": 1, "label": "b"},
        ],
    }
    old_rmsd = {
        "enabled": True,
        "rmsd_threshold_angstrom": 0.1,
        "energy_window_kcal": 0.01,
        "heavy_atoms_only": False,
    }
    fingerprint = interaction_energy_config_fingerprint(
        interaction,
        complex_charge=0,
        complex_multiplicity=1,
        rmsd_dedup=old_rmsd,
    )
    (workspace / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: conformer_screening",
                "rmsd_dedup:",
                "  enabled: true",
                "  rmsd_threshold_angstrom: 0.25",
                "  energy_window_kcal: 1.0",
                "  heavy_atoms_only: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    stage: dict[str, object] = {
        "stage_id": "ie_failed",
        "stage_kind": "orca_stage",
        "status": "failed",
        "task": {"engine": "orca", "task_kind": "sp", "status": "failed", "payload": {}},
        "metadata": {
            "role": "interaction_fragment",
            "parent_stage_id": "orca_parent",
            "fragment_index": 0,
            "interaction_config_fingerprint": fingerprint,
        },
    }
    parent_stage = {
        "stage_id": "orca_parent",
        "stage_kind": "orca_stage",
        "status": "completed",
        "task": {"engine": "orca", "task_kind": "opt", "status": "completed"},
        "metadata": {},
    }
    payload: dict[str, object] = {
        "workflow_id": "wf_ie_rmsd_change",
        "template_name": "conformer_screening",
        "status": "failed",
        "stages": [parent_stage, stage],
        "metadata": {
            "request": {
                "parameters": {
                    "charge": 0,
                    "multiplicity": 1,
                    "interaction_energy": interaction,
                    "rmsd_dedup": old_rmsd,
                }
            }
        },
    }
    _write_workflow(workspace, payload)

    with pytest.raises(ValueError, match="scientific settings cannot change"):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)
    assert json.loads((workspace / "workflow.json").read_text(encoding="utf-8")) == payload


def test_restart_failed_workflow_rejects_non_mapping_flow_yaml(tmp_path: Path) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_bad_manifest"
    workspace.mkdir(parents=True)
    (workspace / "flow.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_bad_manifest",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "xtb_failed",
                    "status": "failed",
                    "task": {
                        "engine": "xtb",
                        "status": "failed",
                        "payload": {},
                        "enqueue_payload": {},
                    },
                    "metadata": {},
                }
            ],
            "metadata": {},
        },
    )

    with pytest.raises(ValueError, match="Workflow manifest must contain a mapping"):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)


def _electronic_state_payload(*, crest_status: str) -> dict[str, Any]:
    return {
        "template_name": "conformer_screening",
        "metadata": {
            "request": {
                "parameters": {
                    "orca_route_line": "! Opt HF",
                    "charge": 0,
                    "multiplicity": 1,
                }
            }
        },
        "stages": [
            {
                "stage_id": "crest_reactant_01",
                "status": crest_status,
                "task": {"engine": "crest", "status": crest_status},
                "metadata": {},
            },
            {
                "stage_id": "orca_candidate_01",
                "stage_kind": "orca_stage",
                "status": "failed",
                "task": {"engine": "orca", "task_kind": "opt", "status": "failed"},
                "metadata": {},
            },
        ],
    }


def test_restart_refuses_electronic_state_change_over_completed_crest_stage(
    tmp_path: Path,
) -> None:
    # The completed CREST stage screened conformers as a neutral singlet; only
    # the failed ORCA stage would be re-run, on a different surface, with no
    # record of the mismatch. Refuse, as for completed primary ORCA stages.
    payload = _electronic_state_payload(crest_status="completed")
    original = json.loads(json.dumps(payload))

    with pytest.raises(ValueError, match="electronic state cannot change") as excinfo:
        restart_settings._flow_restart_settings_from_manifest(
            tmp_path,
            payload,
            {"orca": {"charge": 1, "multiplicity": 2}},
        )

    assert "crest_reactant_01" in str(excinfo.value)
    assert payload == original


def test_restart_records_electronic_state_change_without_completed_engine_stages(
    tmp_path: Path,
) -> None:
    payload = _electronic_state_payload(crest_status="failed")

    settings = restart_settings._flow_restart_settings_from_manifest(
        tmp_path,
        payload,
        {"orca": {"charge": 1}},
    )

    assert settings["electronic_state_change"] == {
        "previous": {"charge": 0, "multiplicity": 1},
        "current": {"charge": 1, "multiplicity": 1},
        "fields": ["charge"],
    }
    assert settings["charge"] == 1 and settings["multiplicity"] == 1
    assert payload["metadata"]["request"]["parameters"]["charge"] == 1

    unchanged = restart_settings._flow_restart_settings_from_manifest(
        tmp_path,
        _electronic_state_payload(crest_status="failed"),
        {"orca": {"charge": 0, "multiplicity": 1}},
    )
    assert unchanged["electronic_state_change"] is None


def test_restart_summary_and_journal_record_electronic_state_change(tmp_path: Path) -> None:
    from orca_auto.flow.restart import mutation as restart_mutation

    change = {
        "previous": {"charge": 0, "multiplicity": 1},
        "current": {"charge": 1, "multiplicity": 2},
        "fields": ["charge", "multiplicity"],
    }
    payload: dict[str, Any] = {"metadata": {}, "stages": []}
    restart_mutation._apply_restart_summary(
        payload,
        workspace=tmp_path / "wf",
        previous_status="failed",
        restarted_at="2026-09-03T00:00:00+00:00",
        restarted_stages=[],
        flow_settings={"applied": True, "electronic_state_change": change},
    )
    assert payload["metadata"]["restart_summary"]["electronic_state_change"] == change

    mutation = restart_mutation.WorkflowRestartMutation(
        root=tmp_path,
        workspace=tmp_path / "wf",
        payload=payload,
        previous_status="failed",
        restarted_at="2026-09-03T00:00:00+00:00",
        restarted_stages=[],
        flow_manifest_applied=True,
        summary={},
        electronic_state_change=change,
    )
    assert mutation.journal_metadata()["electronic_state_change"] == change
    assert mutation.response_payload()["electronic_state_change"] == change

    plain = restart_mutation.WorkflowRestartMutation(
        root=tmp_path,
        workspace=tmp_path / "wf",
        payload=payload,
        previous_status="failed",
        restarted_at="2026-09-03T00:00:00+00:00",
        restarted_stages=[],
        flow_manifest_applied=True,
        summary={},
    )
    assert "electronic_state_change" not in plain.journal_metadata()
    assert "electronic_state_change" not in plain.response_payload()


def _legacy_payload_without_parameters(
    *, crest_status: str, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    task: dict[str, Any] = {"engine": "crest", "status": crest_status, "payload": {}}
    if overrides is not None:
        task["payload"]["job_manifest_overrides"] = dict(overrides)
    return {
        "template_name": "reaction_ts_search",
        "metadata": {},
        "stages": [
            {"stage_id": "crest_reactant_01", "status": crest_status, "task": task, "metadata": {}},
            {
                "stage_id": "orca_candidate_01",
                "stage_kind": "orca_stage",
                "status": "failed",
                "task": {"engine": "orca", "task_kind": "optts_freq", "status": "failed"},
                "metadata": {},
            },
        ],
    }


def test_restart_accepts_a_manifest_that_restates_the_state_an_old_payload_never_recorded(
    tmp_path: Path,
) -> None:
    # No metadata.request.parameters (pre-May workflow.json): the completed
    # CREST stage ran as a neutral singlet (no charge/uhf overrides), and the
    # manifest states exactly that. Nothing changed for the conformers.
    payload = _legacy_payload_without_parameters(crest_status="completed")

    settings = restart_settings._flow_restart_settings_from_manifest(
        tmp_path,
        payload,
        {"charge": 0, "orca": {"multiplicity": 1}},
    )

    assert settings["electronic_state_change"] == {
        "previous": {"charge": None, "multiplicity": None},
        "current": {"charge": 0, "multiplicity": 1},
        "fields": ["charge", "multiplicity"],
    }


def test_restart_refuses_a_new_state_over_a_completed_stage_of_an_old_payload(
    tmp_path: Path,
) -> None:
    payload = _legacy_payload_without_parameters(crest_status="completed")

    with pytest.raises(ValueError, match="electronic state cannot change") as excinfo:
        restart_settings._flow_restart_settings_from_manifest(
            tmp_path,
            payload,
            {"charge": -1, "orca": {"multiplicity": 2}},
        )

    assert "crest_reactant_01(charge=0)" in str(excinfo.value)


def test_restart_refuses_a_multiplicity_only_change_over_a_completed_stage(
    tmp_path: Path,
) -> None:
    payload = _electronic_state_payload(crest_status="completed")

    with pytest.raises(ValueError, match="electronic state cannot change") as excinfo:
        restart_settings._flow_restart_settings_from_manifest(
            tmp_path,
            payload,
            {"orca": {"multiplicity": 3}},
        )

    assert "crest_reactant_01(multiplicity=1)" in str(excinfo.value)
    assert "requested=(charge=0, multiplicity=3)" in str(excinfo.value)


def test_restart_accepts_the_state_a_completed_stage_actually_ran_on(tmp_path: Path) -> None:
    # The stage's own manifest carried charge -1 / uhf 1 while the request
    # parameters were never recorded: the manifest agrees with the stage.
    payload = _legacy_payload_without_parameters(
        crest_status="completed",
        overrides={"charge": -1, "uhf": 1, "gfn": 1},
    )

    settings = restart_settings._flow_restart_settings_from_manifest(
        tmp_path,
        payload,
        {"charge": -1, "orca": {"multiplicity": 2}},
    )

    assert settings["electronic_state_change"]["current"] == {"charge": -1, "multiplicity": 2}


def test_restart_end_to_end_records_the_electronic_state_change(tmp_path: Path) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_state_change"
    (workspace / "old_xtb").mkdir(parents=True)
    (workspace / "flow.yaml").write_text(
        "workflow_type: reaction_ts_search\ncharge: -1\norca:\n  multiplicity: 2\n",
        encoding="utf-8",
    )
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_state_change",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "xtb_path_01",
                    "status": "failed",
                    "task": {
                        "engine": "xtb",
                        "status": "failed",
                        "payload": {
                            "job_dir": str(workspace / "old_xtb"),
                            "job_manifest_overrides": {"gfn": 1},
                        },
                        "metadata": {"job_manifest_overrides": {"gfn": 1}},
                        "enqueue_payload": {"job_dir": str(workspace / "old_xtb")},
                    },
                    "metadata": {"job_manifest_overrides": {"gfn": 1}},
                },
            ],
            "metadata": {},
        },
    )

    result = restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    expected = {
        "previous": {"charge": None, "multiplicity": None},
        "current": {"charge": -1, "multiplicity": 2},
        "fields": ["charge", "multiplicity"],
    }
    assert result["electronic_state_change"] == expected
    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    assert saved["metadata"]["restart_summary"]["electronic_state_change"] == expected


def test_restart_refuses_a_partial_manifest_whose_effective_state_differs(
    tmp_path: Path,
) -> None:
    # Old payload without recorded parameters; the completed CREST stage ran
    # at charge -1. A manifest stating only multiplicity 2 would restart the
    # remaining stages at the neutral default charge 0: refuse on the pair the
    # restart would actually run on, not only on the stated field.
    payload = _legacy_payload_without_parameters(
        crest_status="completed",
        overrides={"charge": -1, "uhf": 0},
    )

    with pytest.raises(ValueError, match="electronic state cannot change") as excinfo:
        restart_settings._flow_restart_settings_from_manifest(
            tmp_path,
            payload,
            {"orca": {"multiplicity": 2}},
        )

    assert "requested=(charge=0, multiplicity=2)" in str(excinfo.value)
    assert "crest_reactant_01(charge=-1)" in str(excinfo.value)


def test_restart_record_rejects_a_corrupt_recorded_charge_with_a_labelled_error(
    tmp_path: Path,
) -> None:
    payload = _electronic_state_payload(crest_status="failed")
    payload["metadata"]["request"]["parameters"]["charge"] = "x"

    with pytest.raises(ValueError, match="charge must be an integer"):
        restart_settings._flow_restart_settings_from_manifest(
            tmp_path,
            payload,
            {"orca": {"multiplicity": 2}},
        )
