"""Workflow SI assembly (workflow_si.md + si_data.csv) tests."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.artifacts import (
    INTERACTION_ENERGY_CSV_FILE,
    INTERACTION_ENERGY_CSV_OWNER_FILE,
    WORKFLOW_SI_CSV_FILE,
    WORKFLOW_SI_MD_FILE,
)
from orca_auto.flow.manifest import interaction_energy_config_fingerprint
from orca_auto.flow.workflow.si import (
    _CSV_COLUMNS,
    collect_workflow_si_data,
    render_interaction_energy_csv,
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
# A second chemical species (formula NO, not CH) for per-cluster tests.
_COORDS_C = (
    ("N", 0.0, 0.0, 0.0),
    ("O", 1.100000, 0.0, 0.0),
)
_COORDS_D = (
    ("N", 0.0, 0.0, 0.0),
    ("O", 1.150000, 0.0, 0.0),
)
_MIN_FREQS = (10.0, 20.0, 30.0, 40.0, 80.0, 120.0)
_ALT_MIN_FREQS = (12.0, 22.0, 35.0, 45.0, 90.0, 110.0)
_ONE_IMAG_FREQS = (-500.0, 20.0, 30.0, 40.0, 80.0, 120.0)


def _out_text(
    *,
    route: str,
    energy: float,
    coords: tuple[tuple[str, float, float, float], ...],
    freqs: tuple[float, ...] = (),
    thermo: bool = False,
    temp: float = 298.15,
    thermo_header: bool = True,
    opt_converged: bool | None = True,
    charge: int = 0,
    multiplicity: int = 1,
) -> str:
    lines = [
        "                                 Program Version 6.0.1 -  RELEASE  -",
        f"|  1> ! {route}",
        f"|  2> * xyz {charge} {multiplicity}",
        "|  3> C 0.0 0.0 0.0",
        "|  4> *",
        "",
        "CARTESIAN COORDINATES (ANGSTROEM)",
        "---------------------------------",
    ]
    lines += [f"  {el:<2}  {x:12.6f} {y:12.6f} {z:12.6f}" for el, x, y, z in coords]
    lines += ["", f"FINAL SINGLE POINT ENERGY     {energy:.12f}"]
    if opt_converged is True:
        lines.append("THE OPTIMIZATION HAS CONVERGED")
    elif opt_converged is False:
        lines.append("The optimization did not converge")
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
        # thermo_header=False keeps the Gibbs/correction lines but drops the
        # "THERMOCHEMISTRY AT" banner, so the parser sees a Gibbs energy with no
        # temperature — the case where an override must not weight blindly.
        if thermo_header:
            lines += ["--------------------------", f"THERMOCHEMISTRY AT {temp:.2f}K"]
        lines += [
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
    temp: float = 298.15,
    thermo_header: bool = True,
    opt_converged: bool | None = True,
    charge: int = 0,
    multiplicity: int = 1,
) -> Path:
    stage_dir = root / name
    stage_dir.mkdir(parents=True)
    inp = stage_dir / "job.inp"
    inp.write_text(f"! {route}\n* xyz {charge} {multiplicity}\nC 0 0 0\n*\n", encoding="utf-8")
    out = stage_dir / "job.out"
    out.write_text(
        _out_text(
            route=route,
            energy=energy,
            coords=coords,
            freqs=freqs,
            thermo=thermo,
            temp=temp,
            thermo_header=thermo_header,
            opt_converged=opt_converged,
            charge=charge,
            multiplicity=multiplicity,
        ),
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


def _payload(
    stages: list[dict[str, Any]],
    *,
    status: str = "completed",
    boltzmann_temperature_k: Any = None,
    template_name: str = "conformer_screening",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workflow_id": "wf_si_test",
        "template_name": template_name,
        "status": status,
        "reaction_key": "input",
        "stages": stages,
    }
    if boltzmann_temperature_k is not None:
        payload["metadata"] = {
            "request": {"parameters": {"boltzmann_temperature_k": boltzmann_temperature_k}}
        }
    return payload


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
        freqs=_MIN_FREQS,
        thermo=True,
    )
    dir_b = _stage_dir(
        tmp_path,
        "conf_02",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.005,
        coords=_COORDS_B,
        freqs=_ALT_MIN_FREQS,
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
        freqs=_MIN_FREQS,
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


def test_single_point_pair_includes_exact_geometry_tolerance_boundary(tmp_path: Path) -> None:
    minimum = _minimum(tmp_path, "min", energy=-100.0, coords=_COORDS_A)
    edge_coords = (
        _COORDS_A[0],
        ("H", 0.123556, -0.654321, 2.0),
    )
    single_point = _stage_dir(
        tmp_path,
        "sp_edge",
        route="wB97M-V def2-TZVPP",
        energy=-200.0,
        coords=edge_coords,
    )

    data = collect_workflow_si_data(
        _payload([_orca_stage("min", minimum), _orca_stage("sp", single_point)])
    )

    assert data.entries[0].sp_label == "sp"


def test_single_point_pair_requires_explicit_electronic_state_provenance(tmp_path: Path) -> None:
    minimum = _minimum(tmp_path, "min", energy=-100.0, coords=_COORDS_A)
    single_point = _stage_dir(
        tmp_path,
        "sp",
        route="wB97M-V def2-TZVPP",
        energy=-200.0,
        coords=_COORDS_A,
    )
    out_path = single_point / "job.out"
    out_path.write_text(
        "\n".join(
            line
            for line in out_path.read_text(encoding="utf-8").splitlines()
            if "* xyz " not in line
        ),
        encoding="utf-8",
    )

    data = collect_workflow_si_data(
        _payload([_orca_stage("min", minimum), _orca_stage("sp", single_point)])
    )

    assert data.entries[0].sp_block is None
    assert [entry.block.name for entry in data.extra_blocks] == ["sp"]


def test_workflow_si_never_renders_nonfinite_composite_energy(tmp_path: Path) -> None:
    minimum = _minimum(tmp_path, "min", energy=-100.0, coords=_COORDS_A)
    single_point = _stage_dir(
        tmp_path,
        "sp",
        route="wB97M-V def2-TZVPP",
        energy=-200.0,
        coords=_COORDS_A,
    )
    data = collect_workflow_si_data(
        _payload([_orca_stage("min", minimum), _orca_stage("sp", single_point)])
    )
    broken_entry = replace(data.entries[0], composite_gibbs=float("inf"))

    rendered = render_workflow_si_md(replace(data, entries=(broken_entry,)))

    assert "G(composite) =              inf" not in rendered
    assert "composite Gibbs energies combine" not in rendered


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
        freqs=_MIN_FREQS,
        thermo=True,
    )
    min_b = _stage_dir(
        tmp_path,
        "min_b",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.2,  # opt level prefers min_b ...
        coords=_COORDS_B,
        freqs=_ALT_MIN_FREQS,
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


def test_opt_only_refinements_use_sp_energies_without_composite(tmp_path: Path) -> None:
    # Opt-only structures (no Freq → no G−E(el)) with SP refinements: E/ΔE and
    # the ranking must still follow E(SP), and neither the table note nor the
    # methods text may claim composite Gibbs energies (Codex #48 P2).
    min_a = _stage_dir(
        tmp_path, "min_a", route="r2SCAN-3c Opt TightSCF", energy=-100.0, coords=_COORDS_A
    )
    min_b = _stage_dir(
        tmp_path,
        "min_b",
        route="r2SCAN-3c Opt TightSCF",
        energy=-100.2,  # opt level prefers min_b ...
        coords=_COORDS_B,
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

    data = collect_workflow_si_data(payload)
    assert all(entry.sp_energy is not None for entry in data.entries)
    assert all(entry.composite_gibbs is None for entry in data.entries)

    rendered = render_workflow_si_md(data)
    assert "E(SP)/Eh" in rendered
    table_rows = [line for line in rendered.splitlines() if line.lstrip().startswith(("1 ", "2 "))]
    assert "min_a" in table_rows[0] and "-200.900000" in table_rows[0]
    assert "min_b" in table_rows[1] and "+251.00" in table_rows[1]
    assert "E, ΔE, and the ranking are at the single-point level" in rendered
    assert "refined by single-point calculations" in rendered
    assert "composite" not in rendered  # no G−E(el) anywhere → no composite claim


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
        freqs=_MIN_FREQS,
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
    assert all(row is None for row in data.populations)
    assert "ensemble is incomplete" in data.population_note
    reasons = {stage.stage_id: stage.reason for stage in data.excluded}
    assert "orca_failed" in reasons and "stage status: failed" in reasons["orca_failed"]
    assert "orca_scan" in reasons and "relaxed scan" in reasons["orca_scan"]

    rendered = render_workflow_si_md(data)
    assert "## Excluded jobs" in rendered
    assert "stage status: failed" in rendered
    population_section = rendered.split("## Boltzmann populations", 1)[1].split("## Structures", 1)[
        0
    ]
    assert "100.00" not in population_section


def test_nonfinite_parsed_thermochemistry_is_excluded_from_workflow_si(tmp_path: Path) -> None:
    minimum = _minimum(tmp_path, "overflow", energy=-100.0, coords=_COORDS_A)
    out_path = minimum / "job.out"
    out_path.write_text(
        out_path.read_text(encoding="utf-8").replace(
            "THERMOCHEMISTRY AT 298.15K",
            f"THERMOCHEMISTRY AT {'9' * 400}.00K",
        ),
        encoding="utf-8",
    )

    data = collect_workflow_si_data(_payload([_orca_stage("overflow", minimum, label="overflow")]))

    assert data.entries == ()
    assert data.excluded[0].reason == "output contains a non-finite numeric result"
    rendered = render_workflow_si_md(data).lower()
    assert "inf eh" not in rendered
    assert "inf k" not in rendered


def test_write_workflow_si_writes_and_cleans_up(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stage_dir = _stage_dir(
        tmp_path,
        "only",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.0,
        coords=_COORDS_A,
        freqs=_MIN_FREQS,
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


def test_write_workflow_si_removes_pair_when_second_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import orca_auto.flow.workflow.si as si_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    md_path = workspace / WORKFLOW_SI_MD_FILE
    csv_path = workspace / WORKFLOW_SI_CSV_FILE
    md_path.write_text("old md", encoding="utf-8")
    csv_path.write_text("old csv", encoding="utf-8")
    minimum = _minimum(tmp_path, "conf", energy=-100.0, coords=_COORDS_A)
    payload = _payload([_orca_stage("orca_conf", minimum, label="conf")])
    real_write = si_mod.atomic_write_text
    calls = 0

    def fail_second(path: Path, text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second publish failed")
        real_write(path, text)

    monkeypatch.setattr(si_mod, "atomic_write_text", fail_second)

    assert si_mod.write_workflow_si(workspace, payload) is None
    assert not md_path.exists()
    assert not csv_path.exists()


# ---------------------------------------------------------------------------
# Boltzmann populations
# ---------------------------------------------------------------------------


def _minimum(root: Path, name: str, *, energy: float, coords: Any, temp: float = 298.15) -> Path:
    return _stage_dir(
        root,
        name,
        route="B3LYP def2-SVP Opt Freq",
        energy=energy,
        coords=coords,
        freqs=_MIN_FREQS,
        thermo=True,
        temp=temp,
    )


def test_boltzmann_populations_single_species(tmp_path: Path) -> None:
    lo = _minimum(tmp_path, "conf_lo", energy=-100.010, coords=_COORDS_A)
    hi = _minimum(tmp_path, "conf_hi", energy=-100.000, coords=_COORDS_B)
    payload = _payload(
        [_orca_stage("orca_lo", lo, label="conf_lo"), _orca_stage("orca_hi", hi, label="conf_hi")]
    )

    data = collect_workflow_si_data(payload)

    pops = [p for p in data.populations if p is not None]
    assert len(pops) == 2
    assert len({p.cluster_key for p in pops}) == 1  # one species
    assert pops[0].cluster_key.endswith("|0|1")
    assert sum(p.population or 0.0 for p in pops) == pytest.approx(1.0)
    assert data.boltzmann_temperature_k == pytest.approx(298.15)
    assert data.boltzmann_temperature_source == "thermochemistry output"

    by_name = {entry.block.name: data.populations[i] for i, entry in enumerate(data.entries)}
    lo_row = by_name["conf_lo"]
    hi_row = by_name["conf_hi"]
    assert lo_row is not None and hi_row is not None
    assert lo_row.population is not None and hi_row.population is not None
    assert lo_row.population > hi_row.population  # lower G dominates
    assert lo_row.rel_g_kcalmol == pytest.approx(0.0)  # baseline is the min

    rendered = render_workflow_si_md(data)
    assert "## Boltzmann populations" in rendered
    assert "298.15 K" in rendered
    assert "population/%" in rendered


def test_conformer_population_rejects_completed_transition_state_member(tmp_path: Path) -> None:
    minimum = _minimum(tmp_path, "min", energy=-100.0, coords=_COORDS_A)
    ts = _stage_dir(
        tmp_path,
        "ts",
        route="B3LYP def2-SVP OptTS Freq",
        energy=-99.9,
        coords=_COORDS_B,
        freqs=_ONE_IMAG_FREQS,
        thermo=True,
    )
    payload = _payload(
        [_orca_stage("orca_min", minimum, label="min"), _orca_stage("orca_ts", ts, label="ts")]
    )

    data = collect_workflow_si_data(payload)

    kinds = {entry.block.name: entry.block.kind for entry in data.entries}
    assert kinds == {"min": "min", "ts": "ts"}
    assert data.populations == (None, None)
    assert "ensemble is incomplete" in data.population_note


def test_all_nonminimum_conformer_members_render_population_omission_note(tmp_path: Path) -> None:
    ts = _stage_dir(
        tmp_path,
        "ts",
        route="B3LYP def2-SVP OptTS Freq",
        energy=-99.9,
        coords=_COORDS_B,
        freqs=_ONE_IMAG_FREQS,
        thermo=True,
    )

    data = collect_workflow_si_data(_payload([_orca_stage("orca_ts", ts, label="ts")]))

    assert data.populations == (None,)
    assert "ensemble is incomplete" in data.population_note
    rendered = render_workflow_si_md(data)
    assert "## Boltzmann populations" in rendered
    assert "ensemble is incomplete" in rendered


def test_nonconformer_population_excludes_transition_state_rows(tmp_path: Path) -> None:
    minimum = _minimum(tmp_path, "min", energy=-100.0, coords=_COORDS_A)
    ts = _stage_dir(
        tmp_path,
        "ts",
        route="B3LYP def2-SVP OptTS Freq",
        energy=-99.9,
        coords=_COORDS_B,
        freqs=_ONE_IMAG_FREQS,
        thermo=True,
    )

    data = collect_workflow_si_data(
        _payload(
            [_orca_stage("orca_min", minimum), _orca_stage("orca_ts", ts)],
            template_name="reaction_ts_search",
        )
    )

    by_kind = {entry.block.kind: data.populations[i] for i, entry in enumerate(data.entries)}
    assert by_kind["ts"] is None
    assert by_kind["min"] is not None
    assert by_kind["min"].population == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("freqs", "opt_converged"),
    [
        (_ONE_IMAG_FREQS, True),
        (_MIN_FREQS, None),
        (_MIN_FREQS, False),
    ],
)
def test_boltzmann_requires_a_converged_nimag_zero_minimum(
    tmp_path: Path,
    freqs: tuple[float, ...],
    opt_converged: bool | None,
) -> None:
    stage = _stage_dir(
        tmp_path,
        "not_verified",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.0,
        coords=_COORDS_A,
        freqs=freqs,
        thermo=True,
        opt_converged=opt_converged,
    )

    data = collect_workflow_si_data(
        _payload([_orca_stage("orca_not_verified", stage, label="not_verified")])
    )

    assert data.populations == (None,)
    assert "0 of 1 route-classified minima are usable" in data.population_note


def test_boltzmann_requires_a_complete_3n_vibrational_spectrum(tmp_path: Path) -> None:
    partial = _stage_dir(
        tmp_path,
        "partial_freq",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.0,
        coords=_COORDS_A,
        freqs=(30.0, 120.0),
        thermo=True,
    )

    data = collect_workflow_si_data(_payload([_orca_stage("partial", partial, label="partial")]))

    assert data.entries[0].block.analysis is not None
    assert len(data.entries[0].block.analysis.frequencies) == 2
    assert data.populations == (None,)
    assert "complete 3N spectra" in data.population_note


def test_conformer_opt_role_rejects_completed_nonminimum_member(tmp_path: Path) -> None:
    minimum = _minimum(tmp_path, "min", energy=-100.0, coords=_COORDS_A)
    single_point = _stage_dir(
        tmp_path,
        "sp",
        route="wB97M-V def2-TZVPP",
        energy=-200.0,
        coords=_COORDS_B,
    )
    sp_stage = _orca_stage("sp", single_point, label="sp")
    sp_stage["task"] = {"task_kind": "opt"}

    data = collect_workflow_si_data(_payload([_orca_stage("min", minimum, label="min"), sp_stage]))

    assert data.populations == (None,)
    assert "ensemble is incomplete" in data.population_note


def test_boltzmann_waits_for_terminal_workflow(tmp_path: Path) -> None:
    minimum = _minimum(tmp_path, "conf", energy=-100.0, coords=_COORDS_A)

    data = collect_workflow_si_data(
        _payload(
            [_orca_stage("orca_conf", minimum, label="conf")],
            status="running",
        )
    )

    assert data.populations == (None,)
    assert "ensemble is not terminal" in data.population_note


def test_boltzmann_normalizes_within_each_species(tmp_path: Path) -> None:
    stages = [
        _orca_stage("oa1", _minimum(tmp_path, "a1", energy=-100.010, coords=_COORDS_A), label="a1"),
        _orca_stage("oa2", _minimum(tmp_path, "a2", energy=-100.008, coords=_COORDS_B), label="a2"),
        _orca_stage("ob1", _minimum(tmp_path, "b1", energy=-200.010, coords=_COORDS_C), label="b1"),
        _orca_stage("ob2", _minimum(tmp_path, "b2", energy=-200.008, coords=_COORDS_D), label="b2"),
    ]

    data = collect_workflow_si_data(_payload(stages))

    by_cluster: dict[str, list[float]] = {}
    for i, _entry in enumerate(data.entries):
        row = data.populations[i]
        assert row is not None
        assert row.population is not None
        by_cluster.setdefault(row.cluster_key, []).append(row.population)
    assert len(by_cluster) == 2  # two distinct species
    for members in by_cluster.values():
        # Each species is a separate partition function, not one global Z.
        assert sum(members) == pytest.approx(1.0)
    assert "group " in render_workflow_si_md(data)  # multi-group headers


def test_opt_only_workflow_reports_no_populations(tmp_path: Path) -> None:
    stage_dir = _stage_dir(
        tmp_path, "opt", route="r2SCAN-3c Opt TightSCF", energy=-100.0, coords=_COORDS_A
    )

    data = collect_workflow_si_data(_payload([_orca_stage("orca_opt", stage_dir, label="opt")]))

    assert all(row is None for row in data.populations)
    assert data.boltzmann_temperature_k is None
    rendered = render_workflow_si_md(data)
    assert "## Boltzmann populations" in rendered  # minima exist, so the section shows...
    assert "no complete set" in rendered  # ...a fail-closed note, not numbers

    rows = list(csv.reader(render_workflow_si_csv(data).splitlines()))
    assert rows[1][-5:] == ["", "", "", "", ""]  # blank population cells


def test_boltzmann_temperature_override_and_disagreement(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    minimum = _minimum(tmp_path, "conf", energy=-100.0, coords=_COORDS_A)  # freq T = 298.15
    payload = _payload(
        [_orca_stage("orca_conf", minimum, label="conf")],
        boltzmann_temperature_k=298.15,
    )
    write_workflow_si(workspace, payload)
    md = (workspace / WORKFLOW_SI_MD_FILE).read_text(encoding="utf-8")
    assert "manifest boltzmann_temperature_k" in md
    assert "298.15 K" in md

    # The durable request, not a mutable workspace manifest, owns the value.
    (workspace / "flow.yaml").write_text("boltzmann_temperature_k: 353.15\n", encoding="utf-8")
    write_workflow_si(workspace, payload)
    stable = (workspace / WORKFLOW_SI_MD_FILE).read_text(encoding="utf-8")
    assert "manifest boltzmann_temperature_k" in stable
    assert "298.15 K" in stable

    # An override at a temperature the freq job did not use must fail closed.
    mismatched = _payload(
        [_orca_stage("orca_conf", minimum, label="conf")],
        boltzmann_temperature_k=298.64,
    )
    write_workflow_si(workspace, mismatched)
    md2 = (workspace / WORKFLOW_SI_MD_FILE).read_text(encoding="utf-8")
    assert "disagrees with the thermochemistry temperature" in md2


def test_boltzmann_temperature_tolerance_includes_exact_hundredth_kelvin(
    tmp_path: Path,
) -> None:
    first = _minimum(tmp_path, "first", energy=-100.0, coords=_COORDS_A, temp=298.15)
    second = _minimum(tmp_path, "second", energy=-100.0, coords=_COORDS_B, temp=298.16)

    data = collect_workflow_si_data(
        _payload(
            [_orca_stage("first", first), _orca_stage("second", second)],
        ),
        boltzmann_temperature_k=298.16,
    )

    assert all(row is not None for row in data.populations)
    assert data.boltzmann_temperature_k == pytest.approx(298.16)


def test_invalid_durable_boltzmann_temperature_omits_only_populations(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    minimum = _minimum(tmp_path, "conf", energy=-100.0, coords=_COORDS_A)
    payload = _payload(
        [_orca_stage("orca_conf", minimum, label="conf")],
        boltzmann_temperature_k="fast",
    )

    md_path = write_workflow_si(workspace, payload)

    assert md_path is not None
    rendered = md_path.read_text(encoding="utf-8")
    assert "durable boltzmann_temperature_k is invalid" in rendered
    assert "## Relative energies" in rendered


def test_malformed_manifest_does_not_suppress_si(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    minimum = _minimum(tmp_path, "conf", energy=-100.0, coords=_COORDS_A)
    payload = _payload([_orca_stage("orca_conf", minimum, label="conf")])
    (workspace / "flow.yaml").write_text("bad: [1, 2\n", encoding="utf-8")  # invalid YAML

    md_path = write_workflow_si(workspace, payload)

    assert md_path is not None and md_path.exists()  # live flow.yaml is not the durable source
    assert "## Relative energies" in md_path.read_text(encoding="utf-8")


def test_population_failure_still_writes_valid_si(tmp_path: Path, monkeypatch: Any) -> None:
    import orca_auto.flow.workflow.si as si_mod

    minimum = _minimum(tmp_path, "conf", energy=-100.0, coords=_COORDS_A)
    payload = _payload([_orca_stage("orca_conf", minimum, label="conf")])

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("population bug")

    monkeypatch.setattr(si_mod, "_compute_populations", _boom)

    data = si_mod.collect_workflow_si_data(payload)

    assert data.populations == (None,)  # isolated: base rows remain aligned and renderable
    assert "computation failed" in data.population_note
    rendered = si_mod.render_workflow_si_md(data)
    assert "## Relative energies" in rendered
    assert "## Structures" in rendered


def test_duplicate_stage_id_does_not_cross_map_populations(tmp_path: Path) -> None:
    lo = _minimum(tmp_path, "lo", energy=-100.010, coords=_COORDS_A)
    hi = _minimum(tmp_path, "hi", energy=-100.000, coords=_COORDS_B)
    # Both stages share an empty stage_id — previously the population map key.
    payload = _payload([_orca_stage("", lo, label="lo"), _orca_stage("", hi, label="hi")])

    data = collect_workflow_si_data(payload)

    by_name = {entry.block.name: data.populations[i] for i, entry in enumerate(data.entries)}
    lo_row = by_name["lo"]
    hi_row = by_name["hi"]
    assert lo_row is not None and hi_row is not None
    assert lo_row.population is not None and hi_row.population is not None
    assert lo_row.population > hi_row.population
    populated = [p for p in data.populations if p is not None]
    assert sum(p.population or 0.0 for p in populated) == pytest.approx(1.0)


def test_si_csv_appends_population_columns_without_touching_the_schema(tmp_path: Path) -> None:
    minimum = _minimum(tmp_path, "conf", energy=-100.0, coords=_COORDS_A)
    data = collect_workflow_si_data(_payload([_orca_stage("orca_conf", minimum, label="conf")]))

    fields = render_workflow_si_csv(data).splitlines()[0].split(",")

    assert fields[:27] == [
        "name",
        "stage_id",
        "kind",
        "formula",
        "charge",
        "multiplicity",
        "method",
        "basis_set",
        "solvation",
        "orca_version",
        "route",
        "E_Eh",
        "ZPE_Eh",
        "H_Eh",
        "G_Eh",
        "G_minus_Eel_Eh",
        "sp_method",
        "sp_basis_set",
        "sp_solvation",
        "sp_orca_version",
        "sp_route",
        "E_SP_Eh",
        "G_composite_Eh",
        "Nimag",
        "lowest_freq_cm1",
        "temperature_K",
        "warnings",
    ]
    assert fields[27:] == [
        "cluster_key",
        "rel_E_kcalmol",
        "rel_G_kcalmol",
        "boltzmann_T_K",
        "boltzmann_population",
    ]


def test_misaligned_population_rows_cannot_drop_base_csv_entries(tmp_path: Path) -> None:
    lo = _minimum(tmp_path, "lo", energy=-100.01, coords=_COORDS_A)
    hi = _minimum(tmp_path, "hi", energy=-100.00, coords=_COORDS_B)
    data = collect_workflow_si_data(
        _payload([_orca_stage("lo", lo, label="lo"), _orca_stage("hi", hi, label="hi")])
    )
    broken = replace(data, populations=(data.populations[0],))

    rows = list(csv.DictReader(render_workflow_si_csv(broken).splitlines()))

    assert {row["name"] for row in rows} == {"lo", "hi"}
    assert all(row["boltzmann_population"] == "" for row in rows)


def test_boltzmann_uses_composite_gibbs_when_every_member_is_refined(tmp_path: Path) -> None:
    # opt ordering prefers "lo" (lower opt energy); the SP refinement prefers "hi".
    lo = _minimum(tmp_path, "lo", energy=-100.010, coords=_COORDS_A)
    hi = _minimum(tmp_path, "hi", energy=-100.000, coords=_COORDS_B)
    sp_lo = _stage_dir(
        tmp_path, "sp_lo", route="wB97M-V def2-TZVPP", energy=-200.900, coords=_COORDS_A
    )
    sp_hi = _stage_dir(
        tmp_path, "sp_hi", route="wB97M-V def2-TZVPP", energy=-200.905, coords=_COORDS_B
    )
    payload = _payload(
        [
            _orca_stage("olo", lo, label="lo"),
            _orca_stage("ohi", hi, label="hi"),
            _orca_stage("splo", sp_lo, label="sp_lo"),
            _orca_stage("sphi", sp_hi, label="sp_hi"),
        ]
    )

    data = collect_workflow_si_data(payload)

    assert all(entry.composite_gibbs is not None for entry in data.entries)  # both refined
    pops = {entry.block.name: data.populations[i] for i, entry in enumerate(data.entries)}
    lo_row = pops["lo"]
    hi_row = pops["hi"]
    assert lo_row is not None and hi_row is not None
    assert lo_row.population is not None and hi_row.population is not None
    # Composite G follows the SP ordering, so "hi" dominates — proving the composite
    # (not the opt-level) Gibbs drives the populations.
    assert hi_row.population > lo_row.population
    assert hi_row.rel_g_kcalmol == pytest.approx(0.0)


def test_boltzmann_falls_back_to_plain_gibbs_when_sp_levels_differ(tmp_path: Path) -> None:
    lo = _minimum(tmp_path, "lo", energy=-100.010, coords=_COORDS_A)
    hi = _minimum(tmp_path, "hi", energy=-100.000, coords=_COORDS_B)
    sp_lo = _stage_dir(
        tmp_path, "sp_lo", route="wB97M-V def2-TZVPP D3BJ", energy=-200.900, coords=_COORDS_A
    )
    # Parsed method/basis match, but the exact executed routes differ.
    sp_hi = _stage_dir(
        tmp_path, "sp_hi", route="wB97M-V def2-TZVPP D4", energy=-200.905, coords=_COORDS_B
    )
    payload = _payload(
        [
            _orca_stage("olo", lo, label="lo"),
            _orca_stage("ohi", hi, label="hi"),
            _orca_stage("splo", sp_lo, label="sp_lo"),
            _orca_stage("sphi", sp_hi, label="sp_hi"),
        ]
    )

    data = collect_workflow_si_data(payload)

    assert all(entry.composite_gibbs is not None for entry in data.entries)  # both paired...
    pops = {entry.block.name: data.populations[i] for i, entry in enumerate(data.entries)}
    lo_row = pops["lo"]
    hi_row = pops["hi"]
    assert lo_row is not None and hi_row is not None
    assert lo_row.population is not None and hi_row.population is not None
    # ...but the SP levels differ, so plain (opt-level) Gibbs is used: "lo" dominates.
    assert lo_row.population > hi_row.population
    relative = (
        render_workflow_si_md(data)
        .split("## Relative energies", 1)[1]
        .split("## Boltzmann populations", 1)[0]
    )
    assert " 1  lo" in relative
    assert "single-point refinement levels differ" in relative


def test_boltzmann_omits_mixed_optimization_levels(tmp_path: Path) -> None:
    a = _stage_dir(
        tmp_path,
        "a",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.01,
        coords=_COORDS_A,
        freqs=_MIN_FREQS,
        thermo=True,
    )
    b = _stage_dir(
        tmp_path,
        "b",
        route="PBE0 def2-SVP Opt Freq",
        energy=-100.00,
        coords=_COORDS_B,
        freqs=_MIN_FREQS,
        thermo=True,
    )

    data = collect_workflow_si_data(
        _payload([_orca_stage("oa", a, label="a"), _orca_stage("ob", b, label="b")])
    )

    assert data.populations == (None, None)
    assert "provenance is missing or differs" in data.population_note


def test_boltzmann_omits_missing_required_provenance(tmp_path: Path) -> None:
    a = _minimum(tmp_path, "a", energy=-100.01, coords=_COORDS_A)
    b = _minimum(tmp_path, "b", energy=-100.00, coords=_COORDS_B)
    for stage_dir in (a, b):
        out_path = stage_dir / "job.out"
        lines = [
            line
            for line in out_path.read_text(encoding="utf-8").splitlines()
            if "Program Version" not in line
        ]
        out_path.write_text("\n".join(lines), encoding="utf-8")

    data = collect_workflow_si_data(
        _payload([_orca_stage("a", a, label="a"), _orca_stage("b", b, label="b")])
    )

    assert [entry.block.result.orca_version for entry in data.entries] == ["", ""]
    assert data.populations == (None, None)
    assert "provenance is missing or differs" in data.population_note


def test_boltzmann_never_merges_members_with_missing_echoed_state(tmp_path: Path) -> None:
    a = _stage_dir(
        tmp_path,
        "a",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.01,
        coords=_COORDS_A,
        freqs=_MIN_FREQS,
        thermo=True,
        charge=1,
        multiplicity=2,
    )
    b = _stage_dir(
        tmp_path,
        "b",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.00,
        coords=_COORDS_B,
        freqs=_MIN_FREQS,
        thermo=True,
        charge=-1,
        multiplicity=1,
    )
    for stage_dir in (a, b):
        out_path = stage_dir / "job.out"
        lines = [
            line
            for line in out_path.read_text(encoding="utf-8").splitlines()
            if "* xyz " not in line
        ]
        out_path.write_text("\n".join(lines), encoding="utf-8")

    data = collect_workflow_si_data(
        _payload([_orca_stage("a", a, label="a"), _orca_stage("b", b, label="b")])
    )

    assert {entry.block.result.input_line for entry in data.entries} == {
        "B3LYP def2-SVP Opt Freq",
    }
    assert all(not entry.block.result.electronic_state_verified for entry in data.entries)
    assert data.populations == (None, None)
    assert "provenance is missing or differs" in data.population_note


def test_single_point_pair_requires_unique_matching_electronic_state(tmp_path: Path) -> None:
    minimum = _minimum(tmp_path, "min", energy=-100.0, coords=_COORDS_A)
    wrong_state = _stage_dir(
        tmp_path,
        "sp_wrong_state",
        route="wB97M-V def2-TZVPP",
        energy=-200.0,
        coords=_COORDS_A,
        charge=1,
        multiplicity=2,
    )
    duplicate_a = _stage_dir(
        tmp_path,
        "sp_a",
        route="wB97M-V def2-TZVPP",
        energy=-201.0,
        coords=_COORDS_A,
    )
    duplicate_b = _stage_dir(
        tmp_path,
        "sp_b",
        route="wB97M-V def2-TZVPP",
        energy=-202.0,
        coords=_COORDS_A,
    )

    data = collect_workflow_si_data(
        _payload(
            [
                _orca_stage("omin", minimum, label="min"),
                _orca_stage("wrong", wrong_state, label="wrong"),
                _orca_stage("spa", duplicate_a, label="spa"),
                _orca_stage("spb", duplicate_b, label="spb"),
            ]
        )
    )

    assert data.entries[0].sp_block is None
    assert {entry.block.name for entry in data.extra_blocks} == {"wrong", "spa", "spb"}


def test_single_point_pair_rejects_one_sp_for_duplicate_stationary_geometries(
    tmp_path: Path,
) -> None:
    minimum_a = _minimum(tmp_path, "min_a", energy=-100.01, coords=_COORDS_A)
    minimum_b = _minimum(tmp_path, "min_b", energy=-100.00, coords=_COORDS_A)
    single_point = _stage_dir(
        tmp_path,
        "sp",
        route="wB97M-V def2-TZVPP",
        energy=-200.0,
        coords=_COORDS_A,
    )

    data = collect_workflow_si_data(
        _payload(
            [
                _orca_stage("oma", minimum_a, label="min_a"),
                _orca_stage("omb", minimum_b, label="min_b"),
                _orca_stage("sp", single_point, label="sp"),
            ]
        )
    )

    assert all(entry.sp_block is None for entry in data.entries)
    assert [entry.block.name for entry in data.extra_blocks] == ["sp"]


def test_si_csv_populated_row_carries_population_values(tmp_path: Path) -> None:
    lo = _minimum(tmp_path, "conf_lo", energy=-100.010, coords=_COORDS_A)
    hi = _minimum(tmp_path, "conf_hi", energy=-100.000, coords=_COORDS_B)
    data = collect_workflow_si_data(
        _payload([_orca_stage("olo", lo, label="conf_lo"), _orca_stage("ohi", hi, label="conf_hi")])
    )

    rows = list(csv.DictReader(render_workflow_si_csv(data).splitlines()))
    lo_row = next(row for row in rows if row["name"] == "conf_lo")

    assert lo_row["cluster_key"].endswith("|0|1")
    assert float(lo_row["boltzmann_T_K"]) == pytest.approx(298.15)
    assert float(lo_row["rel_G_kcalmol"]) == pytest.approx(0.0)  # conf_lo is the min-G member
    # The column is a fraction in [0, 1], not a percentage.
    assert float(lo_row["boltzmann_population"]) > 0.99
    total = sum(float(row["boltzmann_population"]) for row in rows if row["boltzmann_population"])
    assert total == pytest.approx(1.0)


def test_boltzmann_omitted_when_parsed_temperatures_disagree(tmp_path: Path) -> None:
    a = _minimum(tmp_path, "a", energy=-100.010, coords=_COORDS_A, temp=298.15)
    b = _minimum(tmp_path, "b", energy=-100.000, coords=_COORDS_B, temp=298.60)
    data = collect_workflow_si_data(
        _payload([_orca_stage("oa", a, label="a"), _orca_stage("ob", b, label="b")])
    )

    assert all(row is None for row in data.populations)
    assert data.boltzmann_temperature_k is None
    rendered = render_workflow_si_md(data)
    assert "thermochemistry temperatures disagree" in rendered
    assert "one frequency temperature" in rendered  # the note promises no override rescue


def test_boltzmann_omits_incomplete_minimum_set(tmp_path: Path) -> None:
    with_freq = _minimum(tmp_path, "with_freq", energy=-100.010, coords=_COORDS_A)
    # Same species (CH), but Opt-only → a minimum with no Gibbs energy.
    bare_min = _stage_dir(
        tmp_path, "no_freq", route="B3LYP def2-SVP Opt", energy=-100.0, coords=_COORDS_B
    )
    data = collect_workflow_si_data(
        _payload(
            [
                _orca_stage("of", with_freq, label="with_freq"),
                _orca_stage("onf", bare_min, label="no_freq"),
            ]
        )
    )

    pops = {entry.block.name: data.populations[i] for i, entry in enumerate(data.entries)}
    assert pops == {"with_freq": None, "no_freq": None}
    rendered = render_workflow_si_md(data)
    assert "1 of 2 route-classified minima are usable" in rendered
    population_section = rendered.split("## Boltzmann populations", 1)[1].split("## Structures", 1)[
        0
    ]
    assert "100.00" not in population_section


def test_boltzmann_override_ignores_minimum_without_parsed_temperature(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # A minimum with a Gibbs energy but no parsed THERMOCHEMISTRY-AT temperature.
    no_temp = _stage_dir(
        tmp_path,
        "no_temp",
        route="B3LYP def2-SVP Opt Freq",
        energy=-100.0,
        coords=_COORDS_A,
        freqs=_MIN_FREQS,
        thermo=True,
        thermo_header=False,
    )
    payload = _payload(
        [_orca_stage("ont", no_temp, label="no_temp")],
        boltzmann_temperature_k=298.15,
    )

    write_workflow_si(workspace, payload)

    csv_rows = list(
        csv.DictReader((workspace / WORKFLOW_SI_CSV_FILE).read_text(encoding="utf-8").splitlines())
    )
    # An unverified temperature must not be Boltzmann-weighted at the override value.
    assert csv_rows[0]["boltzmann_population"] == ""


# ---------------------------------------------------------------------------
# Interaction energy (ΔE_int) and RMSD re-dedup (feature 2)
# ---------------------------------------------------------------------------

_SP_ROUTE = "r2scan-3c TightSCF"
_OPT_ROUTE = "B3LYP def2-SVP Opt"
_IE_COORDS = (("C", 0.0, 0.0, 0.0), ("O", 1.2, 0.0, 0.0))


def _interaction_stage(
    stage_id: str,
    stage_dir: Path,
    *,
    role: str,
    parent: str,
    status: str = "completed",
    fragment_index: int | None = None,
    fragment_label: str = "",
    fragment_charge: int = 0,
    fragment_multiplicity: int = 1,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "selected_input_label": stage_id,
        "role": role,
        "parent_stage_id": parent,
    }
    if fragment_index is not None:
        metadata.update(
            {
                "fragment_index": fragment_index,
                "fragment_label": fragment_label,
                "fragment_charge": fragment_charge,
                "fragment_multiplicity": fragment_multiplicity,
            }
        )
    return {
        "stage_id": stage_id,
        "stage_kind": "orca_stage",
        "status": status,
        "metadata": metadata,
        "output_artifacts": [{"kind": "orca_output_dir", "path": str(stage_dir)}],
    }


def _params_payload(
    stages: list[dict[str, Any]],
    *,
    interaction_energy: dict[str, Any] | None = None,
    rmsd_dedup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workflow_id": "wf_si_test",
        "template_name": "conformer_screening",
        "status": "completed",
        "reaction_key": "input",
        "stages": stages,
    }
    parameters: dict[str, Any] = {}
    if interaction_energy is not None:
        parameters["interaction_energy"] = interaction_energy
    if rmsd_dedup is not None:
        parameters["rmsd_dedup"] = rmsd_dedup
    if parameters:
        payload["metadata"] = {"request": {"parameters": parameters}}
    return payload


def _interaction_payload(tmp_path: Path, *, fragment_b_completed: bool = True) -> dict[str, Any]:
    interaction_energy: dict[str, Any] = {
        "enabled": True,
        "sp_route_line": f"! {_SP_ROUTE}",
        "max_fragments": 2,
        "fragments": [
            {"atom_indices": [0], "charge": 0, "multiplicity": 1, "label": "host"},
            {"atom_indices": [1], "charge": 0, "multiplicity": 1, "label": "guest"},
        ],
    }
    fingerprint = interaction_energy_config_fingerprint(
        interaction_energy, complex_charge=0, complex_multiplicity=1
    )
    complex_opt = _stage_dir(tmp_path, "cx_opt", route=_OPT_ROUTE, energy=-100.0, coords=_IE_COORDS)
    complex_sp = _stage_dir(tmp_path, "cx_sp", route=_SP_ROUTE, energy=-100.0, coords=_IE_COORDS)
    frag_a = _stage_dir(tmp_path, "frag_a", route=_SP_ROUTE, energy=-60.0, coords=(_IE_COORDS[0],))
    stages = [
        _orca_stage("orca_conf_01", complex_opt, label="conf1"),
        _interaction_stage(
            "ie_complex", complex_sp, role="interaction_complex_sp", parent="orca_conf_01"
        ),
        _interaction_stage(
            "ie_f0",
            frag_a,
            role="interaction_fragment",
            parent="orca_conf_01",
            fragment_index=0,
            fragment_label="host",
        ),
    ]
    if fragment_b_completed:
        frag_b = _stage_dir(
            tmp_path, "frag_b", route=_SP_ROUTE, energy=-39.99, coords=(_IE_COORDS[1],)
        )
        frag_b_dir = frag_b
    else:
        frag_b_dir = tmp_path / "frag_b_missing"  # no state → fail-closed
        frag_b_dir.mkdir()
    stages.append(
        _interaction_stage(
            "ie_f1",
            frag_b_dir,
            role="interaction_fragment",
            parent="orca_conf_01",
            status="completed" if fragment_b_completed else "running",
            fragment_index=1,
            fragment_label="guest",
        )
    )
    for stage in stages:
        metadata = stage.get("metadata", {})
        if str(metadata.get("role", "")).startswith("interaction_"):
            metadata["interaction_config_fingerprint"] = fingerprint
        fragment_index = metadata.get("fragment_index")
        if isinstance(fragment_index, int):
            metadata["fragment_atom_indices"] = interaction_energy["fragments"][fragment_index][
                "atom_indices"
            ]
    return _params_payload(stages, interaction_energy=interaction_energy)


def test_interaction_stages_never_leak_into_the_structure_path(tmp_path: Path) -> None:
    data = collect_workflow_si_data(_interaction_payload(tmp_path))
    structure_ids = {entry.stage_id for entry in (*data.entries, *data.extra_blocks)}
    assert structure_ids == {"orca_conf_01"}
    assert "ie_complex" not in structure_ids
    assert "ie_f0" not in structure_ids and "ie_f1" not in structure_ids
    # And never rendered as an SI structure row.
    csv_text = render_workflow_si_csv(data)
    rows = list(csv.DictReader(csv_text.splitlines()))
    assert {row["stage_id"] for row in rows} == {"orca_conf_01"}


def test_interaction_energy_is_computed_and_rendered(tmp_path: Path) -> None:
    data = collect_workflow_si_data(_interaction_payload(tmp_path))
    assert len(data.interaction_energies) == 1
    result = data.interaction_energies[0]
    assert result.resolved
    assert result.de_int_hartree is not None
    assert abs(result.de_int_hartree - (-100.0 - (-60.0 - 39.99))) < 1e-9

    md = render_workflow_si_md(data)
    assert "## Interaction energies" in md
    assert "conf1 (orca_conf_01)" in md

    interaction_csv = render_interaction_energy_csv(data)
    assert interaction_csv is not None
    ie_rows = list(csv.DictReader(interaction_csv.splitlines()))
    assert {row["fragment_label"] for row in ie_rows} == {"host", "guest"}
    assert all(row["parent_stage_id"] == "orca_conf_01" for row in ie_rows)
    assert all(row["complex_stage_id"] == "ie_complex" for row in ie_rows)
    assert all(row["ghost_counterpoise_applied"] == "false" for row in ie_rows)
    assert {row["fragment_atom_indices"] for row in ie_rows} == {"0", "1"}
    assert all(row["route_line"] == _SP_ROUTE for row in ie_rows)
    assert "Interaction-energy single points were performed" in md
    assert "No separate Boys–Bernardi ghost-atom counterpoise" in md
    assert "r2SCAN-3c gCP" in md


def test_interaction_energy_fails_closed_on_missing_fragment(tmp_path: Path) -> None:
    data = collect_workflow_si_data(_interaction_payload(tmp_path, fragment_b_completed=False))
    assert len(data.interaction_energies) == 1
    result = data.interaction_energies[0]
    assert not result.resolved
    assert result.de_int_hartree is None
    md = render_workflow_si_md(data)
    assert "ΔE_int omitted" in md


def test_interaction_energy_fails_closed_on_impossible_fragment_electron_state(
    tmp_path: Path,
) -> None:
    payload = _interaction_payload(tmp_path)
    cfg = payload["metadata"]["request"]["parameters"]["interaction_energy"]
    for fragment in cfg["fragments"]:
        fragment["multiplicity"] = 2
    fingerprint = interaction_energy_config_fingerprint(
        cfg, complex_charge=0, complex_multiplicity=1
    )
    for stage in payload["stages"]:
        metadata = stage.get("metadata", {})
        if str(metadata.get("role", "")).startswith("interaction_"):
            metadata["interaction_config_fingerprint"] = fingerprint
        if isinstance(metadata.get("fragment_index"), int):
            metadata["fragment_multiplicity"] = 2

    result = collect_workflow_si_data(payload).interaction_energies[0]

    assert not result.resolved
    assert "wrong parity" in result.note


def test_interaction_energy_fails_closed_when_fragment_stage_is_absent(tmp_path: Path) -> None:
    payload = _interaction_payload(tmp_path)
    payload["stages"] = [stage for stage in payload["stages"] if stage["stage_id"] != "ie_f1"]
    result = collect_workflow_si_data(payload).interaction_energies[0]
    assert not result.resolved
    assert result.de_int_hartree is None
    assert "fragment 1 expected exactly one stage" in result.note


def test_interaction_energy_fails_closed_on_duplicate_fragment_index(tmp_path: Path) -> None:
    payload = _interaction_payload(tmp_path)
    duplicate = dict(next(stage for stage in payload["stages"] if stage["stage_id"] == "ie_f0"))
    duplicate["stage_id"] = "ie_f0_duplicate"
    duplicate["metadata"] = dict(duplicate["metadata"])
    payload["stages"].append(duplicate)
    result = collect_workflow_si_data(payload).interaction_energies[0]
    assert not result.resolved
    assert "fragment 0 expected exactly one stage, found 2" in result.note


def test_running_interaction_stage_never_reads_stale_completed_output(tmp_path: Path) -> None:
    payload = _interaction_payload(tmp_path)
    fragment = next(stage for stage in payload["stages"] if stage["stage_id"] == "ie_f1")
    fragment["status"] = "running"
    fragment["metadata"]["reaction_dir"] = fragment["output_artifacts"][0]["path"]
    fragment["output_artifacts"] = []
    result = collect_workflow_si_data(payload).interaction_energies[0]
    assert not result.resolved
    assert "stage status is running" in result.note


def test_disabled_interaction_config_ignores_persisted_stages(tmp_path: Path) -> None:
    payload = _interaction_payload(tmp_path)
    payload["metadata"]["request"]["parameters"].pop("interaction_energy")
    data = collect_workflow_si_data(payload)
    assert data.interaction_energies == ()
    assert not data.interaction_energy_enabled
    assert "## Interaction energies" not in render_workflow_si_md(data)


def test_interaction_energy_rejects_mixed_executed_routes(tmp_path: Path) -> None:
    payload = _interaction_payload(tmp_path)
    fragment = next(stage for stage in payload["stages"] if stage["stage_id"] == "ie_f1")
    stage_dir = Path(fragment["output_artifacts"][0]["path"])
    out_path = stage_dir / "job.out"
    out_path.write_text(
        out_path.read_text(encoding="utf-8").replace(_SP_ROUTE, "HF STO-3G"),
        encoding="utf-8",
    )
    inp_path = stage_dir / "job.inp"
    inp_path.write_text(
        inp_path.read_text(encoding="utf-8").replace(_SP_ROUTE, "HF STO-3G"),
        encoding="utf-8",
    )
    result = collect_workflow_si_data(payload).interaction_energies[0]
    assert not result.resolved
    assert "levels differ" in result.note


def test_interaction_energy_rejects_selected_input_output_route_mismatch(
    tmp_path: Path,
) -> None:
    payload = _interaction_payload(tmp_path)
    fragment = next(stage for stage in payload["stages"] if stage["stage_id"] == "ie_f1")
    stage_dir = Path(fragment["output_artifacts"][0]["path"])
    inp_path = stage_dir / "job.inp"
    inp_path.write_text(
        inp_path.read_text(encoding="utf-8").replace(_SP_ROUTE, "HF STO-3G"),
        encoding="utf-8",
    )
    result = collect_workflow_si_data(payload).interaction_energies[0]
    assert not result.resolved
    assert "selected-input route/electronic state" in result.note


def test_enabled_interaction_energy_reports_whole_missing_fanout(tmp_path: Path) -> None:
    payload = _interaction_payload(tmp_path)
    payload["stages"] = [
        stage
        for stage in payload["stages"]
        if not str(stage.get("metadata", {}).get("role", "")).startswith("interaction_")
    ]
    data = collect_workflow_si_data(payload)
    assert len(data.interaction_energies) == 1
    result = data.interaction_energies[0]
    assert not result.resolved
    assert "expected exactly one complex single point, found 0" in result.note
    assert "fragment 0 expected exactly one stage, found 0" in result.note


def test_hidden_default_rmsd_grouping_does_not_report_intentional_nonrepresentative(
    tmp_path: Path,
) -> None:
    payload = _interaction_payload(tmp_path)
    duplicate = _stage_dir(
        tmp_path,
        "duplicate_opt",
        route=_OPT_ROUTE,
        energy=-99.99995,
        coords=_IE_COORDS,
    )
    payload["stages"].append(_orca_stage("orca_conf_02", duplicate, label="conf2"))
    data = collect_workflow_si_data(payload)
    assert {entry.stage_id for entry in data.entries} == {"orca_conf_01", "orca_conf_02"}
    assert [result.parent_stage_id for result in data.interaction_energies] == ["orca_conf_01"]


def test_write_workflow_si_emits_interaction_csv(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    write_workflow_si(workspace, _interaction_payload(tmp_path))
    assert (workspace / INTERACTION_ENERGY_CSV_FILE).is_file()
    assert (workspace / INTERACTION_ENERGY_CSV_OWNER_FILE).is_file()


def test_feature_off_preserves_unowned_interaction_csv(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sentinel = workspace / INTERACTION_ENERGY_CSV_FILE
    sentinel.write_text("USER RESEARCH DATA\n", encoding="utf-8")
    stage = _stage_dir(tmp_path, "conf", route=_OPT_ROUTE, energy=-100.0, coords=_COORDS_A)
    write_workflow_si(workspace, _payload([_orca_stage("conf", stage)]))
    assert sentinel.read_text(encoding="utf-8") == "USER RESEARCH DATA\n"


def test_unowned_interaction_conflict_preserves_last_good_base_si(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    base_stage = _stage_dir(
        tmp_path, "base_conf", route=_OPT_ROUTE, energy=-80.0, coords=_IE_COORDS
    )
    write_workflow_si(
        workspace,
        _payload([_orca_stage("base_conf", base_stage)]),
        raise_on_error=True,
    )
    md_path = workspace / WORKFLOW_SI_MD_FILE
    csv_path = workspace / WORKFLOW_SI_CSV_FILE
    before_md = md_path.read_bytes()
    before_csv = csv_path.read_bytes()
    sentinel = workspace / INTERACTION_ENERGY_CSV_FILE
    sentinel.write_text("USER RESEARCH DATA\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to overwrite unowned"):
        write_workflow_si(workspace, _interaction_payload(tmp_path), raise_on_error=True)

    assert md_path.read_bytes() == before_md
    assert csv_path.read_bytes() == before_csv
    assert sentinel.read_text(encoding="utf-8") == "USER RESEARCH DATA\n"


def test_enabled_feature_refuses_to_overwrite_unowned_interaction_csv(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sentinel = workspace / INTERACTION_ENERGY_CSV_FILE
    sentinel.write_text("USER RESEARCH DATA\n", encoding="utf-8")
    assert write_workflow_si(workspace, _interaction_payload(tmp_path)) is None
    assert sentinel.read_text(encoding="utf-8") == "USER RESEARCH DATA\n"
    assert not (workspace / INTERACTION_ENERGY_CSV_OWNER_FILE).exists()


def test_disabling_feature_removes_only_owned_interaction_csv(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = _interaction_payload(tmp_path)
    write_workflow_si(workspace, payload)
    payload["metadata"]["request"]["parameters"].pop("interaction_energy")
    write_workflow_si(workspace, payload)
    assert not (workspace / INTERACTION_ENERGY_CSV_FILE).exists()
    assert not (workspace / INTERACTION_ENERGY_CSV_OWNER_FILE).exists()


def test_interaction_csv_neutralizes_formula_leading_labels(tmp_path: Path) -> None:
    data = collect_workflow_si_data(_interaction_payload(tmp_path))
    first = data.interaction_energies[0].fragments[0]
    hardened = replace(
        data,
        interaction_energies=(
            replace(
                data.interaction_energies[0],
                fragments=(replace(first, label='=HYPERLINK("https://invalid")'),),
            ),
        ),
    )
    csv_text = render_interaction_energy_csv(hardened)
    assert csv_text is not None
    rows = list(csv.DictReader(csv_text.splitlines()))
    assert rows[0]["fragment_label"].startswith("'=")


def test_interaction_csv_neutralizes_every_durable_text_field(tmp_path: Path) -> None:
    data = collect_workflow_si_data(_interaction_payload(tmp_path))
    result = data.interaction_energies[0]
    fragment = result.fragments[0]
    formula = '=HYPERLINK("https://invalid")'
    hardened = replace(
        data,
        interaction_energies=(
            replace(
                result,
                parent_stage_id=formula,
                complex_stage_id=formula,
                complex_label=formula,
                complex_formula=formula,
                method=formula,
                basis_set=formula,
                solvation=formula,
                orca_version=formula,
                input_line=formula,
                note=formula,
                fragments=(replace(fragment, label=formula, stage_id=formula, formula=formula),),
            ),
        ),
    )
    csv_text = render_interaction_energy_csv(hardened)
    assert csv_text is not None
    row = next(csv.DictReader(csv_text.splitlines()))
    for column in (
        "parent_stage_id",
        "complex_stage_id",
        "complex_label",
        "complex_formula",
        "method",
        "basis_set",
        "solvation",
        "orca_version",
        "route_line",
        "fragment_label",
        "fragment_stage_id",
        "fragment_formula",
        "note",
    ):
        assert row[column].startswith("'=")


def test_modified_or_marker_only_interaction_csv_is_never_overwritten(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = _interaction_payload(tmp_path)
    write_workflow_si(workspace, payload)
    interaction_path = workspace / INTERACTION_ENERGY_CSV_FILE
    owner_path = workspace / INTERACTION_ENERGY_CSV_OWNER_FILE

    interaction_path.write_text("USER MODIFIED DATA\n", encoding="utf-8")
    assert write_workflow_si(workspace, payload) is None
    assert interaction_path.read_text(encoding="utf-8") == "USER MODIFIED DATA\n"
    assert not owner_path.exists()

    interaction_path.unlink()
    write_workflow_si(workspace, payload)
    interaction_path.unlink()
    assert write_workflow_si(workspace, payload, raise_on_error=True) is not None
    assert interaction_path.exists()
    interaction_path.unlink()
    interaction_path.write_text("USER NEW DATA\n", encoding="utf-8")
    assert write_workflow_si(workspace, payload) is None
    assert interaction_path.read_text(encoding="utf-8") == "USER NEW DATA\n"


def test_interaction_owner_pending_digest_recovers_after_marker_finalize_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import orca_auto.flow.workflow.si as si_mod

    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = _interaction_payload(tmp_path)
    owner_path = workspace / INTERACTION_ENERGY_CSV_OWNER_FILE
    real_write = si_mod.atomic_write_text
    owner_writes = 0

    def crash_final_marker(path: Path, text: str) -> None:
        nonlocal owner_writes
        if path == owner_path:
            owner_writes += 1
            if owner_writes == 2:
                raise KeyboardInterrupt("simulated process crash")
        real_write(path, text)

    monkeypatch.setattr(si_mod, "atomic_write_text", crash_final_marker)
    with pytest.raises(KeyboardInterrupt):
        si_mod.write_workflow_si(workspace, payload, raise_on_error=True)
    monkeypatch.setattr(si_mod, "atomic_write_text", real_write)

    assert si_mod.write_workflow_si(workspace, payload, raise_on_error=True) is not None
    assert si_mod._owned_interaction_artifact(
        workspace / INTERACTION_ENERGY_CSV_FILE,
        owner_path,
        workflow_id="wf_si_test",
    )


def test_invalid_durable_interaction_config_preserves_last_good_artifacts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = _interaction_payload(tmp_path)
    write_workflow_si(workspace, payload, raise_on_error=True)
    interaction_path = workspace / INTERACTION_ENERGY_CSV_FILE
    before = interaction_path.read_bytes()
    payload["metadata"]["request"]["parameters"]["interaction_energy"]["typo"] = True
    with pytest.raises(ValueError, match="unknown key"):
        write_workflow_si(workspace, payload, raise_on_error=True)
    assert interaction_path.read_bytes() == before


def test_unsupported_template_feature_preserves_last_good_artifacts(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = _interaction_payload(tmp_path)
    write_workflow_si(workspace, payload, raise_on_error=True)
    interaction_path = workspace / INTERACTION_ENERGY_CSV_FILE
    before = interaction_path.read_bytes()
    payload["template_name"] = "reaction_ts_search"

    with pytest.raises(ValueError, match="supported only for conformer_screening"):
        write_workflow_si(workspace, payload, raise_on_error=True)

    assert interaction_path.read_bytes() == before


def test_corrupt_interaction_stage_metadata_preserves_last_good_artifacts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = _interaction_payload(tmp_path)
    write_workflow_si(workspace, payload, raise_on_error=True)
    interaction_path = workspace / INTERACTION_ENERGY_CSV_FILE
    before = interaction_path.read_bytes()
    fragment = next(stage for stage in payload["stages"] if stage["stage_id"] == "ie_f0")
    fragment["metadata"]["fragment_atom_indices"] = 0

    with pytest.raises(TypeError):
        write_workflow_si(workspace, payload, raise_on_error=True)

    assert interaction_path.read_bytes() == before


def test_lost_parent_removes_owned_stale_interaction_csv(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = _interaction_payload(tmp_path)
    write_workflow_si(workspace, payload, raise_on_error=True)
    payload["stages"] = [
        stage for stage in payload["stages"] if stage["stage_id"] != "orca_conf_01"
    ]
    assert write_workflow_si(workspace, payload, raise_on_error=True) is None
    assert not (workspace / INTERACTION_ENERGY_CSV_FILE).exists()
    assert not (workspace / INTERACTION_ENERGY_CSV_OWNER_FILE).exists()


def test_strict_no_orca_cleanup_propagates_unlink_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    md_path = workspace / WORKFLOW_SI_MD_FILE
    md_path.write_text("stale\n", encoding="utf-8")
    real_unlink = Path.unlink

    def fail_md_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == md_path:
            raise PermissionError("simulated cleanup denial")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_md_unlink)
    with pytest.raises(PermissionError, match="cleanup denial"):
        write_workflow_si(
            workspace,
            {"workflow_id": "wf_empty", "template_name": "conformer_screening", "stages": []},
            raise_on_error=True,
        )


def test_rmsd_dedup_collapses_degenerate_minima(tmp_path: Path) -> None:
    keep = _stage_dir(tmp_path, "keep", route=_OPT_ROUTE, energy=-50.00005, coords=_COORDS_C)
    drop = _stage_dir(tmp_path, "drop", route=_OPT_ROUTE, energy=-50.0, coords=_COORDS_C)
    payload = _params_payload(
        [
            _orca_stage("orca_conf_keep", keep, label="keep"),
            _orca_stage("orca_conf_drop", drop, label="drop"),
        ],
        rmsd_dedup={
            "enabled": True,
            "rmsd_threshold_angstrom": 0.25,
            "energy_window_kcal": 1.0,
            "heavy_atoms_only": True,
        },
    )
    data = collect_workflow_si_data(payload)
    # Both are identical NO geometries within 1 kcal/mol → one representative kept.
    assert [entry.block.name for entry in data.entries] == ["keep"]
    assert data.rmsd_dedup_enabled
    csv_text = render_workflow_si_csv(data)
    header = csv_text.splitlines()[0].split(",")
    assert header[-3:] == ["rmsd_group", "degeneracy", "merged_stage_ids"]
    row = next(csv.DictReader(csv_text.splitlines()))
    assert row["degeneracy"] == "2"
    assert row["merged_stage_ids"] == "orca_conf_drop"
    assert "RMSD representatives" in render_workflow_si_md(data)


def test_rmsd_dedup_cannot_hide_an_unusable_population_member(tmp_path: Path) -> None:
    usable = _minimum(tmp_path, "usable", energy=-50.00005, coords=_COORDS_C)
    unusable = _stage_dir(
        tmp_path,
        "unusable",
        route="B3LYP def2-SVP Opt Freq",
        energy=-50.0,
        coords=_COORDS_C,
    )
    payload = _params_payload(
        [
            _orca_stage("orca_conf_usable", usable, label="usable"),
            _orca_stage("orca_conf_unusable", unusable, label="unusable"),
        ],
        rmsd_dedup={
            "enabled": True,
            "rmsd_threshold_angstrom": 0.25,
            "energy_window_kcal": 1.0,
            "heavy_atoms_only": False,
        },
    )
    data = collect_workflow_si_data(payload)
    assert [entry.block.name for entry in data.entries] == ["usable"]
    assert data.populations == (None,)
    assert "1 of 2 route-classified minima are usable" in data.population_note


def test_rmsd_dedup_never_uses_unconverged_or_known_saddle_as_representative(
    tmp_path: Path,
) -> None:
    good = _stage_dir(
        tmp_path,
        "good",
        route="B3LYP def2-SVP Opt Freq",
        energy=-50.0,
        coords=_COORDS_C,
        freqs=_MIN_FREQS,
    )
    unknown = _stage_dir(
        tmp_path,
        "unknown",
        route="B3LYP def2-SVP Opt Freq",
        energy=-50.00005,
        coords=_COORDS_C,
        freqs=_MIN_FREQS,
        opt_converged=None,
    )
    saddle = _stage_dir(
        tmp_path,
        "saddle",
        route="B3LYP def2-SVP Opt Freq",
        energy=-50.00006,
        coords=_COORDS_C,
        freqs=_ONE_IMAG_FREQS,
    )
    payload = _params_payload(
        [
            _orca_stage("good", good),
            _orca_stage("unknown", unknown),
            _orca_stage("saddle", saddle),
        ],
        rmsd_dedup={
            "enabled": True,
            "rmsd_threshold_angstrom": 0.25,
            "energy_window_kcal": 1.0,
            "heavy_atoms_only": False,
        },
    )
    data = collect_workflow_si_data(payload)
    assert {entry.stage_id for entry in data.entries} == {"good", "unknown", "saddle"}
    assert all(group.degeneracy == 1 for group in data.rmsd_groups)


def test_single_minimum_rmsd_metadata_reports_singleton_group(tmp_path: Path) -> None:
    only = _stage_dir(
        tmp_path,
        "only",
        route=_OPT_ROUTE,
        energy=-50.0,
        coords=_COORDS_C,
    )
    data = collect_workflow_si_data(
        _params_payload(
            [_orca_stage("only", only)],
            rmsd_dedup={"enabled": True},
        )
    )
    row = next(csv.DictReader(render_workflow_si_csv(data).splitlines()))
    assert row["rmsd_group"] == "1"
    assert row["degeneracy"] == "1"


def test_rmsd_dedup_uses_the_uniform_single_point_energy_convention(tmp_path: Path) -> None:
    opt_a = _stage_dir(tmp_path, "opt_a", route=_OPT_ROUTE, energy=-50.00005, coords=_COORDS_C)
    opt_b = _stage_dir(tmp_path, "opt_b", route=_OPT_ROUTE, energy=-50.0, coords=_COORDS_D)
    sp_a = _stage_dir(tmp_path, "sp_a", route=_SP_ROUTE, energy=-100.0, coords=_COORDS_C)
    sp_b = _stage_dir(tmp_path, "sp_b", route=_SP_ROUTE, energy=-100.00005, coords=_COORDS_D)
    payload = _params_payload(
        [
            _orca_stage("opt_a", opt_a, label="opt_a"),
            _orca_stage("opt_b", opt_b, label="opt_b"),
            _orca_stage("sp_a", sp_a, label="sp_a"),
            _orca_stage("sp_b", sp_b, label="sp_b"),
        ],
        rmsd_dedup={
            "enabled": True,
            "rmsd_threshold_angstrom": 0.25,
            "energy_window_kcal": 0.1,
            "heavy_atoms_only": False,
        },
    )
    data = collect_workflow_si_data(payload)
    assert [entry.block.name for entry in data.entries] == ["opt_b"]
    assert data.entries[0].sp_energy == pytest.approx(-100.00005)


@pytest.mark.parametrize(
    ("winner", "refine_a_energy", "refine_b_energy"),
    [
        ("parent_a", -100.00005, -100.0),
        ("parent_b", -100.0, -100.00005),
    ],
)
def test_interaction_parent_grouping_ignores_known_saddle_in_sp_convention(
    tmp_path: Path,
    winner: str,
    refine_a_energy: float,
    refine_b_energy: float,
) -> None:
    coords_a = (("C", 0.0, 0.0, 0.0), ("O", 1.10, 0.0, 0.0))
    coords_b = (("C", 0.0, 0.0, 0.0), ("O", 1.15, 0.0, 0.0))
    coords_saddle = coords_a
    opt_a = _stage_dir(tmp_path, "parent_a", route=_OPT_ROUTE, energy=-50.00005, coords=coords_a)
    opt_b = _stage_dir(tmp_path, "parent_b", route=_OPT_ROUTE, energy=-50.0, coords=coords_b)
    saddle = _stage_dir(
        tmp_path,
        "known_saddle",
        route=f"{_OPT_ROUTE} Freq",
        energy=-50.1,
        coords=coords_saddle,
        freqs=_ONE_IMAG_FREQS,
    )
    refine_a = _stage_dir(
        tmp_path, "refine_a", route=_SP_ROUTE, energy=refine_a_energy, coords=coords_a
    )
    refine_b = _stage_dir(
        tmp_path, "refine_b", route=_SP_ROUTE, energy=refine_b_energy, coords=coords_b
    )
    winner_coords = coords_a if winner == "parent_a" else coords_b
    winner_energy = refine_a_energy if winner == "parent_a" else refine_b_energy
    ie_complex = _stage_dir(
        tmp_path, f"{winner}_ie", route=_SP_ROUTE, energy=winner_energy, coords=winner_coords
    )
    ie_c = _stage_dir(
        tmp_path, f"{winner}_c", route=_SP_ROUTE, energy=-60.0, coords=(winner_coords[0],)
    )
    ie_o = _stage_dir(
        tmp_path, f"{winner}_o", route=_SP_ROUTE, energy=-39.99, coords=(winner_coords[1],)
    )
    rmsd_cfg = {
        "enabled": True,
        "rmsd_threshold_angstrom": 0.25,
        "energy_window_kcal": 0.1,
        "heavy_atoms_only": False,
    }
    interaction_cfg: dict[str, Any] = {
        "enabled": True,
        "sp_route_line": f"! {_SP_ROUTE}",
        "max_fragments": 2,
        "fragments": [
            {"atom_indices": [0], "charge": 0, "multiplicity": 1, "label": "carbon"},
            {"atom_indices": [1], "charge": 0, "multiplicity": 1, "label": "oxygen"},
        ],
    }
    fingerprint = interaction_energy_config_fingerprint(
        interaction_cfg,
        complex_charge=0,
        complex_multiplicity=1,
        rmsd_dedup=rmsd_cfg,
    )
    interaction_stages = [
        _interaction_stage(
            f"ie_{winner}_complex",
            ie_complex,
            role="interaction_complex_sp",
            parent=winner,
        ),
        _interaction_stage(
            f"ie_{winner}_c",
            ie_c,
            role="interaction_fragment",
            parent=winner,
            fragment_index=0,
            fragment_label="carbon",
        ),
        _interaction_stage(
            f"ie_{winner}_o",
            ie_o,
            role="interaction_fragment",
            parent=winner,
            fragment_index=1,
            fragment_label="oxygen",
        ),
    ]
    for stage in interaction_stages:
        stage["metadata"]["interaction_config_fingerprint"] = fingerprint
        index = stage["metadata"].get("fragment_index")
        if isinstance(index, int):
            stage["metadata"]["fragment_atom_indices"] = interaction_cfg["fragments"][index][
                "atom_indices"
            ]
    payload = _params_payload(
        [
            _orca_stage("parent_a", opt_a),
            _orca_stage("parent_b", opt_b),
            _orca_stage("known_saddle", saddle),
            _orca_stage("refine_a", refine_a),
            _orca_stage("refine_b", refine_b),
            *interaction_stages,
        ],
        interaction_energy=interaction_cfg,
        rmsd_dedup=rmsd_cfg,
    )

    data = collect_workflow_si_data(payload)

    merged = next(group for group in data.rmsd_groups if "parent_a" in group.member_stage_ids)
    assert merged.representative_stage_id == winner
    assert merged.member_stage_ids == ("parent_a", "parent_b")
    representative = next(entry for entry in data.entries if entry.stage_id == winner)
    assert representative.sp_energy == pytest.approx(winner_energy)
    assert representative.sp_label == f"refine_{winner[-1]}"
    assert {entry.stage_id for entry in data.extra_blocks}.isdisjoint({"refine_a", "refine_b"})
    assert [result.parent_stage_id for result in data.interaction_energies] == [winner]
    assert data.interaction_energies[0].resolved


def test_features_off_are_byte_identical_to_baseline(tmp_path: Path) -> None:
    stage = _stage_dir(tmp_path, "conf", route=_OPT_ROUTE, energy=-100.0, coords=_COORDS_A)
    payload = _payload([_orca_stage("orca_conf_01", stage, label="conf1")])
    data = collect_workflow_si_data(payload)

    csv_text = render_workflow_si_csv(data)
    header = csv_text.splitlines()[0].split(",")
    assert header == _CSV_COLUMNS  # no rmsd_dedup columns appended when off

    md = render_workflow_si_md(data)
    assert "## Interaction energies" not in md
    assert "RMSD representatives" not in md
    assert render_interaction_energy_csv(data) is None
