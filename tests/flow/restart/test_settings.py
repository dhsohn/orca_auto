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
