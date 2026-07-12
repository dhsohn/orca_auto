"""Interaction-energy single-point fan-out (advance-phase materialization) tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orca_auto.flow.manifest import interaction_energy_config_fingerprint
from orca_auto.flow.orchestration.interaction_energy_materialization import (
    append_interaction_energy_stages_impl,
)

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
) -> dict[str, Any]:
    stage_dir = root / name
    stage_dir.mkdir(parents=True)
    inp = stage_dir / "job.inp"
    inp.write_text(f"! {route}\n* xyz 0 1\nC 0 0 0\nO 1.1 0 0\n*\n", encoding="utf-8")
    out = stage_dir / "job.out"
    out.write_text(
        _out_text(energy, coords, route=route, converged=task_kind == "opt"), encoding="utf-8"
    )
    state = {
        "schema_version": 1,
        "engine": "orca",
        "job": {"id": name, "dir": str(stage_dir)},
        "status": {"state": "completed"},
        "input": {"primary_path": str(inp)},
        "timestamps": {"started_at": "2026-07-05T01:00:00+00:00", "updated_at": ""},
        "engine_payload": {
            "run_id": "run_test",
            "max_retries": 0,
            "attempts": [{"index": 1, "out_path": str(out)}],
            "final_result": {"last_out_path": str(out)},
        },
    }
    (stage_dir / "job_state.json").write_text(json.dumps(state), encoding="utf-8")
    return {
        "stage_id": f"orca_{name}",
        "stage_kind": "orca_stage",
        "status": "completed",
        "metadata": {"selected_input_label": name},
        "task": {"task_kind": task_kind},
        "output_artifacts": [{"kind": "orca_output_dir", "path": str(stage_dir)}],
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
    before = len(payload["stages"])
    assert append_interaction_energy_stages_impl(payload, workspace_dir=tmp_path / "ws") is False
    assert len(payload["stages"]) == before


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
