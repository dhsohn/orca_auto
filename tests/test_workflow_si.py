"""Workflow SI assembly (workflow_si.md + si_data.csv) tests."""

from __future__ import annotations

import csv
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
# A second chemical species (formula NO, not CH) for per-cluster tests.
_COORDS_C = (
    ("N", 0.0, 0.0, 0.0),
    ("O", 1.100000, 0.0, 0.0),
)
_COORDS_D = (
    ("N", 0.0, 0.0, 0.0),
    ("O", 1.150000, 0.0, 0.0),
)


def _out_text(
    *,
    route: str,
    energy: float,
    coords: tuple[tuple[str, float, float, float], ...],
    freqs: tuple[float, ...] = (),
    thermo: bool = False,
    temp: float = 298.15,
    thermo_header: bool = True,
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
) -> Path:
    stage_dir = root / name
    stage_dir.mkdir(parents=True)
    inp = stage_dir / "job.inp"
    inp.write_text(f"! {route}\n* xyz 0 1\nC 0 0 0\n*\n", encoding="utf-8")
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
        freqs=(30.0, 120.0),
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


def test_boltzmann_excludes_transition_states(tmp_path: Path) -> None:
    minimum = _minimum(tmp_path, "min", energy=-100.0, coords=_COORDS_A)
    ts = _stage_dir(
        tmp_path,
        "ts",
        route="B3LYP def2-SVP OptTS Freq",
        energy=-99.9,
        coords=_COORDS_B,
        freqs=(-500.0, 120.0),
        thermo=True,
    )
    payload = _payload(
        [_orca_stage("orca_min", minimum, label="min"), _orca_stage("orca_ts", ts, label="ts")]
    )

    data = collect_workflow_si_data(payload)

    kinds = {entry.block.name: entry.block.kind for entry in data.entries}
    assert kinds == {"min": "min", "ts": "ts"}
    by_name = {entry.block.name: data.populations[i] for i, entry in enumerate(data.entries)}
    assert by_name["ts"] is None  # a saddle point is never Boltzmann-populated
    min_row = by_name["min"]
    assert min_row is not None
    assert min_row.population == pytest.approx(1.0)  # the only minimum


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
    assert "add a frequency calculation" in rendered  # ...a fail-closed note, not numbers

    rows = list(csv.reader(render_workflow_si_csv(data).splitlines()))
    assert rows[1][-5:] == ["", "", "", "", ""]  # blank population cells


def test_boltzmann_temperature_override_and_disagreement(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    minimum = _minimum(tmp_path, "conf", energy=-100.0, coords=_COORDS_A)  # freq T = 298.15
    payload = _payload([_orca_stage("orca_conf", minimum, label="conf")])

    (workspace / "flow.yaml").write_text(
        "workflow_type: conformer_screening\nboltzmann_temperature_k: 298.15\n", encoding="utf-8"
    )
    write_workflow_si(workspace, payload)
    md = (workspace / WORKFLOW_SI_MD_FILE).read_text(encoding="utf-8")
    assert "manifest boltzmann_temperature_k" in md
    assert "298.15 K" in md

    # An override at a temperature the freq job did not use must fail closed.
    (workspace / "flow.yaml").write_text("boltzmann_temperature_k: 353.15\n", encoding="utf-8")
    write_workflow_si(workspace, payload)
    md2 = (workspace / WORKFLOW_SI_MD_FILE).read_text(encoding="utf-8")
    assert "disagrees with the thermochemistry temperature" in md2


def test_malformed_manifest_does_not_suppress_si(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    minimum = _minimum(tmp_path, "conf", energy=-100.0, coords=_COORDS_A)
    payload = _payload([_orca_stage("orca_conf", minimum, label="conf")])
    (workspace / "flow.yaml").write_text("bad: [1, 2\n", encoding="utf-8")  # invalid YAML

    md_path = write_workflow_si(workspace, payload)

    assert md_path is not None and md_path.exists()  # a manifest typo cannot drop the SI
    assert "## Relative energies" in md_path.read_text(encoding="utf-8")


def test_population_failure_still_writes_valid_si(tmp_path: Path, monkeypatch: Any) -> None:
    import orca_auto.flow.workflow.si as si_mod

    minimum = _minimum(tmp_path, "conf", energy=-100.0, coords=_COORDS_A)
    payload = _payload([_orca_stage("orca_conf", minimum, label="conf")])

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("population bug")

    monkeypatch.setattr(si_mod, "_compute_populations", _boom)

    data = si_mod.collect_workflow_si_data(payload)

    assert data.populations == ()  # isolated: the base SI must still render
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
        tmp_path, "sp_lo", route="wB97M-V def2-TZVPP", energy=-200.900, coords=_COORDS_A
    )
    # A different single-point level within the same cluster must NOT be mixed.
    sp_hi = _stage_dir(tmp_path, "sp_hi", route="PBE0 def2-SVP", energy=-200.905, coords=_COORDS_B)
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
    b = _minimum(tmp_path, "b", energy=-100.000, coords=_COORDS_B, temp=300.00)  # 1.85 K apart
    data = collect_workflow_si_data(
        _payload([_orca_stage("oa", a, label="a"), _orca_stage("ob", b, label="b")])
    )

    assert all(row is None for row in data.populations)
    assert data.boltzmann_temperature_k is None
    rendered = render_workflow_si_md(data)
    assert "thermochemistry temperatures disagree" in rendered
    assert "one frequency temperature" in rendered  # the note promises no override rescue


def test_boltzmann_partial_coverage_note(tmp_path: Path) -> None:
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
    freq_row = pops["with_freq"]
    assert freq_row is not None and freq_row.population == pytest.approx(1.0)
    assert pops["no_freq"] is None  # not usable: no Gibbs energy
    assert "populations cover 1 of 2 minima" in render_workflow_si_md(data)


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
        freqs=(30.0, 120.0),
        thermo=True,
        thermo_header=False,
    )
    payload = _payload([_orca_stage("ont", no_temp, label="no_temp")])
    (workspace / "flow.yaml").write_text("boltzmann_temperature_k: 298.15\n", encoding="utf-8")

    write_workflow_si(workspace, payload)

    csv_rows = list(
        csv.DictReader((workspace / WORKFLOW_SI_CSV_FILE).read_text(encoding="utf-8").splitlines())
    )
    # An unverified temperature must not be Boltzmann-weighted at the override value.
    assert csv_rows[0]["boltzmann_population"] == ""
