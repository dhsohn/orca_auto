from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.orca.attempt_engine import run_attempts
from orca_auto.orca.scants import apply_scants_relaxed_scan_resume_rewrite
from orca_auto.orca.state import load_state, new_state
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
            _write_surface_scan_failure(inp_path, out_path)
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
) -> tuple[int, RunState]:
    state = new_state(reaction_dir, selected_inp, max_retries=1)
    rc = run_attempts(
        reaction_dir,
        selected_inp,
        state,
        resumed=resumed,
        runner=runner,
        max_retries=1,
        retry_inp_path=_retry_inp_path,
        to_resolved_local=lambda raw: Path(raw),
        emit=lambda _payload: None,
    )
    saved = load_state(reaction_dir)
    assert saved is not None
    return rc, saved


def _write_surface_scan_failure(inp_path: Path, out_path: Path) -> None:
    root = inp_path.parent
    _write_scan_xyz_series(root, inp_path.stem, count=3)
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


def _attempt_actions(saved: RunState) -> list[str]:
    attempts = saved["attempts"]
    assert isinstance(attempts, list)
    first_attempt = attempts[0]
    assert isinstance(first_attempt, dict)
    actions = first_attempt["patch_actions"]
    assert isinstance(actions, list)
    return [str(action) for action in actions]


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


def test_failed_scants_retries_as_optts_from_highest_surface_xyz(tmp_path: Path) -> None:
    selected_inp = tmp_path / "rxn.inp"
    _write_scants_input(selected_inp)
    runner = _ScanTsFallbackRunner()

    rc, saved = _run_attempt(tmp_path, selected_inp, resumed=False, runner=runner)
    retry_inp = tmp_path / "rxn.retry01.inp"
    retry_text = retry_inp.read_text(encoding="utf-8")

    assert rc == 0
    assert runner.seen == [selected_inp, retry_inp]
    assert "OPTTS" in retry_text
    assert "ScanTS" not in retry_text
    assert "B 4 20 =" not in retry_text
    assert "* xyzfile 0 1 rxn.002.xyz" in retry_text
    assert saved["status"] == "completed"
    actions = _attempt_actions(saved)
    assert "scants_fallback_to_optts" in actions
    assert "scants_guess_from_rxn.002.xyz" in actions
