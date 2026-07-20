from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.orca.attempt.engine import run_attempts
from orca_auto.orca.retry_policy import retry_policy_for_input
from orca_auto.orca.scants import (
    apply_scants_failed_scan_retry_rewrite,
    apply_scants_relaxed_scan_resume_rewrite,
    input_uses_scants,
    prepare_scants_optts_fallback_input,
    prepare_scants_scan_retry_input,
    scan_profile_interior_barrier_kcal,
)
from orca_auto.orca.state import load_state, new_state, save_state
from orca_auto.orca.types import RunState


class _CaptureSuccessRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        out_path.write_text(
            "\n".join(
                [
                    "VIBRATIONAL FREQUENCIES",
                    "  1   -150.00 cm**-1",
                    "  2    120.00 cm**-1",
                    "****ORCA TERMINATED NORMALLY****",
                ]
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _ScanTsReverseDerivedOpttsFailsRunner(_CaptureSuccessRunner):
    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
            _write_surface_scan_failure(inp_path, out_path, xyz_count=32)
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=32)
        _write_surface_scan_done_out(out_path)
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _ScanTsContinuationOpttsFailureReverseRunner(_CaptureSuccessRunner):
    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
            _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=32)
            inp_path.with_suffix(".gbw").write_bytes(b"failed checkpoint")
            out_path.write_text(
                "ORCA finished by error termination in Startup\n"
                "[file orca_tools/qcmsg.cpp, line 394]:\n"
                "  .... aborting the run\n",
                encoding="utf-8",
            )
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        if len(self.seen) == 2:
            _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=6)
            _write_surface_scan_done_out(out_path)
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        if len(self.seen) == 3:
            out_path.write_text(
                "\n".join(
                    [
                        "VIBRATIONAL FREQUENCIES",
                        "  1   -150.00 cm**-1",
                        "  2    120.00 cm**-1",
                        "****ORCA TERMINATED NORMALLY****",
                    ]
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        out_path.write_text(
            "\n".join(
                [
                    "VIBRATIONAL FREQUENCIES",
                    "  1   -150.00 cm**-1",
                    "  2    120.00 cm**-1",
                    "****ORCA TERMINATED NORMALLY****",
                ]
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _ScanTsRefinementCrashOpttsRunner(_CaptureSuccessRunner):
    """Zero-distance crash in ORCA's TS-guess refinement, then OptTS finds the TS."""

    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
            _write_refinement_zero_distance_out(inp_path, out_path)
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        out_path.write_text(
            "\n".join(
                [
                    "VIBRATIONAL FREQUENCIES",
                    "  1   -150.00 cm**-1",
                    "  2    120.00 cm**-1",
                    "****ORCA TERMINATED NORMALLY****",
                ]
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _ScanTsOpttsFallbackFailsThenChainRunner(_CaptureSuccessRunner):
    """Refinement crash, failed OptTS fallback, then the ordinary chain resumes."""

    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
            _write_refinement_zero_distance_out(inp_path, out_path)
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        if len(self.seen) == 2:
            _write_ts_not_found_out(out_path)
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        if len(self.seen) == 3:
            _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=29)
            out_path.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        out_path.write_text(
            "\n".join(
                [
                    "VIBRATIONAL FREQUENCIES",
                    "  1   -150.00 cm**-1",
                    "  2    120.00 cm**-1",
                    "****ORCA TERMINATED NORMALLY****",
                ]
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _ScanTsNoSurfaceFallbackRunner(_CaptureSuccessRunner):
    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
            _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=32)
            inp_path.with_suffix(".gbw").write_bytes(b"failed checkpoint")
            inp_path.with_suffix(".xyz").write_text(
                "2\nfailed geometry\nH 0 0 0\nH 0 0 0\n",
                encoding="utf-8",
            )
            out_path.write_text(
                "ORCA finished by error termination in Startup\n"
                "[file orca_tools/qcmsg.cpp, line 394]:\n"
                "  .... aborting the run\n",
                encoding="utf-8",
            )
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        out_path.write_text(
            "\n".join(
                [
                    "VIBRATIONAL FREQUENCIES",
                    "  1   -150.00 cm**-1",
                    "  2    120.00 cm**-1",
                    "****ORCA TERMINATED NORMALLY****",
                ]
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _ScanTsNoSurfaceNoNumberedRunner(_CaptureSuccessRunner):
    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        inp_path.with_suffix(".gbw").write_bytes(b"failed checkpoint")
        inp_path.with_suffix(".xyz").write_text(
            "2\nfailed same-stem geometry\nH 0 0 0\nH 0 0 0\n",
            encoding="utf-8",
        )
        out_path.write_text(
            "ORCA finished by error termination in Startup\n"
            "[file orca_tools/qcmsg.cpp, line 394]:\n"
            "  .... aborting the run\n",
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _ScanTsTwoFailureRunner(_CaptureSuccessRunner):
    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) <= 2:
            if len(self.seen) == 1:
                _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=32)
            inp_path.with_suffix(".gbw").write_bytes(b"failed checkpoint")
            inp_path.with_suffix(".xyz").write_text(
                "2\nfailed geometry\nH 0 0 0\nH 0 0 0\n",
                encoding="utf-8",
            )
            out_path.write_text(
                "ORCA finished by error termination in Startup\n"
                "[file orca_tools/qcmsg.cpp, line 394]:\n"
                "  .... aborting the run\n",
                encoding="utf-8",
            )
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        out_path.write_text(
            "\n".join(
                [
                    "VIBRATIONAL FREQUENCIES",
                    "  1   -150.00 cm**-1",
                    "  2    120.00 cm**-1",
                    "****ORCA TERMINATED NORMALLY****",
                ]
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


def _retry_inp_path(selected_inp: Path, retry_number: int) -> Path:
    return selected_inp.parent / f"{selected_inp.stem}.retry{retry_number:02d}.inp"


def _write_scan_xyz_series(root: Path, stem: str = "tsopt", count: int = 4) -> None:
    for idx in range(1, count + 1):
        (root / f"{stem}.{idx:03d}.xyz").write_text(
            f"2\nscan step {idx}\nH 0 0 0\nH 0 0 {idx}.0\n",
            encoding="utf-8",
        )


def _write_scants_input(
    path: Path,
    scan_lines: list[str] | None = None,
    *,
    stem_xyz: str = "input.xyz",
) -> None:
    lines = scan_lines or ["    B 4 20 = 1.86, 3.40, 32"]
    path.write_text(
        "\n".join(
            [
                "! ScanTS B3LYP def2-SVP D3BJ CPCM(THF) Freq",
                "",
                "%geom",
                "  MaxIter 200",
                "  Scan",
                *lines,
                "  end",
                "end",
                "",
                f"* xyzfile 0 1 {stem_xyz}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _run_attempt(
    reaction_dir: Path,
    selected_inp: Path,
    *,
    resumed: bool,
    runner: _CaptureSuccessRunner,
    max_retries: int = 1,
) -> tuple[int, RunState]:
    state = new_state(reaction_dir, selected_inp, max_retries=max_retries)
    rc = run_attempts(
        reaction_dir,
        selected_inp,
        state,
        resumed=resumed,
        runner=runner,
        max_retries=max_retries,
        retry_inp_path=_retry_inp_path,
        to_resolved_local=lambda raw: Path(raw),
        emit=lambda _payload: None,
    )
    saved = load_state(reaction_dir)
    assert saved is not None
    return rc, saved


def _write_ts_not_found_out(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "VIBRATIONAL FREQUENCIES",
                "  1    120.00 cm**-1",
                "  2    240.00 cm**-1",
                "****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )


def _write_surface_scan_failure(inp_path: Path, out_path: Path, *, xyz_count: int = 3) -> None:
    root = inp_path.parent
    _write_scan_xyz_series(root, inp_path.stem, count=xyz_count)
    inp_path.with_suffix(".xyz").write_text(
        "2\ninvalid refined guess\nH 0 0 0\nH 0 0 0\n",
        encoding="utf-8",
    )
    _write_surface_scan_done_out(out_path)
    out_path.write_text(
        out_path.read_text(encoding="utf-8")
        + "ORCA finished by error termination in Startup\n"
        + "[file orca_tools/qcmsg.cpp, line 394]:\n"
        + "  .... aborting the run\n",
        encoding="utf-8",
    )


def _write_actual_surface_out(path: Path, energies: list[float]) -> None:
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
            ]
        ),
        encoding="utf-8",
    )


def _write_refinement_zero_distance_out(inp_path: Path, out_path: Path) -> None:
    """Scan finished (surface bracketed a maximum at point 2), then ORCA's
    TS-guess refinement constructed a zero-distance geometry and aborted."""
    _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=3)
    inp_path.with_suffix(".xyz").write_text(
        "2\ncorrupted refinement geometry\nH 0 0 0\nH 0 0 0\n",
        encoding="utf-8",
    )
    _write_surface_scan_done_out(out_path)
    out_path.write_text(
        out_path.read_text(encoding="utf-8")
        + "\n".join(
            [
                "REFINING THE TS GUESS STRUCTURE",
                "Error (ORCA_GTOINT/SHARK): Zero distance encountered between atoms 4 and 20",
                "ORCA finished by error termination in Startup",
                "[file orca_tools/qcmsg.cpp, line 394]:",
                "  .... aborting the run",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_surface_scan_done_out(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "**** RELAXED SURFACE SCAN DONE ***",
                "RELAXED SURFACE SCAN RESULTS",
                "The Calculated Surface using the 'Actual Energy'",
                "   1.86000000 -100.00000000",
                "   1.91000000 -99.50000000",
                "   1.96000000 -99.75000000",
                "The Calculated Surface using the SCF energy",
                "   1.86000000 -101.00000000",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _attempt_actions(saved: RunState, index: int = 0) -> list[str]:
    attempts = saved["attempts"]
    assert isinstance(attempts, list)
    attempt = attempts[index]
    assert isinstance(attempt, dict)
    actions = attempt.get("patch_actions")
    assert isinstance(actions, list)
    return [str(action) for action in actions]


def test_scants_detection_agrees_with_retry_policy_for_block_before_route(
    tmp_path: Path,
) -> None:
    """A %-block before the route line must not split the ScanTS predicates.

    retry_policy classifies inputs by scanning every route line, so the scants
    rewriters must use the same whole-file scan; otherwise the policy assigns
    ScanTS retries that every rewriter then refuses to prepare.
    """
    source_inp = tmp_path / "rxn.inp"
    source_inp.write_text(
        "\n".join(
            [
                "%maxcore 3000",
                "! B3LYP ScanTS",
                "%geom",
                "  Scan",
                "    B 4 20 = 1.86, 3.40, 32",
                "  end",
                "end",
                "* xyzfile 0 1 input.xyz",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert input_uses_scants(source_inp)
    assert retry_policy_for_input(source_inp).name == "scants"

    _write_scan_xyz_series(tmp_path, "rxn", count=8)
    prepared, actions = prepare_scants_scan_retry_input(
        source_inp=source_inp,
        target_inp=tmp_path / "rxn.retry01.inp",
        retry_number=1,
    )
    assert prepared is not None
    assert "scants_scan_range_continued_after_point_008" in actions


@pytest.mark.parametrize(
    ("name", "scan_lines", "expected_lines"),
    [
        (
            "bond",
            ["    B 4 20 = 1.86, 3.40, 32"],
            ["    B 4 20 = 2.05870968, 3.40, 28"],
        ),
        (
            "angle",
            ["    A 5 6 7 = 90.00, 120.00, 32"],
            ["    A 5 6 7 = 93.87096774, 120.00, 28"],
        ),
        (
            "mixed",
            [
                "    B 4 20 = 1.86, 3.40, 32",
                "    A 8 9 10 = 120.00, 60.00, 32",
            ],
            [
                "    B 4 20 = 2.05870968, 3.40, 28",
                "    A 8 9 10 = 112.25806452, 60.00, 28",
            ],
        ),
    ],
)
def test_relaxed_scan_resume_rewrite_updates_scan_ranges(
    tmp_path: Path,
    name: str,
    scan_lines: list[str],
    expected_lines: list[str],
) -> None:
    del name
    source_inp = tmp_path / "tsopt.inp"
    source_inp.write_text("! ScanTS\n", encoding="utf-8")
    _write_scan_xyz_series(tmp_path)
    lines = ["! ScanTS", "%geom", "  Scan", *scan_lines, "  end", "end"]

    actions = apply_scants_relaxed_scan_resume_rewrite(lines, source_inp)

    assert actions == ["scants_scan_range_resumed_after_point_004"]
    for expected in expected_lines:
        assert expected in lines
    for original in scan_lines:
        assert original not in lines


@pytest.mark.parametrize(
    ("name", "scan_lines", "expected_lines"),
    [
        (
            "bond_breaking",
            ["    B 4 20 = 1.86, 3.40, 32"],
            ["    B 4 20 = 3.44967742, 3.69806452, 6"],
        ),
        (
            "bond_forming",
            ["    B 4 20 = 3.40, 1.86, 32"],
            ["    B 4 20 = 1.81032258, 1.56193548, 6"],
        ),
        (
            "mixed",
            [
                "    B 4 20 = 1.86, 3.40, 32",
                "    A 8 9 10 = 120.00, 60.00, 32",
            ],
            [
                "    B 4 20 = 3.44967742, 3.69806452, 6",
                "    A 8 9 10 = 58.06451613, 48.38709677, 6",
            ],
        ),
    ],
)
def test_failed_scants_retry_extends_scan_endpoint_in_reaction_direction(
    tmp_path: Path,
    name: str,
    scan_lines: list[str],
    expected_lines: list[str],
) -> None:
    del name
    source_inp = tmp_path / "rxn.inp"
    target_inp = tmp_path / "rxn.retry01.inp"
    _write_scan_xyz_series(tmp_path, "rxn", count=32)
    lines = [
        "! ScanTS MORead",
        '%moinp "stale.gbw"',
        "%geom",
        "  Scan",
        *scan_lines,
        "  end",
        "end",
        "* xyzfile 0 1 input.xyz",
    ]

    actions = apply_scants_failed_scan_retry_rewrite(
        lines,
        retry_number=1,
        source_inp=source_inp,
        target_inp=target_inp,
    )

    assert "scants_scan_endpoint_extended_by_006_step" in actions
    assert not any(action.startswith("scants_scan_points_increased_") for action in actions)
    assert "route_remove_moread" in actions
    assert "moinp_removed" in actions
    assert "geometry_restart_from_rxn.032.xyz" in actions
    assert "scants_scan_range_continued_after_point_032" in actions
    assert "scants_retry_preserved_source_geometry" not in actions
    assert "MORead" not in lines[0]
    assert not any(line.strip().lower().startswith("%moinp") for line in lines)
    assert "* xyzfile 0 1 rxn.032.xyz" in lines
    assert "* xyzfile 0 1 input.xyz" not in lines
    for expected in expected_lines:
        assert expected in lines
    for original in scan_lines:
        assert original not in lines


def test_failed_scants_retry_removes_moread_from_all_route_lines(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    target_inp = tmp_path / "rxn.retry01.inp"
    _write_scan_xyz_series(tmp_path, "rxn", count=32)
    lines = [
        "! ScanTS B3LYP",
        "# route provenance # ! MORead",
        '# checkpoint provenance # %moinp "stale.gbw"',
        "%geom",
        "  Scan",
        "    B 4 20 = 1.86, 3.40, 32",
        "  end",
        "end",
        "* xyzfile 0 1 input.xyz",
    ]

    actions = apply_scants_failed_scan_retry_rewrite(
        lines,
        retry_number=1,
        source_inp=source_inp,
        target_inp=target_inp,
    )

    assert "route_remove_moread" in actions
    assert "moinp_removed" in actions
    assert all("MORead" not in line for line in lines if line.strip().startswith("!"))
    assert not any(line.strip().lower().startswith("%moinp") for line in lines)


def test_resumed_scants_retry_sources_scan_from_executed_resume_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On a resumed first retry, the scan-continuation source must be the input
    # that actually ran (tsopt.resume.inp, whose *.NNN.xyz are on disk), not
    # selected_inp (tsopt.inp). Sourcing from selected_inp reads stale/absent
    # numbered files and would dead-end the run despite valid resume artifacts.
    from orca_auto.orca.attempt.retry import RetryAttemptRequest, prepare_retry_attempt
    from orca_auto.orca.out_analyzer import OutAnalysis, _default_markers
    from orca_auto.orca.statuses import AnalyzerStatus

    selected_inp = tmp_path / "tsopt.inp"
    _write_scants_input(selected_inp)
    resume_inp = tmp_path / "tsopt.resume.inp"
    _write_scants_input(resume_inp)
    out_path = tmp_path / "tsopt.resume.out"
    out_path.write_text("mid-scan failure with no surface table\n", encoding="utf-8")

    captured: dict[str, Path] = {}

    def _spy(
        *, source_inp: Path, target_inp: Path, retry_number: int, max_memory_gb: int | None
    ) -> tuple[Path, list[str]]:
        captured["source_inp"] = source_inp
        return target_inp, ["scants_scan_continued"]

    monkeypatch.setattr("orca_auto.orca.attempt.retry.prepare_scants_scan_retry_input", _spy)

    state = new_state(tmp_path, selected_inp, max_retries=1)
    state["attempts"].append({"inp_path": str(resume_inp), "out_path": str(out_path)})
    ctx = RetryAttemptRequest(
        reaction_dir=tmp_path,
        selected_inp=selected_inp,
        state=state,
        resumed=True,
        current_inp=resume_inp,
        out_path=out_path,
        execution_index=1,
        retries_used=0,
        max_retries=1,
        analysis=OutAnalysis(
            status=AnalyzerStatus.TS_NOT_FOUND,
            reason="ts_not_found",
            markers=_default_markers(out_path),
        ),
        retry_inp_path=_retry_inp_path,
        emit=lambda _payload: None,
        notify_finished=None,
        notify_retry=None,
    )

    result = prepare_retry_attempt(ctx)

    assert result is None  # a retry was prepared, not a terminal exit
    assert captured["source_inp"] == resume_inp


def test_prepare_retry_attempt_appends_retry_patch_actions_to_existing_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.orca.attempt.retry import RetryAttemptRequest, prepare_retry_attempt
    from orca_auto.orca.out_analyzer import OutAnalysis, _default_markers
    from orca_auto.orca.statuses import AnalyzerStatus

    selected_inp = tmp_path / "calc.inp"
    selected_inp.write_text("! Opt B3LYP def2-SVP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    out_path = tmp_path / "calc.out"
    out_path.write_text("failed opt\n", encoding="utf-8")

    monkeypatch.setattr(
        "orca_auto.orca.attempt.retry.prepare_scants_scan_retry_input",
        lambda *, source_inp, target_inp, retry_number, max_memory_gb: (
            target_inp,
            ["retry_tightscf_added", "retry_slowconv_added"],
        ),
    )

    state = new_state(tmp_path, selected_inp, max_retries=1)
    state["attempts"].append(
        {
            "inp_path": str(selected_inp),
            "out_path": str(out_path),
            "patch_actions": ["resume_recreated_missing_input:calc.inp"],
        }
    )
    ctx = RetryAttemptRequest(
        reaction_dir=tmp_path,
        selected_inp=selected_inp,
        state=state,
        resumed=True,
        current_inp=selected_inp,
        out_path=out_path,
        execution_index=1,
        retries_used=0,
        max_retries=1,
        analysis=OutAnalysis(
            status=AnalyzerStatus.GEOM_NOT_CONVERGED,
            reason="geom_not_converged",
            markers=_default_markers(out_path),
        ),
        retry_inp_path=_retry_inp_path,
        emit=lambda _payload: None,
        notify_finished=None,
        notify_retry=None,
    )

    result = prepare_retry_attempt(ctx)

    assert result is None
    assert state["attempts"][-1]["patch_actions"] == [
        "resume_recreated_missing_input:calc.inp",
        "retry_tightscf_added",
        "retry_slowconv_added",
    ]


def test_resumed_scants_uses_refined_tsopt_geometry_as_optts_when_marker_present(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "tsopt.inp"
    _write_scants_input(selected_inp)
    selected_inp.with_suffix(".gbw").write_bytes(b"checkpoint")
    selected_inp.with_suffix(".xyz").write_text(
        "2\ninterrupted optts geometry\nH 0 0 0\nH 0 0 2.15\n",
        encoding="utf-8",
    )
    selected_inp.with_suffix(".out").write_text(
        "ScanTS option: We are already beyond the maximum\nREFINING TS GUESS STRUCTURE\n",
        encoding="utf-8",
    )
    runner = _CaptureSuccessRunner()

    rc, saved = _run_attempt(tmp_path, selected_inp, resumed=True, runner=runner)
    resume_inp = tmp_path / "tsopt.resume.inp"
    resume_text = resume_inp.read_text(encoding="utf-8")

    assert rc == 0
    assert runner.seen == [resume_inp]
    assert "OPTTS" in resume_text
    assert "ScanTS" not in resume_text
    assert "B 4 20 =" not in resume_text
    assert "* xyzfile 0 1 tsopt.xyz" in resume_text
    assert '%moinp "tsopt.gbw"' in resume_text
    actions = _attempt_actions(saved)
    assert "resume_checkpoint_restart_from_tsopt.gbw" in actions
    assert "resume_scants_resume_to_optts" in actions
    assert "resume_scants_scan_block_removed" in actions
    assert "resume_geometry_restart_from_tsopt.xyz" in actions


def test_resumed_scants_without_refining_marker_resumes_scan_range(tmp_path: Path) -> None:
    selected_inp = tmp_path / "tsopt.inp"
    _write_scants_input(
        selected_inp,
        [
            "    B 4 20 = 1.86, 3.40, 32",
            "    A 5 6 7 = 90.00, 120.00, 32",
        ],
    )
    selected_inp.with_suffix(".gbw").write_bytes(b"checkpoint")
    selected_inp.with_suffix(".xyz").write_text(
        "2\nrelaxed scan geometry\nH 0 0 0\nH 0 0 2.15\n",
        encoding="utf-8",
    )
    _write_scan_xyz_series(tmp_path)
    selected_inp.with_suffix(".out").write_text(
        "GEOMETRY OPTIMIZATION CYCLE 4\nRELAXED SURFACE SCAN STEP 4\n",
        encoding="utf-8",
    )
    runner = _CaptureSuccessRunner()

    rc, saved = _run_attempt(tmp_path, selected_inp, resumed=True, runner=runner)
    resume_inp = tmp_path / "tsopt.resume.inp"
    resume_text = resume_inp.read_text(encoding="utf-8")

    assert rc == 0
    assert runner.seen == [resume_inp]
    assert "ScanTS" in resume_text
    assert "OPTTS" not in resume_text
    assert "B 4 20 = 1.86, 3.40, 32" not in resume_text
    assert "A 5 6 7 = 90.00, 120.00, 32" not in resume_text
    assert "B 4 20 = 2.05870968, 3.40, 28" in resume_text
    assert "A 5 6 7 = 93.87096774, 120.00, 28" in resume_text
    assert "* xyzfile 0 1 tsopt.xyz" in resume_text
    actions = _attempt_actions(saved)
    assert "resume_scants_resume_to_optts" not in actions
    assert "resume_scants_scan_range_resumed_after_point_004" in actions
    assert "resume_geometry_restart_from_tsopt.xyz" in actions


def test_resumed_scants_after_scan_done_uses_highest_surface_xyz(tmp_path: Path) -> None:
    selected_inp = tmp_path / "tsopt.inp"
    _write_scants_input(selected_inp)
    selected_inp.with_suffix(".gbw").write_bytes(b"checkpoint")
    selected_inp.with_suffix(".xyz").write_text(
        "2\nsame-stem relaxed scan geometry\nH 0 0 0\nH 0 0 3.00\n",
        encoding="utf-8",
    )
    _write_scan_xyz_series(tmp_path, count=3)
    _write_surface_scan_done_out(selected_inp.with_suffix(".out"))
    runner = _CaptureSuccessRunner()

    rc, saved = _run_attempt(tmp_path, selected_inp, resumed=True, runner=runner)
    resume_inp = tmp_path / "tsopt.resume.inp"
    resume_text = resume_inp.read_text(encoding="utf-8")

    assert rc == 0
    assert runner.seen == [resume_inp]
    assert "OPTTS" in resume_text
    assert "ScanTS" not in resume_text
    assert "B 4 20 =" not in resume_text
    assert "* xyzfile 0 1 tsopt.002.xyz" in resume_text
    assert "* xyzfile 0 1 tsopt.xyz" not in resume_text
    actions = _attempt_actions(saved)
    assert "resume_scants_resume_to_optts" in actions
    assert "resume_geometry_restart_from_tsopt.002.xyz" in actions


def test_resume_finalizes_finished_scants_scan_as_exhausted(tmp_path: Path) -> None:
    selected_inp = tmp_path / "tsopt.inp"
    _write_scants_input(selected_inp)
    out_path = selected_inp.with_suffix(".out")
    _write_surface_scan_done_out(out_path)

    # A worker crashed after recording a finished ScanTS scan that did not
    # verify a TS, before the retry decision ran.
    state = new_state(tmp_path, selected_inp, max_retries=3)
    state["status"] = "retrying"
    state["attempts"].append(
        {
            "index": 1,
            "inp_path": str(selected_inp),
            "out_path": str(out_path),
            "return_code": 0,
            "analyzer_status": "ts_not_found",
            "analyzer_reason": "ts_criteria_failed",
            "markers": {"geometry_zero_distance": False},
            "patch_actions": [],
            "started_at": "2026-07-03T01:00:00+00:00",
            "ended_at": "2026-07-03T02:00:00+00:00",
        }
    )
    save_state(tmp_path, state)

    runner = _CaptureSuccessRunner()
    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=True,
        runner=runner,
        max_retries=3,
        retry_inp_path=_retry_inp_path,
        to_resolved_local=lambda raw: Path(raw),
        emit=lambda _payload: None,
    )
    saved = load_state(tmp_path)
    assert saved is not None

    # Resume must not recover/run a missing retry01: the scan already finished.
    assert rc == 1
    assert runner.seen == []
    assert not (tmp_path / "tsopt.retry01.inp").exists()
    final_result = saved.get("final_result")
    assert isinstance(final_result, dict)
    assert final_result.get("reason") == "scants_recipes_exhausted"


def test_resume_finalizes_failed_optts_fallback_as_exhausted(tmp_path: Path) -> None:
    # A ScanTS scan finished, its one-shot OptTS fallback ran and failed, then the
    # worker crashed before the exhaustion decision was recorded. On resume the last
    # recorded attempt is the failed OptTS fallback (input no longer ScanTS, output
    # has no surface table), but the ScanTS recipe chain is spent, so the run must be
    # finalized as scants_recipes_exhausted -- not re-run as further retries.
    selected_inp = tmp_path / "tsopt.inp"
    _write_scants_input(selected_inp)
    scan_out = selected_inp.with_suffix(".out")
    _write_surface_scan_done_out(scan_out)

    optts_inp = tmp_path / "tsopt.retry01.inp"
    optts_inp.write_text(
        "! OptTS B3LYP def2-SVP Freq\n* xyzfile 0 1 tsopt.002.xyz\n", encoding="utf-8"
    )
    optts_out = optts_inp.with_suffix(".out")
    _write_ts_not_found_out(optts_out)

    state = new_state(tmp_path, selected_inp, max_retries=3)
    state["status"] = "retrying"
    state["attempts"].append(
        {
            "index": 1,
            "inp_path": str(selected_inp),
            "out_path": str(scan_out),
            "return_code": 0,
            "analyzer_status": "ts_not_found",
            "analyzer_reason": "geometry_zero_distance",
            "markers": {"geometry_zero_distance": True},
            "patch_actions": ["scants_fallback_to_optts", "scants_guess_from_tsopt.002.xyz"],
            "started_at": "2026-07-03T01:00:00+00:00",
            "ended_at": "2026-07-03T02:00:00+00:00",
        }
    )
    state["attempts"].append(
        {
            "index": 2,
            "inp_path": str(optts_inp),
            "out_path": str(optts_out),
            "return_code": 1,
            "analyzer_status": "ts_not_found",
            "analyzer_reason": "ts_criteria_failed",
            "markers": {"geometry_zero_distance": False},
            "patch_actions": [],
            "started_at": "2026-07-03T03:00:00+00:00",
            "ended_at": "2026-07-03T04:00:00+00:00",
        }
    )
    save_state(tmp_path, state)

    runner = _CaptureSuccessRunner()
    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=True,
        runner=runner,
        max_retries=3,
        retry_inp_path=_retry_inp_path,
        to_resolved_local=lambda raw: Path(raw),
        emit=lambda _payload: None,
    )
    saved = load_state(tmp_path)
    assert saved is not None

    assert rc == 1
    assert runner.seen == []  # must not re-run the OptTS input
    assert not (tmp_path / "tsopt.retry02.inp").exists()
    final_result = saved.get("final_result")
    assert isinstance(final_result, dict)
    assert final_result.get("reason") == "scants_recipes_exhausted"


def test_resume_shield_reruns_prepared_optts_fallback_that_never_ran(tmp_path: Path) -> None:
    # The one-shot OptTS fallback was PREPARED (its patch action was recorded on the
    # ScanTS scan attempt and the retry input written), but the OptTS attempt was
    # interrupted before it ran/recorded, so the last recorded attempt is still the
    # ScanTS scan. The shield must NOT declare exhaustion -- the prepared OptTS must
    # still get its one run, matching the live retry path.
    from orca_auto.orca.attempt.resume import _scants_surface_exhausted_on_resume

    selected_inp = tmp_path / "tsopt.inp"
    _write_scants_input(selected_inp)
    scan_out = selected_inp.with_suffix(".out")
    _write_surface_scan_done_out(scan_out)
    state = new_state(tmp_path, selected_inp, max_retries=3)
    state["status"] = "retrying"
    state["attempts"].append(
        {
            "index": 1,
            "inp_path": str(selected_inp),
            "out_path": str(scan_out),
            "return_code": 0,
            "analyzer_status": "ts_not_found",
            "analyzer_reason": "geometry_zero_distance",
            "markers": {"geometry_zero_distance": True},
            "patch_actions": ["scants_fallback_to_optts", "scants_guess_from_tsopt.002.xyz"],
            "started_at": "2026-07-03T01:00:00+00:00",
            "ended_at": "2026-07-03T02:00:00+00:00",
        }
    )

    assert _scants_surface_exhausted_on_resume(state, state["attempts"][-1]) is False


def test_resume_finalizes_failed_resume_converted_optts_as_exhausted(tmp_path: Path) -> None:
    # Sibling of the failed-live-fallback case: the resume-path ScanTS->OptTS
    # conversion records "resume_scants_resume_to_optts" (not "scants_fallback_to_optts").
    # A second resume after that converted OptTS attempt fails must also finalize as
    # scants_recipes_exhausted, not re-run the OptTS input as further retries.
    selected_inp = tmp_path / "tsopt.inp"
    _write_scants_input(selected_inp)
    scan_out = selected_inp.with_suffix(".out")
    _write_surface_scan_done_out(scan_out)

    optts_inp = tmp_path / "tsopt.resume.inp"
    optts_inp.write_text("! OptTS B3LYP def2-SVP Freq\n* xyzfile 0 1 tsopt.xyz\n", encoding="utf-8")
    optts_out = optts_inp.with_suffix(".out")
    _write_ts_not_found_out(optts_out)

    state = new_state(tmp_path, selected_inp, max_retries=3)
    state["status"] = "retrying"
    state["attempts"].append(
        {
            "index": 1,
            "inp_path": str(selected_inp),
            "out_path": str(scan_out),
            "return_code": 0,
            "analyzer_status": "ts_not_found",
            "analyzer_reason": "ts_criteria_failed",
            "markers": {"geometry_zero_distance": False},
            "patch_actions": [],
            "started_at": "2026-07-03T01:00:00+00:00",
            "ended_at": "2026-07-03T02:00:00+00:00",
        }
    )
    state["attempts"].append(
        {
            "index": 2,
            "inp_path": str(optts_inp),
            "out_path": str(optts_out),
            "return_code": 1,
            "analyzer_status": "ts_not_found",
            "analyzer_reason": "ts_criteria_failed",
            "markers": {"geometry_zero_distance": False},
            "patch_actions": [
                "resume_scants_resume_to_optts",
                "resume_geometry_restart_from_tsopt.gbw",
            ],
            "started_at": "2026-07-03T03:00:00+00:00",
            "ended_at": "2026-07-03T04:00:00+00:00",
        }
    )
    save_state(tmp_path, state)

    runner = _CaptureSuccessRunner()
    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=True,
        runner=runner,
        max_retries=3,
        retry_inp_path=_retry_inp_path,
        to_resolved_local=lambda raw: Path(raw),
        emit=lambda _payload: None,
    )
    saved = load_state(tmp_path)
    assert saved is not None

    assert rc == 1
    assert runner.seen == []
    final_result = saved.get("final_result")
    assert isinstance(final_result, dict)
    assert final_result.get("reason") == "scants_recipes_exhausted"


def test_scants_optts_fallback_builder_still_uses_highest_surface_xyz(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    _write_scan_xyz_series(tmp_path, "rxn", count=3)
    selected_inp.with_suffix(".xyz").write_text(
        "2\ninvalid refined guess\nH 0 0 0\nH 0 0 0\n",
        encoding="utf-8",
    )
    _write_surface_scan_done_out(selected_inp.with_suffix(".out"))
    retry_inp = tmp_path / "rxn.retry01.inp"

    prepared, actions = prepare_scants_optts_fallback_input(
        source_inp=selected_inp,
        target_inp=retry_inp,
        reaction_dir=tmp_path,
        out_path=selected_inp.with_suffix(".out"),
    )
    retry_text = retry_inp.read_text(encoding="utf-8")

    assert prepared == retry_inp
    assert "OPTTS" in retry_text
    assert "ScanTS" not in retry_text
    assert "B 4 20 =" not in retry_text
    assert "* xyzfile 0 1 rxn.002.xyz" in retry_text
    assert "* xyzfile 0 1 rxn.xyz" not in retry_text
    assert "scants_fallback_to_optts" in actions
    assert "scants_guess_from_rxn.002.xyz" in actions


def test_zero_distance_refinement_crash_retries_as_optts_from_maximum(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    runner = _ScanTsRefinementCrashOpttsRunner()

    rc, saved = _run_attempt(
        tmp_path,
        selected_inp,
        resumed=False,
        runner=runner,
        max_retries=3,
    )
    retry01_inp = tmp_path / "rxn.retry01.inp"
    retry01_text = retry01_inp.read_text(encoding="utf-8")

    assert rc == 0
    assert runner.seen == [selected_inp, retry01_inp]
    assert "OPTTS" in retry01_text
    assert "ScanTS" not in retry01_text
    assert "Scan" not in retry01_text
    assert "Freq" in retry01_text
    # Guess is the HIGHEST surface point (index 2), not the corrupted same-stem
    # rxn.xyz that ORCA's refinement left behind.
    assert "* xyzfile 0 1 rxn.002.xyz" in retry01_text
    assert "rxn.xyz" not in retry01_text
    assert saved.get("status") == "completed"
    actions = _attempt_actions(saved)
    assert "scants_fallback_to_optts" in actions
    assert "scants_scan_block_removed" in actions
    assert "scants_guess_from_rxn.002.xyz" in actions
    # The synthetic attempt harness binds no execution generation, so no report
    # file is published (fail closed); render the report body directly instead.
    from orca_auto.orca.report import compose_job_report_html

    report_html = compose_job_report_html(tmp_path, saved)
    assert report_html is not None
    assert "OptTS fallback (scan maximum)" in report_html


def test_failed_optts_fallback_exhausts_recipes(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    runner = _ScanTsOpttsFallbackFailsThenChainRunner()

    rc, saved = _run_attempt(
        tmp_path,
        selected_inp,
        resumed=False,
        runner=runner,
        max_retries=3,
    )
    optts_inp = tmp_path / "rxn.retry01.inp"

    # The one-shot OptTS fallback runs; when it fails too, the run ends —
    # endpoint/reverse exploration now belongs to the scan_ts_search workflow.
    assert rc == 1
    assert runner.seen == [selected_inp, optts_inp]
    assert not (tmp_path / "rxn.retry02.inp").exists()
    assert saved.get("status") == "failed"
    final_result = saved.get("final_result")
    assert isinstance(final_result, dict)
    assert final_result.get("reason") == "scants_recipes_exhausted"
    all_actions = [action for index in range(2) for action in _attempt_actions(saved, index=index)]
    assert all_actions.count("scants_fallback_to_optts") == 1


def test_scan_profile_interior_barrier_prominence() -> None:
    kcal_per_hartree = 627.5094740631

    assert scan_profile_interior_barrier_kcal([-100.0, -99.9]) is None
    assert scan_profile_interior_barrier_kcal([-100.0, -100.1, -100.2, -100.3]) == pytest.approx(
        0.0
    )
    # Maximum at the profile edge is not an interior barrier.
    assert scan_profile_interior_barrier_kcal([-99.9, -100.0, -100.1]) == pytest.approx(0.0)
    # Interior hump of 0.00015 Ha above the shallower flank.
    noise = scan_profile_interior_barrier_kcal([-100.0, -99.99985, -100.0002, -100.001])
    assert noise == pytest.approx(0.00015 * kcal_per_hartree, rel=1e-6)
    barrier = scan_profile_interior_barrier_kcal([-100.0, -99.99, -100.02])
    assert barrier == pytest.approx(0.01 * kcal_per_hartree, rel=1e-6)


def test_surface_maximum_failure_without_zero_distance_exhausts_immediately(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    runner = _ScanTsReverseDerivedOpttsFailsRunner()

    rc, saved = _run_attempt(
        tmp_path,
        selected_inp,
        resumed=False,
        runner=runner,
        max_retries=4,
    )

    # The scan finished (surface table present) before the failure, so this is
    # not a mid-scan crash: no continuation, no hardening — the run ends.
    assert rc == 1
    assert runner.seen == [selected_inp]
    assert not (tmp_path / "rxn.retry01.inp").exists()
    assert saved.get("status") == "failed"
    final_result = saved.get("final_result")
    assert isinstance(final_result, dict)
    assert final_result.get("reason") == "scants_recipes_exhausted"
    actions = _attempt_actions(saved)
    assert "scants_retry_stopped:scants_recipes_exhausted" in actions
    assert not any(action.startswith("scf_") for action in actions)


def test_scants_continuation_then_surface_maximum_failure_exhausts(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    runner = _ScanTsContinuationOpttsFailureReverseRunner()

    rc, saved = _run_attempt(
        tmp_path,
        selected_inp,
        resumed=False,
        runner=runner,
        max_retries=3,
    )
    retry01_inp = tmp_path / "rxn.retry01.inp"
    retry01_text = retry01_inp.read_text(encoding="utf-8")

    # Mid-scan crash without a surface -> continuation retry (a genuine
    # calculation-failure recipe). The continuation then fails WITH a surface
    # table, so the run ends instead of chaining a reverse scan.
    assert rc == 1
    assert runner.seen == [selected_inp, retry01_inp]
    assert "ScanTS" in retry01_text
    assert "B 4 20 = 3.44967742, 3.69806452, 6" in retry01_text
    assert "* xyzfile 0 1 rxn.032.xyz" in retry01_text
    assert not (tmp_path / "rxn.retry02.inp").exists()
    assert saved.get("status") == "failed"
    final_result = saved.get("final_result")
    assert isinstance(final_result, dict)
    assert final_result.get("reason") == "scants_recipes_exhausted"


def test_failed_scants_without_surface_maximum_continues_from_last_numbered_xyz(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    selected_inp.with_suffix(".gbw").write_bytes(b"stale checkpoint")
    runner = _ScanTsNoSurfaceFallbackRunner()

    rc, saved = _run_attempt(tmp_path, selected_inp, resumed=False, runner=runner)
    retry_inp = tmp_path / "rxn.retry01.inp"
    retry_text = retry_inp.read_text(encoding="utf-8")

    assert rc == 0
    assert runner.seen == [selected_inp, retry_inp]
    assert "ScanTS" in retry_text
    assert "OPTTS" not in retry_text
    assert "B 4 20 = 3.44967742, 3.69806452, 6" in retry_text
    assert "B 4 20 = 1.86, 3.40, 32" not in retry_text
    assert "* xyzfile 0 1 rxn.032.xyz" in retry_text
    assert "* xyzfile 0 1 input.xyz" not in retry_text
    assert "rxn.xyz" not in retry_text
    assert "%moinp" not in retry_text
    assert "MORead" not in retry_text
    assert saved.get("status") == "completed"
    actions = _attempt_actions(saved)
    assert "scants_scan_endpoint_extended_by_006_step" in actions
    assert not any(action.startswith("scants_scan_points_increased_") for action in actions)
    assert "geometry_restart_from_rxn.032.xyz" in actions
    assert "scants_scan_range_continued_after_point_032" in actions
    assert "scants_retry_preserved_source_geometry" not in actions
    assert "scants_fallback_to_optts" not in actions
    assert "checkpoint_restart_from_rxn.gbw" not in actions


def test_failed_scants_without_surface_maximum_or_numbered_xyz_fails_closed(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    runner = _ScanTsNoSurfaceNoNumberedRunner()

    rc, saved = _run_attempt(tmp_path, selected_inp, resumed=False, runner=runner)
    retry_inp = tmp_path / "rxn.retry01.inp"

    assert rc == 1
    assert runner.seen == [selected_inp]
    assert not retry_inp.exists()
    assert saved.get("status") == "failed"
    final_result = saved.get("final_result")
    assert isinstance(final_result, dict)
    assert final_result.get("reason") == "scants_recipes_exhausted"
    actions = _attempt_actions(saved)
    assert "scants_retry_stopped:scants_recipes_exhausted" in actions
    assert "checkpoint_restart_from_rxn.gbw" not in actions
    assert "geometry_restart_from_rxn.xyz" not in actions


def test_failed_scants_second_failure_fails_closed_without_generic_hardening(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    runner = _ScanTsTwoFailureRunner()

    rc, saved = _run_attempt(
        tmp_path,
        selected_inp,
        resumed=False,
        runner=runner,
        max_retries=8,
    )
    retry01_inp = tmp_path / "rxn.retry01.inp"
    retry02_inp = tmp_path / "rxn.retry02.inp"
    retry01_text = retry01_inp.read_text(encoding="utf-8")

    assert rc == 1
    assert runner.seen == [selected_inp, retry01_inp]
    assert not retry02_inp.exists()
    assert "ScanTS" in retry01_text
    assert "OPTTS" not in retry01_text
    assert "TightSCF" not in retry01_text
    assert "SlowConv" not in retry01_text
    assert "%scf" not in retry01_text
    assert "B 4 20 = 3.44967742, 3.69806452, 6" in retry01_text
    assert "* xyzfile 0 1 rxn.032.xyz" in retry01_text
    assert "rxn.retry01.xyz" not in retry01_text
    assert "rxn.retry01.gbw" not in retry01_text
    assert "%moinp" not in retry01_text
    assert "MORead" not in retry01_text
    assert saved.get("status") == "failed"
    final_result = saved.get("final_result")
    assert isinstance(final_result, dict)
    assert final_result.get("reason") == "scants_recipes_exhausted"
    actions = _attempt_actions(saved, index=1)
    assert "scants_retry_stopped:scants_recipes_exhausted" in actions
    assert "route_add_tightscf_slowconv" not in actions
    assert "scf_maxiter_300" not in actions
    assert "checkpoint_restart_from_rxn.retry01.gbw" not in actions
