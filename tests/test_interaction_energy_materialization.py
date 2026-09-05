"""Interaction-energy single-point fan-out (advance-phase materialization) tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.artifacts import RUN_REPORT_JSON_FILE
from orca_auto.core.engine_runner import confined_output_identity
from orca_auto.core.machine_observation import machine_json_bytes
from orca_auto.flow.contracts.workflow import is_valid_interaction_stage_contract
from orca_auto.flow.manifest import interaction_energy_config_fingerprint
from orca_auto.flow.orchestration.interaction_energy_materialization import (
    append_interaction_energy_stages_impl,
)
from orca_auto.orca.state import _machine_observation
from tests.engine_artifact_helpers import bind_report_generation

_OPT_ROUTE = "B3LYP def2-SVP Opt"
_SP_ROUTE = "wB97M-V def2-TZVPP"
_COORDS_C = (("C", 0.0, 0.0, 0.0), ("O", 1.100000, 0.0, 0.0))
_COORDS_D = (("C", 0.0, 0.0, 0.0), ("O", 1.150000, 0.0, 0.0))

_INTERACTION_CONFIG = {
    "enabled": True,
    "sp_route_line": "! r2scan-3c TightSCF",
    "max_fragments": 2,
    "fragments": [
        {"atom_indices": [0], "charge": 0, "multiplicity": 1, "label": "host"},
        {"atom_indices": [1], "charge": 0, "multiplicity": 1, "label": "guest"},
    ],
}


def _write_machine(generation: Path, state: dict[str, Any]) -> None:
    observation = _machine_observation(generation, state)
    (generation / RUN_REPORT_JSON_FILE).write_bytes(machine_json_bytes(observation))


def _out_text(
    energy: float,
    coords: tuple[tuple[str, float, float, float], ...],
    *,
    route: str = _OPT_ROUTE,
    converged: bool = True,
) -> str:
    lines = [
        "                                 Program Version 6.0.1 -  RELEASE  -",
        f"|  1> ! {route}",
        "|  2> * xyz 0 1",
        "|  3> C 0.0 0.0 0.0",
        "|  4> *",
        "",
        "CARTESIAN COORDINATES (ANGSTROEM)",
        "---------------------------------",
    ]
    lines += [f"  {el:<2}  {x:12.6f} {y:12.6f} {z:12.6f}" for el, x, y, z in coords]
    lines += [
        "",
        f"FINAL SINGLE POINT ENERGY     {energy:.12f}",
        *(["THE OPTIMIZATION HAS CONVERGED", ""] if converged else []),
        "                             ****ORCA TERMINATED NORMALLY****",
        "TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds 3 msec",
    ]
    return "\n".join(lines)


def _completed_opt_stage(
    root: Path,
    name: str,
    *,
    energy: float,
    coords: tuple[tuple[str, float, float, float], ...],
    route: str = _OPT_ROUTE,
    task_kind: str = "opt",
    extra_directives: str = "",
) -> dict[str, Any]:
    job_dir = root / name
    job_dir.mkdir(parents=True)
    inp = job_dir / "job.inp"
    directives = f"{extra_directives.strip()}\n" if extra_directives.strip() else ""
    inp.write_text(
        f"! {route}\n{directives}* xyz 0 1\nC 0 0 0\nO 1.1 0 0\n*\n",
        encoding="utf-8",
    )
    binding_state: dict[str, Any] = {"selected_inp": str(inp)}
    generation = bind_report_generation(job_dir, binding_state)
    bound_inp = Path(binding_state["selected_inp"])
    out = generation / "job.out"
    out.write_text(
        _out_text(energy, coords, route=route, converged=task_kind == "opt"), encoding="utf-8"
    )
    stage_id = f"orca_{name}"
    output_identity = confined_output_identity(generation, out)
    state = {
        "schema_version": 1,
        "engine": "orca",
        "job": {"id": stage_id, "dir": str(job_dir)},
        "status": {"state": "completed"},
        "input": {"primary_path": str(bound_inp)},
        "timestamps": {"started_at": "2026-07-05T01:00:00+00:00", "updated_at": ""},
        "engine_payload": {
            "run_id": "run_test",
            "attempts": [
                {
                    "index": 1,
                    "out_path": str(out),
                    "output_identity": output_identity,
                }
            ],
            "execution_provenance": binding_state["execution_provenance"],
            "final_result": {"last_out_path": str(out)},
        },
        "execution_provenance": binding_state["execution_provenance"],
    }
    (generation / "job_state.json").write_text(json.dumps(state), encoding="utf-8")
    _write_machine(generation, state)
    return {
        "stage_id": stage_id,
        "stage_kind": "orca_stage",
        "status": "completed",
        "metadata": {
            "selected_input_label": name,
            "child_job_id": stage_id,
            "run_id": "run_test",
        },
        "task": {"engine": "orca", "task_kind": task_kind},
        "output_artifacts": [
            {"kind": "orca_output_dir", "path": str(generation)},
            {"kind": "orca_report_json", "path": str(generation / RUN_REPORT_JSON_FILE)},
        ],
    }


def _two_conformer_payload(tmp_path: Path) -> dict[str, Any]:
    # Two identical NO geometries within the energy window: they RMSD-merge, so
    # only the lower-energy representative should be fanned out.
    return {
        "workflow_id": "wf_ie",
        "template_name": "conformer_screening",
        "status": "running",
        "reaction_key": "input",
        "stages": [
            _completed_opt_stage(tmp_path, "conf_low", energy=-50.00005, coords=_COORDS_C),
            _completed_opt_stage(tmp_path, "conf_high", energy=-50.0, coords=_COORDS_C),
        ],
        "metadata": {
            "request": {
                "parameters": {
                    "charge": 0,
                    "multiplicity": 1,
                    "interaction_energy": _INTERACTION_CONFIG,
                    "rmsd_dedup": {
                        "enabled": True,
                        "rmsd_threshold_angstrom": 0.25,
                        "energy_window_kcal": 1.0,
                        "heavy_atoms_only": True,
                    },
                }
            }
        },
    }


def _interaction_stages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        stage
        for stage in payload["stages"]
        if str(stage.get("metadata", {}).get("role", "")).startswith("interaction_")
    ]


def test_fan_out_targets_only_the_rmsd_representative(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    created = append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws")
    assert created is True

    interaction = _interaction_stages(payload)
    roles = sorted(stage["metadata"]["role"] for stage in interaction)
    assert roles == [
        "interaction_complex_sp",
        "interaction_fragment",
        "interaction_fragment",
    ]
    parents = {stage["metadata"]["parent_stage_id"] for stage in interaction}
    # Only the lower-energy representative is fanned out (the duplicate is dropped).
    assert parents == {"orca_conf_low"}


def test_fan_out_is_idempotent_on_a_second_advance(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is True
    assert all(
        is_valid_interaction_stage_contract(stage, payload["stages"])
        for stage in _interaction_stages(payload)
    )
    before = len(payload["stages"])
    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is False
    assert len(payload["stages"]) == before


def test_spoofed_primary_interaction_role_does_not_block_fan_out(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    primary = payload["stages"][0]
    payload["stages"] = [primary]
    primary["metadata"].update(
        {
            "role": "interaction_fragment",
            "parent_stage_id": "orca_missing_parent",
            "fragment_index": 0,
            "interaction_config_fingerprint": "spoofed-generation",
        }
    )

    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is True
    generated = [
        stage for stage in payload["stages"] if stage.get("task", {}).get("task_kind") == "sp"
    ]
    assert len(generated) == 3
    assert {stage["metadata"]["parent_stage_id"] for stage in generated} == {"orca_conf_low"}
    assert all(is_valid_interaction_stage_contract(stage, payload["stages"]) for stage in generated)


def test_wrong_generation_fingerprint_sp_does_not_block_or_claim_fan_out(
    tmp_path: Path,
) -> None:
    payload = _two_conformer_payload(tmp_path)
    spoofed = _completed_opt_stage(
        tmp_path,
        "spoofed_sp",
        energy=-49.0,
        coords=_COORDS_D,
        route=_SP_ROUTE,
        task_kind="sp",
    )
    spoofed["metadata"].update(
        {
            "role": "interaction_complex_sp",
            "parent_stage_id": "orca_conf_low",
            "interaction_config_fingerprint": "b" * 64,
        }
    )
    payload["stages"].append(spoofed)
    params = payload["metadata"]["request"]["parameters"]
    expected_fingerprint = interaction_energy_config_fingerprint(
        params["interaction_energy"],
        complex_charge=0,
        complex_multiplicity=1,
        rmsd_dedup=params["rmsd_dedup"],
    )

    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is True

    trusted = [
        stage
        for stage in payload["stages"]
        if is_valid_interaction_stage_contract(
            stage,
            payload["stages"],
            expected_config_fingerprint=expected_fingerprint,
        )
    ]
    assert len(trusted) == 3
    assert spoofed not in trusted


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("child", "stage_kind", "xtb_stage"),
        ("child_task", "engine", "xtb"),
        ("parent", "stage_kind", "xtb_stage"),
        ("parent_task", "engine", "xtb"),
        ("parent_metadata", "role", "interaction_fragment"),
    ],
)
def test_exact_interaction_contract_rejects_contradictory_durable_rows(
    target: str,
    field: str,
    value: str,
) -> None:
    parent: dict[str, Any] = {
        "stage_id": "orca_parent",
        "stage_kind": "orca_stage",
        "task": {"engine": "orca", "task_kind": "opt"},
        "metadata": {},
    }
    child: dict[str, Any] = {
        "stage_id": "orca_interaction_complex",
        "stage_kind": "orca_stage",
        "task": {"engine": "orca", "task_kind": "sp"},
        "metadata": {
            "role": "interaction_complex_sp",
            "parent_stage_id": "orca_parent",
            "interaction_config_fingerprint": "a" * 64,
        },
    }
    stages = [parent, child]
    assert is_valid_interaction_stage_contract(child, stages)
    assert not is_valid_interaction_stage_contract(
        child,
        stages,
        expected_config_fingerprint="b" * 64,
    )

    corrupted = deepcopy(stages)
    corrupted_parent, corrupted_child = corrupted
    locations = {
        "child": corrupted_child,
        "child_task": corrupted_child["task"],
        "parent": corrupted_parent,
        "parent_task": corrupted_parent["task"],
        "parent_metadata": corrupted_parent["metadata"],
    }
    locations[target][field] = value

    assert not is_valid_interaction_stage_contract(corrupted_child, corrupted)


@pytest.mark.parametrize("fingerprint", [None, "", "spoofed-generation", "A" * 64])
def test_exact_interaction_contract_requires_canonical_generation_fingerprint(
    fingerprint: str | None,
) -> None:
    parent: dict[str, Any] = {
        "stage_id": "orca_parent",
        "stage_kind": "orca_stage",
        "task": {"engine": "orca", "task_kind": "opt"},
        "metadata": {},
    }
    child: dict[str, Any] = {
        "stage_id": "orca_interaction_complex",
        "stage_kind": "orca_stage",
        "task": {"engine": "orca", "task_kind": "sp"},
        "metadata": {
            "role": "interaction_complex_sp",
            "parent_stage_id": "orca_parent",
            "interaction_config_fingerprint": fingerprint,
        },
    }

    assert not is_valid_interaction_stage_contract(child, [parent, child])


def test_fan_out_completes_a_partial_fragment_set(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws")
    payload["stages"] = [
        stage for stage in payload["stages"] if stage.get("metadata", {}).get("fragment_index") != 1
    ]
    remaining = len(_interaction_stages(payload))
    created = append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws")
    assert created is True
    interaction = _interaction_stages(payload)
    assert len(interaction) == remaining + 1
    assert any(stage["metadata"].get("fragment_index") == 1 for stage in interaction)


def test_disabled_interaction_energy_is_a_noop(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    params = payload["metadata"]["request"]["parameters"]
    params["interaction_energy"] = {"enabled": False, "fragments": []}
    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is False


def test_materializer_rejects_fragment_states_with_wrong_electron_parity(
    tmp_path: Path,
) -> None:
    payload = _two_conformer_payload(tmp_path)
    params = payload["metadata"]["request"]["parameters"]
    params["interaction_energy"] = {
        **_INTERACTION_CONFIG,
        "fragments": [
            {"atom_indices": [0], "charge": 0, "multiplicity": 2, "label": "carbon"},
            {"atom_indices": [1], "charge": 0, "multiplicity": 2, "label": "oxygen"},
        ],
    }

    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is False
    assert _interaction_stages(payload) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("charge", -0.5),
        ("charge", True),
        ("multiplicity", 1.5),
        ("multiplicity", True),
    ],
)
def test_materializer_rejects_lossy_complex_electronic_state(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _two_conformer_payload(tmp_path)
    payload["metadata"]["request"]["parameters"][field] = value

    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is False
    assert _interaction_stages(payload) == []
    assert payload["metadata"]["workflow_error"] == {
        "status": "failed",
        "scope": "conformer_screening_interaction_energy",
        "reason": "invalid_electronic_state",
        "message": (
            "The complex and fragment electronic states required for interaction energy "
            "are invalid or incompatible."
        ),
    }


def test_materializer_enforces_hard_fragment_cap_against_durable_state(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    params = payload["metadata"]["request"]["parameters"]
    params["interaction_energy"] = {
        "enabled": True,
        "sp_route_line": "! HF TightSCF",
        "max_fragments": 9,
        "fragments": [
            {"atom_indices": [index], "charge": 0, "multiplicity": 1, "label": f"f{index}"}
            for index in range(9)
        ],
    }
    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is False
    assert _interaction_stages(payload) == []


def test_materializer_rejects_non_optimization_orca_stage(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    payload["stages"] = [payload["stages"][0]]
    payload["stages"][0]["task"]["task_kind"] = "sp"
    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is False
    assert _interaction_stages(payload) == []


def test_materializer_rejects_contradictory_orca_stage_engine(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    payload["stages"][0]["task"]["engine"] = "xtb"

    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is False
    assert _interaction_stages(payload) == []


def test_materializer_rejects_mixed_optimization_science_directives(
    tmp_path: Path,
) -> None:
    payload = _two_conformer_payload(tmp_path)
    payload["stages"] = [
        _completed_opt_stage(tmp_path, "conf_a", energy=-50.00005, coords=_COORDS_C),
        _completed_opt_stage(
            tmp_path,
            "conf_b",
            energy=-50.0,
            coords=_COORDS_D,
            extra_directives="%scf\n  MaxIter 400\nend",
        ),
    ]

    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is False
    assert _interaction_stages(payload) == []


@pytest.mark.parametrize(
    ("route_a", "route_b", "directives_a", "directives_b"),
    [
        (
            _OPT_ROUTE,
            _OPT_ROUTE,
            "%maxcore 1024\n%pal nprocs 2 end",
            "%maxcore 4096\n%pal nprocs 8 end",
        ),
        (f"{_OPT_ROUTE} PAL4", f"{_OPT_ROUTE} PAL8", "", ""),
    ],
)
def test_materializer_allows_resource_only_optimization_differences(
    tmp_path: Path,
    route_a: str,
    route_b: str,
    directives_a: str,
    directives_b: str,
) -> None:
    payload = _two_conformer_payload(tmp_path)
    payload["stages"] = [
        _completed_opt_stage(
            tmp_path,
            "conf_a",
            energy=-50.00005,
            coords=_COORDS_C,
            route=route_a,
            extra_directives=directives_a,
        ),
        _completed_opt_stage(
            tmp_path,
            "conf_b",
            energy=-50.0,
            coords=_COORDS_D,
            route=route_b,
            extra_directives=directives_b,
        ),
    ]

    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is True
    parents = {stage["metadata"]["parent_stage_id"] for stage in _interaction_stages(payload)}
    assert parents == {"orca_conf_a"}


def test_materializer_uses_uniform_single_point_energy_for_rmsd_representative(
    tmp_path: Path,
) -> None:
    payload = _two_conformer_payload(tmp_path)
    payload["stages"] = [
        _completed_opt_stage(tmp_path, "conf_a", energy=-50.00005, coords=_COORDS_C),
        _completed_opt_stage(tmp_path, "conf_b", energy=-50.0, coords=_COORDS_D),
        _completed_opt_stage(
            tmp_path,
            "sp_a",
            energy=-100.0,
            coords=_COORDS_C,
            route=_SP_ROUTE,
            task_kind="sp",
        ),
        _completed_opt_stage(
            tmp_path,
            "sp_b",
            energy=-100.00005,
            coords=_COORDS_D,
            route=_SP_ROUTE,
            task_kind="sp",
        ),
    ]
    payload["metadata"]["request"]["parameters"]["rmsd_dedup"]["energy_window_kcal"] = 0.1
    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is True
    parents = {stage["metadata"]["parent_stage_id"] for stage in _interaction_stages(payload)}
    assert parents == {"orca_conf_b"}


def test_materializer_rejects_mixed_single_point_science_directives(
    tmp_path: Path,
) -> None:
    payload = _two_conformer_payload(tmp_path)
    payload["stages"] = [
        _completed_opt_stage(tmp_path, "conf_a", energy=-50.00005, coords=_COORDS_C),
        _completed_opt_stage(tmp_path, "conf_b", energy=-50.0, coords=_COORDS_D),
        _completed_opt_stage(
            tmp_path,
            "sp_a",
            energy=-100.0,
            coords=_COORDS_C,
            route=_SP_ROUTE,
            task_kind="sp",
        ),
        _completed_opt_stage(
            tmp_path,
            "sp_b",
            energy=-100.00005,
            coords=_COORDS_D,
            route=_SP_ROUTE,
            task_kind="sp",
            extra_directives="%scf\n  MaxIter 400\nend",
        ),
    ]
    payload["metadata"]["request"]["parameters"]["rmsd_dedup"]["energy_window_kcal"] = 0.1

    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is True

    parents = {stage["metadata"]["parent_stage_id"] for stage in _interaction_stages(payload)}
    assert parents == {"orca_conf_a"}


@pytest.mark.parametrize(
    ("route_a", "route_b", "directives_a", "directives_b"),
    [
        (
            _SP_ROUTE,
            _SP_ROUTE,
            "%maxcore 1024\n%pal nprocs 2 end",
            "%maxcore 4096\n%pal nprocs 8 end",
        ),
        (f"{_SP_ROUTE} PAL4", f"{_SP_ROUTE} PAL8", "", ""),
    ],
)
def test_materializer_allows_resource_only_single_point_differences(
    tmp_path: Path,
    route_a: str,
    route_b: str,
    directives_a: str,
    directives_b: str,
) -> None:
    payload = _two_conformer_payload(tmp_path)
    payload["stages"] = [
        _completed_opt_stage(tmp_path, "conf_a", energy=-50.00005, coords=_COORDS_C),
        _completed_opt_stage(tmp_path, "conf_b", energy=-50.0, coords=_COORDS_D),
        _completed_opt_stage(
            tmp_path,
            "sp_a",
            energy=-100.0,
            coords=_COORDS_C,
            route=route_a,
            task_kind="sp",
            extra_directives=directives_a,
        ),
        _completed_opt_stage(
            tmp_path,
            "sp_b",
            energy=-100.00005,
            coords=_COORDS_D,
            route=route_b,
            task_kind="sp",
            extra_directives=directives_b,
        ),
    ]
    payload["metadata"]["request"]["parameters"]["rmsd_dedup"]["energy_window_kcal"] = 0.1

    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is True

    parents = {stage["metadata"]["parent_stage_id"] for stage in _interaction_stages(payload)}
    assert parents == {"orca_conf_b"}


def test_partial_success_terminal_ensemble_fans_out_completed_subset(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    payload["stages"][0]["status"] = "failed"
    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is True
    parents = {stage["metadata"]["parent_stage_id"] for stage in _interaction_stages(payload)}
    assert parents == {"orca_conf_high"}


def test_invalid_durable_rmsd_config_blocks_interaction_fanout(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    payload["metadata"]["request"]["parameters"]["rmsd_dedup"]["rmsd_threshold_angstrom"] = float(
        "nan"
    )
    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is False
    assert _interaction_stages(payload) == []


def test_opt_selected_input_output_route_mismatch_blocks_fanout(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    for stage in payload["stages"]:
        stage_dir = Path(stage["output_artifacts"][0]["path"])
        inp_path = stage_dir / "job.inp"
        inp_path.write_text(
            inp_path.read_text(encoding="utf-8").replace(_OPT_ROUTE, "HF STO-3G Opt"),
            encoding="utf-8",
        )
    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is False
    assert _interaction_stages(payload) == []


def test_opt_terminal_output_mutation_cannot_choose_representative(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    mutated_generation = Path(payload["stages"][0]["output_artifacts"][0]["path"])
    output_path = mutated_generation / "job.out"
    output_path.write_text(
        output_path.read_text(encoding="utf-8").replace("-50.000050000000", "-99.000000000000"),
        encoding="utf-8",
    )

    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is True
    parents = {stage["metadata"]["parent_stage_id"] for stage in _interaction_stages(payload)}
    assert parents == {"orca_conf_high"}


def test_custom_rmsd_grouping_is_part_of_materialized_generation(tmp_path: Path) -> None:
    payload = _two_conformer_payload(tmp_path)
    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is True
    params = payload["metadata"]["request"]["parameters"]
    expected = interaction_energy_config_fingerprint(
        params["interaction_energy"],
        complex_charge=0,
        complex_multiplicity=1,
        rmsd_dedup=params["rmsd_dedup"],
    )
    assert {
        stage["metadata"]["interaction_config_fingerprint"]
        for stage in _interaction_stages(payload)
    } == {expected}
