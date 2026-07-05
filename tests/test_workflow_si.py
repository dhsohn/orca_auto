"""Workflow SI assembly (workflow_si.md + si_data.csv) tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.artifacts import WORKFLOW_SI_CSV_FILE, WORKFLOW_SI_MD_FILE
from orca_auto.flow.workflow.si import (
    collect_workflow_si_data,
    render_workflow_si_csv,
    render_workflow_si_md,
    write_workflow_si,
)

_COORDS_A = (
    ("C", 0.0, 1.234567, -0.987654),
    ("H", 0.123456, -0.654321, 2.0),
)
_COORDS_B = (
    ("C", 0.5, 0.5, 0.5),
    ("H", 1.5, 0.5, 0.5),
)


def _out_text(
    *,
    route: str,
    energy: float,
    coords: tuple[tuple[str, float, float, float], ...],
    freqs: tuple[float, ...] = (),
    thermo: bool = False,
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
        "THE OPTIMIZATION HAS CONVERGED",
    ]
    if freqs:
        lines += ["", "VIBRATIONAL FREQUENCIES", "-----------------------", ""]
        lines += [f"{index:6d}: {value:12.2f} cm**-1" for index, value in enumerate(freqs)]
        lines += [
            "",
            "NORMAL MODES",
            "------------",
            "",
            "                  0          1",
            "      0       0.700000   0.100000",
            "      1       0.100000   0.000000",
            "      2       0.000000   0.000000",
            "      3       0.500000   0.000000",
            "      4       0.000000   0.200000",
            "      5       0.000000   0.100000",
        ]
    if thermo:
        lines += [
            "--------------------------",
            "THERMOCHEMISTRY AT 298.15K",
            "--------------------------",
            "Zero point energy                ...      0.08843782 Eh",
            f"Total enthalpy                   ...  {energy + 0.15:.8f} Eh",
            f"Final Gibbs free energy          ...  {energy + 0.11789012:.8f} Eh",
            "G-E(el)                          ...      0.11789012 Eh",
        ]
    lines += [
        "",
        "                             ****ORCA TERMINATED NORMALLY****",
        "TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds 3 msec",
    ]
    return "\n".join(lines)


def _stage_dir(
    root: Path,
    name: str,
    *,
    route: str,
    energy: float,
    coords: tuple[tuple[str, float, float, float], ...],
    freqs: tuple[float, ...] = (),
    thermo: bool = False,
) -> Path:
    stage_dir = root / name
    stage_dir.mkdir(parents=True)
    inp = stage_dir / "job.inp"
    inp.write_text(f"! {route}\n* xyz 0 1\nC 0 0 0\n*\n", encoding="utf-8")
    out = stage_dir / "job.out"
    out.write_text(
        _out_text(route=route, energy=energy, coords=coords, freqs=freqs, thermo=thermo),
        encoding="utf-8",
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
    return stage_dir


def _orca_stage(
    stage_id: str, stage_dir: Path, *, status: str = "completed", label: str = ""
) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "stage_kind": "orca_stage",
        "status": status,
        "metadata": {"selected_input_label": label or stage_id},
        "output_artifacts": [{"kind": "orca_output_dir", "path": str(stage_dir)}],
    }


def _payload(stages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "workflow_id": "wf_si_test",
        "template_name": "conformer_screening",
        "status": "completed",
        "reaction_key": "input",
        "stages": stages,
    }


def _write_multi_xyz(path: Path, frames: int) -> None:
    blocks = [f"2\n frame {index}\nC 0 0 0\nH 1 0 0\n" for index in range(frames)]
    path.write_text("".join(blocks), encoding="utf-8")


def test_workflow_si_ranks_structures_and_reports_funnel(tmp_path: Path) -> None:
    crest_dir = tmp_path / "01_crest"
    crest_dir.mkdir()
    conformers = crest_dir / "crest_conformers.xyz"
    _write_multi_xyz(conformers, frames=5)

    dir_a = _stage_dir(
        tmp_path,
        "conf_01",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.001,
        coords=_COORDS_A,
        freqs=(30.0, 120.0),
        thermo=True,
    )
    dir_b = _stage_dir(
        tmp_path,
        "conf_02",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.005,
        coords=_COORDS_B,
        freqs=(35.0, 110.0),
        thermo=True,
    )

    payload = _payload(
        [
            {
                "stage_id": "crest_01",
                "stage_kind": "crest_stage",
                "status": "completed",
                "metadata": {"input_role": "molecule"},
                "output_artifacts": [{"kind": "crest_conformer", "path": str(conformers)}],
            },
            _orca_stage("orca_conf_01", dir_a, label="conf_01"),
            _orca_stage("orca_conf_02", dir_b, label="conf_02"),
        ]
    )

    data = collect_workflow_si_data(payload)
    assert [entry.block.name for entry in data.entries] == ["conf_02", "conf_01"]
    assert data.crest_conformer_total == 5

    rendered = render_workflow_si_md(data)
    assert "## Computational details" in rendered
    assert "CREST (5 conformers)" in rendered
    assert "2 structures were refined with ORCA" in rendered
    assert "harmonic frequency calculations" in rendered
    assert "B3LYP/def2-SVP" in rendered
    assert "298.15 K" in rendered
    assert "== conf_02 ==" in rendered
    # ΔE between the two conformers: 0.004 Eh ≈ +2.51 kcal/mol
    assert "+2.51" in rendered

    csv_text = render_workflow_si_csv(data)
    csv_lines = csv_text.strip().splitlines()
    assert len(csv_lines) == 3
    assert csv_lines[0].startswith("name,stage_id,kind")
    assert csv_lines[1].startswith("conf_02,")


def test_single_point_pairs_by_identical_geometry(tmp_path: Path) -> None:
    opt_dir = _stage_dir(
        tmp_path,
        "min_a",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.0,
        coords=_COORDS_A,
        freqs=(30.0, 120.0),
        thermo=True,
    )
    sp_same = _stage_dir(
        tmp_path,
        "sp_a",
        route="wB97M-V def2-TZVPP",
        energy=-100.5,
        coords=_COORDS_A,
    )
    sp_other = _stage_dir(
        tmp_path,
        "sp_b",
        route="wB97M-V def2-TZVPP",
        energy=-100.7,
        coords=_COORDS_B,
    )

    payload = _payload(
        [
            _orca_stage("orca_min_a", opt_dir, label="min_a"),
            _orca_stage("orca_sp_a", sp_same, label="sp_a"),
            _orca_stage("orca_sp_b", sp_other, label="sp_b"),
        ]
    )

    data = collect_workflow_si_data(payload)
    assert len(data.entries) == 1
    entry = data.entries[0]
    assert entry.sp_energy == pytest.approx(-100.5)
    assert entry.sp_label == "sp_a"
    assert entry.composite_gibbs == pytest.approx(-100.5 + 0.11789012)
    # The paired SP's block is kept so its level stays documented.
    assert entry.sp_block is not None
    assert entry.sp_block.result.method == "wB97M-V"
    # The mismatched-geometry SP must not pair: it stays a standalone block.
    assert [extra.block.name for extra in data.extra_blocks] == ["sp_b"]

    rendered = render_workflow_si_md(data)
    assert "G(composite)" in rendered
    assert "composite Gibbs energies combine E(SP)" in rendered
    assert "G is the composite" in rendered
    # The composite energy must carry the SP level that produced it, or the SI
    # documents an unreproducible number (Codex #48 P2).
    assert "wB97M-V/def2-TZVPP" in rendered
    assert "! wB97M-V def2-TZVPP" in rendered

    csv_text = render_workflow_si_csv(data)
    # The stationary row carries the paired SP's method/basis/version/route.
    assert "wB97M-V,def2-TZVPP,,6.0.1,wB97M-V def2-TZVPP" in csv_text


def test_composite_table_ranks_by_single_point_energy(tmp_path: Path) -> None:
    # SP and opt-level orderings can disagree; with the composite convention
    # active, E, ΔE, and the ranking must follow E(SP) — otherwise the table
    # publishes opt-level numbers next to SP-derived G values (Codex #48 P2).
    min_a = _stage_dir(
        tmp_path,
        "min_a",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.0,
        coords=_COORDS_A,
        freqs=(30.0, 120.0),
        thermo=True,
    )
    min_b = _stage_dir(
        tmp_path,
        "min_b",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.2,  # opt level prefers min_b ...
        coords=_COORDS_B,
        freqs=(35.0, 110.0),
        thermo=True,
    )
    sp_a = _stage_dir(
        tmp_path, "sp_a", route="wB97M-V def2-TZVPP", energy=-200.9, coords=_COORDS_A
    )  # ... but the SP level prefers min_a
    sp_b = _stage_dir(tmp_path, "sp_b", route="wB97M-V def2-TZVPP", energy=-200.5, coords=_COORDS_B)

    payload = _payload(
        [
            _orca_stage("orca_min_a", min_a, label="min_a"),
            _orca_stage("orca_min_b", min_b, label="min_b"),
            _orca_stage("orca_sp_a", sp_a, label="sp_a"),
            _orca_stage("orca_sp_b", sp_b, label="sp_b"),
        ]
    )

    rendered = render_workflow_si_md(collect_workflow_si_data(payload))

    assert "E(SP)/Eh" in rendered
    table_rows = [line for line in rendered.splitlines() if line.lstrip().startswith(("1 ", "2 "))]
    assert "min_a" in table_rows[0] and "-200.900000" in table_rows[0]
    assert "min_b" in table_rows[1] and "+251.00" in table_rows[1]  # 0.4 Eh at the SP level
    assert "E, ΔE, and the ranking are at the single-point level" in rendered


def test_opt_only_workflow_does_not_claim_frequency_calculations(tmp_path: Path) -> None:
    # The default conformer-screening route is Opt-only (no Freq): the methods
    # paragraph must not assert harmonic frequency calculations that never ran
    # (Codex #48 P2).
    stage_dir = _stage_dir(
        tmp_path,
        "opt_only",
        route="r2SCAN-3c Opt TightSCF",
        energy=-100.0,
        coords=_COORDS_A,
    )

    data = collect_workflow_si_data(_payload([_orca_stage("orca_opt_only", stage_dir)]))
    rendered = render_workflow_si_md(data)

    assert "Geometry optimizations were performed" in rendered
    assert "harmonic frequency" not in rendered


def test_failed_and_scan_stages_are_excluded_with_reasons(tmp_path: Path) -> None:
    ok_dir = _stage_dir(
        tmp_path,
        "ok",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.0,
        coords=_COORDS_A,
        freqs=(30.0, 120.0),
        thermo=True,
    )
    failed_dir = _stage_dir(
        tmp_path,
        "failed",
        route="B3LYP def2-SVP Opt Freq",
        energy=-99.0,
        coords=_COORDS_B,
    )
    scan_stage = _orca_stage("orca_scan", ok_dir, label="scan")
    scan_stage["task"] = {"task_kind": "relaxed_scan"}

    payload = _payload(
        [
            _orca_stage("orca_ok", ok_dir, label="ok"),
            _orca_stage("orca_failed", failed_dir, status="failed", label="failed"),
            scan_stage,
        ]
    )

    data = collect_workflow_si_data(payload)
    assert len(data.entries) == 1
    reasons = {stage.stage_id: stage.reason for stage in data.excluded}
    assert "orca_failed" in reasons and "stage status: failed" in reasons["orca_failed"]
    assert "orca_scan" in reasons and "relaxed scan" in reasons["orca_scan"]

    rendered = render_workflow_si_md(data)
    assert "## Excluded jobs" in rendered
    assert "stage status: failed" in rendered


def test_write_workflow_si_writes_and_cleans_up(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stage_dir = _stage_dir(
        tmp_path,
        "only",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.0,
        coords=_COORDS_A,
        freqs=(30.0, 120.0),
        thermo=True,
    )

    payload = _payload([_orca_stage("orca_only", stage_dir, label="only")])
    md_path = write_workflow_si(workspace, payload)
    assert md_path is not None and md_path.exists()
    assert (workspace / WORKFLOW_SI_CSV_FILE).exists()

    # A workflow without ORCA stages removes stale SI files.
    assert write_workflow_si(workspace, _payload([])) is None
    assert not (workspace / WORKFLOW_SI_MD_FILE).exists()
    assert not (workspace / WORKFLOW_SI_CSV_FILE).exists()
