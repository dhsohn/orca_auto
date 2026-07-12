"""Interaction-energy single-point fan-out (advance-phase materialization) tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orca_auto.flow.orchestration.interaction_energy_materialization import (
    append_interaction_energy_stages_impl,
)

_OPT_ROUTE = "B3LYP def2-SVP Opt"
_COORDS_C = (("N", 0.0, 0.0, 0.0), ("O", 1.100000, 0.0, 0.0))

_INTERACTION_CONFIG = {
    "enabled": True,
    "sp_route_line": "! r2scan-3c TightSCF",
    "max_fragments": 2,
    "fragments": [
        {"atom_indices": [0], "charge": 0, "multiplicity": 1, "label": "host"},
        {"atom_indices": [1], "charge": 0, "multiplicity": 1, "label": "guest"},
    ],
}


def _out_text(energy: float, coords: tuple[tuple[str, float, float, float], ...]) -> str:
    lines = [
        "                                 Program Version 6.0.1 -  RELEASE  -",
        f"|  1> ! {_OPT_ROUTE}",
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
        "THE OPTIMIZATION HAS CONVERGED",
        "",
        "                             ****ORCA TERMINATED NORMALLY****",
        "TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds 3 msec",
    ]
    return "\n".join(lines)


def _completed_opt_stage(
    root: Path, name: str, *, energy: float, coords: tuple[tuple[str, float, float, float], ...]
) -> dict[str, Any]:
    stage_dir = root / name
    stage_dir.mkdir(parents=True)
    inp = stage_dir / "job.inp"
    inp.write_text(f"! {_OPT_ROUTE}\n* xyz 0 1\nN 0 0 0\nO 1.1 0 0\n*\n", encoding="utf-8")
    out = stage_dir / "job.out"
    out.write_text(_out_text(energy, coords), encoding="utf-8")
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
    assert _interaction_stages(payload) == []
