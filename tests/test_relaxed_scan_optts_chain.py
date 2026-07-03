from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from orca_auto.orca.attempt.engine import run_attempts
from orca_auto.orca.retry_policy import retry_policy_for_input
from orca_auto.orca.state import load_state, new_state, save_state
from orca_auto.orca.types import RunState

_TS_FOUND_OUT = "\n".join(
    [
        "VIBRATIONAL FREQUENCIES",
        "  1   -150.00 cm**-1",
        "  2    120.00 cm**-1",
        "****ORCA TERMINATED NORMALLY****",
    ]
)

_TS_NOT_FOUND_OUT = "\n".join(
    [
        "VIBRATIONAL FREQUENCIES",
        "  1    120.00 cm**-1",
        "  2    240.00 cm**-1",
        "****ORCA TERMINATED NORMALLY****",
    ]
)


def _write_relaxed_scan_input(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "! Opt B3LYP def2-SVP D3BJ CPCM(THF)",
                "",
                "%geom",
                "  MaxIter 200",
                "  Scan",
                "    B 4 20 = 1.86, 1.96, 3",
                "  end",
                "end",
                "",
                "* xyzfile 0 1 input.xyz",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_scan_xyz_series(root: Path, stem: str, count: int) -> None:
    for idx in range(1, count + 1):
        (root / f"{stem}.{idx:03d}.xyz").write_text(
            f"2\nscan step {idx}\nH 0 0 0\nH 0 0 {idx}.0\n",
            encoding="utf-8",
        )


def _write_scan_out(path: Path, energies: list[float]) -> None:
    path.write_text(
        "\n".join(
            [
                "**** RELAXED SURFACE SCAN DONE ***",
                "RELAXED SURFACE SCAN RESULTS",
                "The Calculated Surface using the 'Actual Energy'",
                *(
                    f"   {1.86 + 0.05 * idx:.8f} {energy:.8f}"
                    for idx, energy in enumerate(energies)
                ),
                "The Calculated Surface using the SCF energy",
                "   1.86000000 -101.00000000",
                "",
                "****ORCA TERMINATED NORMALLY****",
                "",
            ]
        ),
        encoding="utf-8",
    )


class _ScanWithBarrierRunner:
    """Relaxed scan completes with an interior barrier; the chained OptTS result
    is configurable per test."""

    def __init__(self, *, optts_out: str) -> None:
        self.seen: list[Path] = []
        self._optts_out = optts_out

    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
            _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=3)
            # Interior maximum at point 2 (~157 kcal/mol prominence).
            _write_scan_out(out_path, [-100.0, -99.5, -99.75])
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        out_path.write_text(self._optts_out, encoding="utf-8")
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _MonotonicScanRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=3)
        _write_scan_out(out_path, [-100.0, -100.1, -100.2])
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _FailingScanRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        out_path.write_text(
            "ORCA finished by error termination in Startup\n"
            "[file orca_tools/qcmsg.cpp, line 394]:\n"
            "  .... aborting the run\n",
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


def _retry_inp_path(selected_inp: Path, retry_number: int) -> Path:
    return selected_inp.parent / f"{selected_inp.stem}.retry{retry_number:02d}.inp"


def _run(
    reaction_dir: Path,
    selected_inp: Path,
    *,
    runner: object,
    resumed: bool = False,
    state: RunState | None = None,
) -> tuple[int, RunState]:
    run_state = state if state is not None else new_state(reaction_dir, selected_inp, max_retries=3)
    rc = run_attempts(
        reaction_dir,
        selected_inp,
        run_state,
        resumed=resumed,
        runner=runner,  # type: ignore[arg-type]
        max_retries=3,
        retry_inp_path=_retry_inp_path,
        to_resolved_local=lambda raw: Path(raw),
        emit=lambda _payload: None,
    )
    saved = load_state(reaction_dir)
    assert saved is not None
    return rc, saved


def _final_reason(saved: RunState) -> str:
    final_result = saved.get("final_result")
    assert isinstance(final_result, dict)
    return str(final_result.get("reason"))


def test_retry_policy_classifies_relaxed_scan(tmp_path: Path) -> None:
    scan_inp = tmp_path / "scan.inp"
    _write_relaxed_scan_input(scan_inp)
    policy = retry_policy_for_input(scan_inp)
    assert policy.name == "relaxed_scan"
    assert policy.max_retries == 2

    plain_opt = tmp_path / "opt.inp"
    plain_opt.write_text("! Opt B3LYP def2-SVP\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")
    assert retry_policy_for_input(plain_opt).name == "opt"


def test_completed_scan_with_barrier_chains_optts_and_verifies_ts(tmp_path: Path) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_relaxed_scan_input(selected_inp)
    runner = _ScanWithBarrierRunner(optts_out=_TS_FOUND_OUT)

    rc, saved = _run(tmp_path, selected_inp, runner=runner)
    optts_inp = tmp_path / "rxn.retry01.inp"
    optts_text = optts_inp.read_text(encoding="utf-8")

    assert rc == 0
    assert runner.seen == [selected_inp, optts_inp]
    assert "OptTS" in optts_text
    assert "Freq" in optts_text
    assert "Scan" not in optts_text
    # Chained from the interior maximum (point 2), not the scan endpoint.
    assert "* xyzfile 0 1 rxn.002.xyz" in optts_text
    assert saved.get("status") == "completed"
    assert _final_reason(saved) == "ts_criteria_met"
    attempts = saved["attempts"]
    assert isinstance(attempts, list)
    actions = [str(action) for action in attempts[0]["patch_actions"]]
    assert "relaxed_scan_optts_chain" in actions
    assert "relaxed_scan_route_to_optts" in actions
    assert "relaxed_scan_freq_added" in actions
    assert "relaxed_scan_guess_from_rxn.002.xyz" in actions
    report_html = (tmp_path / "job_report.html").read_text(encoding="utf-8")
    assert "OptTS chain (scan maximum)" in report_html
    assert "Relaxed scan report" in report_html


def test_monotonic_scan_completes_without_chain(tmp_path: Path) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_relaxed_scan_input(selected_inp)
    runner = _MonotonicScanRunner()

    rc, saved = _run(tmp_path, selected_inp, runner=runner)

    assert rc == 0
    assert runner.seen == [selected_inp]
    assert not (tmp_path / "rxn.retry01.inp").exists()
    assert saved.get("status") == "completed"
    assert _final_reason(saved) == "normal_termination"


def test_failed_chained_optts_ends_with_exhausted_reason(tmp_path: Path) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_relaxed_scan_input(selected_inp)
    runner = _ScanWithBarrierRunner(optts_out=_TS_NOT_FOUND_OUT)

    rc, saved = _run(tmp_path, selected_inp, runner=runner)

    assert rc == 1
    assert runner.seen == [selected_inp, tmp_path / "rxn.retry01.inp"]
    assert not (tmp_path / "rxn.retry02.inp").exists()
    assert saved.get("status") == "failed"
    assert _final_reason(saved) == "relaxed_scan_recipes_exhausted"


def test_failed_scan_ends_without_generic_hardening(tmp_path: Path) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_relaxed_scan_input(selected_inp)
    runner = _FailingScanRunner()

    rc, saved = _run(tmp_path, selected_inp, runner=runner)

    assert rc == 1
    assert runner.seen == [selected_inp]
    assert not (tmp_path / "rxn.retry01.inp").exists()
    assert saved.get("status") == "failed"
    assert _final_reason(saved) == "relaxed_scan_recipes_exhausted"


def test_resume_after_completed_scan_continues_to_optts(tmp_path: Path) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_relaxed_scan_input(selected_inp)
    first_runner = _ScanWithBarrierRunner(optts_out=_TS_FOUND_OUT)
    rc, _saved = _run(tmp_path, selected_inp, runner=first_runner)
    assert rc == 0

    # Simulate a crash after the completed scan attempt was recorded but
    # before the OptTS chain input was written.
    state = load_state(tmp_path)
    assert state is not None
    attempts = state["attempts"]
    assert isinstance(attempts, list)
    del attempts[1:]
    attempts[0]["patch_actions"] = []
    state["status"] = "retrying"
    state["final_result"] = None
    save_state(tmp_path, state)
    (tmp_path / "rxn.retry01.inp").unlink()
    (tmp_path / "rxn.retry01.out").unlink()

    resume_runner = _ScanWithBarrierRunner(optts_out=_TS_FOUND_OUT)
    rc, saved = _run(tmp_path, selected_inp, runner=resume_runner, resumed=True, state=state)

    # Resume must not finish the run off the intermediate scan; recovery reruns
    # the scan copy, then the chain produces the OptTS attempt.
    assert rc == 0
    assert saved.get("status") == "completed"
    assert _final_reason(saved) == "ts_criteria_met"
    optts_inp = tmp_path / "rxn.retry02.inp"
    assert resume_runner.seen == [tmp_path / "rxn.retry01.inp", optts_inp]
    assert "OptTS" in optts_inp.read_text(encoding="utf-8")
