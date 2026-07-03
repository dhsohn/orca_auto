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
    prepare_scants_endpoint_scan_input,
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


class _ScanTsFallbackRunner(_CaptureSuccessRunner):
    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
            _write_surface_scan_failure(inp_path, out_path, xyz_count=3)
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        if len(self.seen) == 2:
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


class _ScanTsOpttsFailureReverseRunner(_CaptureSuccessRunner):
    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
            _write_surface_scan_failure(inp_path, out_path, xyz_count=32)
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        if len(self.seen) == 2:
            _write_ts_not_found_out(out_path)
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


class _ScanTsContinuationEarlyMaximumEndpointReverseRunner(_CaptureSuccessRunner):
    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
            _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=32)
            out_path.write_text(
                "ORCA finished by error termination in Startup\n"
                "[file orca_tools/qcmsg.cpp, line 394]:\n"
                "  .... aborting the run\n",
                encoding="utf-8",
            )
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        if len(self.seen) == 2:
            _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=3)
            _write_surface_scan_done_out(out_path)
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        if len(self.seen) == 3:
            _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=3)
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


class _ScanTsEndpointProfileRunner(_CaptureSuccessRunner):
    """Early-maximum ScanTS failure, then an endpoint scan printing the given surface.

    Any later attempt reports a found TS so barrier-carrying profiles can run
    the reverse scan to a successful finish.
    """

    def __init__(
        self,
        *,
        forward_energies: list[float],
        endpoint_energies: list[float],
    ) -> None:
        super().__init__()
        self._forward_energies = forward_energies
        self._endpoint_energies = endpoint_energies

    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
            _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=3)
            inp_path.with_suffix(".xyz").write_text(
                "2\ninvalid refined guess\nH 0 0 0\nH 0 0 0\n",
                encoding="utf-8",
            )
            _write_actual_surface_out(out_path, self._forward_energies)
            out_path.write_text(
                out_path.read_text(encoding="utf-8")
                + "ORCA finished by error termination in Startup\n"
                + "[file orca_tools/qcmsg.cpp, line 394]:\n"
                + "  .... aborting the run\n",
                encoding="utf-8",
            )
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        if len(self.seen) == 2:
            _write_scan_xyz_series(inp_path.parent, inp_path.stem, count=29)
            _write_actual_surface_out(out_path, self._endpoint_energies)
            out_path.write_text(
                out_path.read_text(encoding="utf-8") + "****ORCA TERMINATED NORMALLY****\n",
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


class _ScanTsResumeEndpointRerunRunner(_CaptureSuccessRunner):
    def run(self, inp_path: Path) -> SimpleNamespace:
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
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


def test_failed_scants_with_early_surface_maximum_completes_endpoint_then_reverses(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    selected_inp.with_suffix(".gbw").write_bytes(b"stale checkpoint")
    runner = _ScanTsFallbackRunner()

    rc, saved = _run_attempt(
        tmp_path,
        selected_inp,
        resumed=False,
        runner=runner,
        max_retries=2,
    )
    endpoint_inp = tmp_path / "rxn.retry01.inp"
    reverse_inp = tmp_path / "rxn.retry02.inp"
    endpoint_text = endpoint_inp.read_text(encoding="utf-8")
    reverse_text = reverse_inp.read_text(encoding="utf-8")

    assert rc == 0
    assert runner.seen == [selected_inp, endpoint_inp, reverse_inp]
    assert "Opt" in endpoint_text
    assert "ScanTS" not in endpoint_text
    assert "Freq" not in endpoint_text
    assert "B 4 20 = 2.00903226, 3.40, 29" in endpoint_text
    assert "* xyzfile 0 1 rxn.003.xyz" in endpoint_text
    assert "%moinp" not in endpoint_text
    assert "MORead" not in endpoint_text
    assert "ScanTS" in reverse_text
    assert "OPTTS" not in reverse_text
    assert "B 4 20 = 3.40, 1.86, 32" in reverse_text
    assert "* xyzfile 0 1 rxn.retry01.029.xyz" in reverse_text
    assert "* xyzfile 0 1 input.xyz" not in reverse_text
    assert "* xyzfile 0 1 rxn.003.xyz" not in reverse_text
    assert "rxn.xyz" not in reverse_text
    assert "%moinp" not in reverse_text
    assert "MORead" not in reverse_text
    assert saved.get("status") == "completed"
    endpoint_actions = _attempt_actions(saved)
    assert "scants_endpoint_scan_route_to_opt" in endpoint_actions
    assert "scants_endpoint_scan_removed_freq_irc" in endpoint_actions
    assert "scants_endpoint_scan_to_original_endpoint" in endpoint_actions
    assert "scants_endpoint_scan_from_point_003" in endpoint_actions
    assert "scants_endpoint_scan_points_29" in endpoint_actions
    assert "geometry_restart_from_rxn.003.xyz" in endpoint_actions
    reverse_actions = _attempt_actions(saved, index=1)
    assert "scants_reverse_scan" in reverse_actions
    assert "scants_reverse_scan_route_restored" in reverse_actions
    assert "scants_reverse_scan_from_continuation_after_point_003" in reverse_actions
    assert "scants_reverse_scan_points_32" in reverse_actions
    assert "geometry_restart_from_rxn.retry01.029.xyz" in reverse_actions
    assert "scants_fallback_to_optts" not in endpoint_actions + reverse_actions
    assert "checkpoint_restart_from_rxn.gbw" not in endpoint_actions + reverse_actions


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


def test_completed_endpoint_scan_with_barrierless_profile_stops_without_reverse_scan(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    runner = _ScanTsEndpointProfileRunner(
        # Tiny endpoint hump (~0.09 kcal/mol), then monotonic descent: no TS
        # anywhere along the coordinate, so the reverse scan must be skipped.
        forward_energies=[-100.0, -99.99985, -99.9999],
        endpoint_energies=[-100.0002, -100.0006, -100.0012, -100.003],
    )

    rc, saved = _run_attempt(
        tmp_path,
        selected_inp,
        resumed=False,
        runner=runner,
        max_retries=3,
    )
    endpoint_inp = tmp_path / "rxn.retry01.inp"
    reverse_inp = tmp_path / "rxn.retry02.inp"

    assert rc == 1
    assert runner.seen == [selected_inp, endpoint_inp]
    assert not reverse_inp.exists()
    assert saved.get("status") == "failed"
    final_result = saved.get("final_result")
    assert isinstance(final_result, dict)
    assert final_result.get("reason") == "scan_profile_no_barrier"
    actions = _attempt_actions(saved, index=1)
    assert "scants_retry_stopped:scan_profile_no_barrier" in actions


def test_completed_endpoint_scan_with_interior_barrier_still_reverses(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    runner = _ScanTsEndpointProfileRunner(
        forward_energies=[-100.0, -99.99985, -99.9999],
        # Interior maximum ~1.1 kcal/mol above the shallower flank: a real
        # barrier, so the reverse scan must still run.
        endpoint_energies=[-100.0002, -99.9985, -100.002, -100.004],
    )

    rc, saved = _run_attempt(
        tmp_path,
        selected_inp,
        resumed=False,
        runner=runner,
        max_retries=3,
    )
    endpoint_inp = tmp_path / "rxn.retry01.inp"
    reverse_inp = tmp_path / "rxn.retry02.inp"
    reverse_text = reverse_inp.read_text(encoding="utf-8")

    assert rc == 0
    assert runner.seen == [selected_inp, endpoint_inp, reverse_inp]
    assert "ScanTS" in reverse_text
    assert "B 4 20 = 3.40, 1.86, 32" in reverse_text
    assert saved.get("status") == "completed"
    reverse_actions = _attempt_actions(saved, index=1)
    assert "scants_reverse_scan" in reverse_actions


def test_endpoint_scan_route_rewrite_does_not_duplicate_opt(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    source_inp.write_text(
        "\n".join(
            [
                "! ScanTS Opt B3LYP def2-SVP Freq",
                "",
                "%geom",
                "  Scan",
                "    B 4 20 = 1.86, 3.40, 32",
                "  end",
                "end",
                "",
                "* xyzfile 0 1 input.xyz",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _write_scan_xyz_series(tmp_path, "rxn", count=3)
    target_inp = tmp_path / "rxn.retry01.inp"

    prepared, actions = prepare_scants_endpoint_scan_input(
        source_inp=source_inp,
        target_inp=target_inp,
    )

    assert prepared == target_inp
    route = next(
        line for line in target_inp.read_text(encoding="utf-8").splitlines() if line.startswith("!")
    )
    assert route.split().count("Opt") == 1
    assert "ScanTS" not in route
    assert "Freq" not in route
    assert "scants_endpoint_scan_route_to_opt" in actions


def test_resume_after_completed_endpoint_scan_continues_to_reverse_scan(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    first_runner = _ScanTsFallbackRunner()
    rc, _saved = _run_attempt(
        tmp_path,
        selected_inp,
        resumed=False,
        runner=first_runner,
        max_retries=3,
    )
    assert rc == 0

    # Simulate a crash after the completed endpoint-scan attempt was recorded
    # but before the reverse-scan retry input was prepared.
    state = load_state(tmp_path)
    assert state is not None
    attempts = state["attempts"]
    assert isinstance(attempts, list)
    del attempts[2:]
    attempts[1]["patch_actions"] = []
    state["status"] = "retrying"
    state["final_result"] = None
    save_state(tmp_path, state)
    (tmp_path / "rxn.retry02.inp").unlink()
    (tmp_path / "rxn.retry02.out").unlink()

    resume_runner = _ScanTsResumeEndpointRerunRunner()
    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=True,
        runner=resume_runner,
        max_retries=3,
        retry_inp_path=_retry_inp_path,
        to_resolved_local=lambda raw: Path(raw),
        emit=lambda _payload: None,
    )
    saved = load_state(tmp_path)
    assert saved is not None

    recovered_endpoint_inp = tmp_path / "rxn.retry02.inp"
    reverse_inp = tmp_path / "rxn.retry03.inp"

    assert rc == 0
    # Without the pending-reverse guard, resume finished the run as COMPLETED
    # off the intermediate endpoint scan without running anything.
    assert resume_runner.seen == [recovered_endpoint_inp, reverse_inp]
    reverse_text = reverse_inp.read_text(encoding="utf-8")
    assert "ScanTS" in reverse_text
    assert "B 4 20 = 3.40, 1.86, 32" in reverse_text
    assert "* xyzfile 0 1 rxn.retry02.029.xyz" in reverse_text
    assert saved.get("status") == "completed"
    reverse_actions = _attempt_actions(saved, index=2)
    assert "scants_reverse_scan" in reverse_actions
    assert "scants_reverse_scan_route_restored" in reverse_actions


def test_reverse_scan_derived_maximum_fails_closed_without_optts_hardening(
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
    assert saved.get("status") == "failed"
    final_result = saved.get("final_result")
    assert isinstance(final_result, dict)
    assert final_result.get("reason") == "scants_recipes_exhausted"
    actions = _attempt_actions(saved, index=1)
    assert "scants_retry_stopped:scants_recipes_exhausted" in actions
    assert not any(action.startswith("scf_") for action in actions)


def test_scants_continuation_maximum_retries_combined_reverse_scan(
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
    retry02_inp = tmp_path / "rxn.retry02.inp"
    retry02_text = retry02_inp.read_text(encoding="utf-8")

    assert rc == 0
    assert runner.seen == [selected_inp, retry01_inp, retry02_inp]
    assert "ScanTS" in retry02_text
    assert "OPTTS" not in retry02_text
    assert "TightSCF" not in retry02_text
    assert "SlowConv" not in retry02_text
    assert "B 4 20 = 3.69806452, 1.86, 38" in retry02_text
    assert "B 4 20 = 3.44967742, 3.69806452, 6" not in retry02_text
    assert "* xyzfile 0 1 rxn.retry01.006.xyz" in retry02_text
    assert "* xyzfile 0 1 input.xyz" not in retry02_text
    assert "* xyzfile 0 1 rxn.032.xyz" not in retry02_text
    actions = _attempt_actions(saved, index=1)
    assert "scants_reverse_scan" in actions
    assert "scants_reverse_scan_from_continuation_after_point_032" in actions
    assert "scants_reverse_scan_points_38" in actions
    assert "geometry_restart_from_rxn.retry01.006.xyz" in actions


def test_scants_continuation_early_maximum_completes_endpoint_then_reverses_38_points(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    runner = _ScanTsContinuationEarlyMaximumEndpointReverseRunner()

    rc, saved = _run_attempt(
        tmp_path,
        selected_inp,
        resumed=False,
        runner=runner,
        max_retries=3,
    )
    retry01_inp = tmp_path / "rxn.retry01.inp"
    endpoint_inp = tmp_path / "rxn.retry02.inp"
    reverse_inp = tmp_path / "rxn.retry03.inp"
    endpoint_text = endpoint_inp.read_text(encoding="utf-8")
    reverse_text = reverse_inp.read_text(encoding="utf-8")

    assert rc == 0
    assert runner.seen == [selected_inp, retry01_inp, endpoint_inp, reverse_inp]
    assert "Opt" in endpoint_text
    assert "ScanTS" not in endpoint_text
    assert "B 4 20 = 3.59870968, 3.69806452, 3" in endpoint_text
    assert "* xyzfile 0 1 rxn.retry01.003.xyz" in endpoint_text
    assert "ScanTS" in reverse_text
    assert "OPTTS" not in reverse_text
    assert "B 4 20 = 3.69806452, 1.86, 38" in reverse_text
    assert "B 4 20 = 3.59870968, 3.69806452, 3" not in reverse_text
    assert "* xyzfile 0 1 rxn.retry02.003.xyz" in reverse_text
    assert "* xyzfile 0 1 rxn.retry01.003.xyz" not in reverse_text
    endpoint_actions = _attempt_actions(saved, index=1)
    assert "scants_endpoint_scan_to_original_endpoint" in endpoint_actions
    assert "scants_endpoint_scan_from_point_003" in endpoint_actions
    assert "scants_endpoint_scan_points_3" in endpoint_actions
    reverse_actions = _attempt_actions(saved, index=2)
    assert "scants_reverse_scan" in reverse_actions
    assert "scants_reverse_scan_route_restored" in reverse_actions
    assert "scants_reverse_scan_from_continuation_after_point_035" in reverse_actions
    assert "scants_reverse_scan_points_38" in reverse_actions
    assert "geometry_restart_from_rxn.retry02.003.xyz" in reverse_actions


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
