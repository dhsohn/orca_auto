from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from orca_auto.orca.attempt_engine import run_attempts
from orca_auto.orca.scants import apply_scants_relaxed_scan_resume_rewrite
from orca_auto.orca.state import load_state, new_state


class _CaptureSuccessRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path):
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
    def run(self, inp_path: Path):
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
            self._write_failed_scants_attempt(inp_path, out_path)
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

    def _write_failed_scants_attempt(self, inp_path: Path, out_path: Path) -> None:
        root = inp_path.parent
        for idx in range(1, 4):
            (root / f"{inp_path.stem}.{idx:03d}.xyz").write_text(
                f"2\nscan step {idx}\nH 0 0 0\nH 0 0 {idx}.0\n",
                encoding="utf-8",
            )
        # The ORCA-refined tsopt.xyz can be invalid after a failed ScanTS refinement;
        # ScanTS fallback must select the highest scan point, not this latest/same-stem xyz.
        inp_path.with_suffix(".xyz").write_text(
            "2\ninvalid refined guess\nH 0 0 0\nH 0 0 0\n",
            encoding="utf-8",
        )
        out_path.write_text(
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
                    "ORCA finished by error termination in Startup",
                    "[file orca_tools/qcmsg.cpp, line 394]:",
                    "  .... aborting the run",
                ]
            ),
            encoding="utf-8",
        )


def _retry_inp_path(selected_inp: Path, retry_number: int) -> Path:
    return selected_inp.parent / f"{selected_inp.stem}.retry{retry_number:02d}.inp"


class TestScanTsSupport(unittest.TestCase):
    def test_relaxed_scan_resume_rewrite_supports_bond_angle_and_mixed_same_count(
        self,
    ) -> None:
        cases = [
            (
                "bond",
                ["    B 4 20 = 1.86, 3.40, 32"],
                ["    B 4 20 = 2.05870968, 3.40, 28"],
            ),
            (
                "multiple_bonds",
                [
                    "    B 4 20 = 1.86, 3.40, 32",
                    "    B 5 21 = 2.50, 1.00, 32",
                ],
                [
                    "    B 4 20 = 2.05870968, 3.40, 28",
                    "    B 5 21 = 2.30645161, 1.00, 28",
                ],
            ),
            (
                "angle",
                ["    A 5 6 7 = 90.00, 120.00, 32"],
                ["    A 5 6 7 = 93.87096774, 120.00, 28"],
            ),
            (
                "multiple_angles",
                [
                    "    A 5 6 7 = 90.00, 120.00, 32",
                    "    A 8 9 10 = 120.00, 60.00, 32",
                ],
                [
                    "    A 5 6 7 = 93.87096774, 120.00, 28",
                    "    A 8 9 10 = 112.25806452, 60.00, 28",
                ],
            ),
            (
                "mixed_bonds_and_angles",
                [
                    "    B 4 20 = 1.86, 3.40, 32",
                    "    B 5 21 = 2.50, 1.00, 32",
                    "    A 5 6 7 = 90.00, 120.00, 32",
                    "    A 8 9 10 = 120.00, 60.00, 32",
                ],
                [
                    "    B 4 20 = 2.05870968, 3.40, 28",
                    "    B 5 21 = 2.30645161, 1.00, 28",
                    "    A 5 6 7 = 93.87096774, 120.00, 28",
                    "    A 8 9 10 = 112.25806452, 60.00, 28",
                ],
            ),
        ]
        for name, scan_lines, expected_lines in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                source_inp = root / "tsopt.inp"
                source_inp.write_text("! ScanTS\n", encoding="utf-8")
                for idx in range(1, 5):
                    (root / f"tsopt.{idx:03d}.xyz").write_text(
                        f"2\nscan step {idx}\nH 0 0 0\nH 0 0 {idx}.0\n",
                        encoding="utf-8",
                    )
                lines = ["! ScanTS", "%geom", "  Scan", *scan_lines, "  end", "end"]

                actions = apply_scants_relaxed_scan_resume_rewrite(lines, source_inp)

                self.assertEqual(actions, ["scants_scan_range_resumed_after_point_004"])
                for expected in expected_lines:
                    self.assertIn(expected, lines)
                for original in scan_lines:
                    self.assertNotIn(original, lines)

    def test_resumed_scants_uses_latest_tsopt_geometry_as_optts_not_scan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "tsopt.inp"
            selected_inp.write_text(
                "\n".join(
                    [
                        "! ScanTS B3LYP def2-SVP D3BJ CPCM(THF) Freq",
                        "",
                        "%geom",
                        "  MaxIter 200",
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
            selected_inp.with_suffix(".gbw").write_bytes(b"checkpoint")
            selected_inp.with_suffix(".xyz").write_text(
                "2\ninterrupted optts geometry\nH 0 0 0\nH 0 0 2.15\n",
                encoding="utf-8",
            )
            selected_inp.with_suffix(".out").write_text(
                "ScanTS option: We are already beyond the maximum\nREFINING TS GUESS STRUCTURE\n",
                encoding="utf-8",
            )
            state = new_state(reaction_dir, selected_inp, max_retries=1)
            runner = _CaptureSuccessRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=True,
                runner=runner,
                max_retries=1,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda _payload: None,
            )

            saved = load_state(reaction_dir)
            resume_inp = reaction_dir / "tsopt.resume.inp"
            resume_text = resume_inp.read_text(encoding="utf-8")

        self.assertEqual(rc, 0)
        self.assertEqual(runner.seen, [resume_inp])
        self.assertIn("OPTTS", resume_text)
        self.assertNotIn("ScanTS", resume_text)
        self.assertNotIn("B 4 20 =", resume_text)
        self.assertIn("* xyzfile 0 1 tsopt.xyz", resume_text)
        self.assertIn('%moinp "tsopt.gbw"', resume_text)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertIn(
            "resume_checkpoint_restart_from_tsopt.gbw", saved["attempts"][0]["patch_actions"]
        )
        self.assertIn("resume_scants_resume_to_optts", saved["attempts"][0]["patch_actions"])
        self.assertIn("resume_scants_scan_block_removed", saved["attempts"][0]["patch_actions"])
        self.assertIn(
            "resume_geometry_restart_from_tsopt.xyz", saved["attempts"][0]["patch_actions"]
        )

    def test_resumed_scants_does_not_promote_relaxed_scan_tsopt_xyz_without_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "tsopt.inp"
            selected_inp.write_text(
                "\n".join(
                    [
                        "! ScanTS B3LYP def2-SVP D3BJ CPCM(THF) Freq",
                        "",
                        "%geom",
                        "  MaxIter 200",
                        "  Scan",
                        "    B 4 20 = 1.86, 3.40, 32",
                        "    A 5 6 7 = 90.00, 120.00, 32",
                        "  end",
                        "end",
                        "",
                        "* xyzfile 0 1 input.xyz",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            selected_inp.with_suffix(".gbw").write_bytes(b"checkpoint")
            selected_inp.with_suffix(".xyz").write_text(
                "2\nrelaxed scan geometry\nH 0 0 0\nH 0 0 2.15\n",
                encoding="utf-8",
            )
            for idx in range(1, 5):
                (reaction_dir / f"tsopt.{idx:03d}.xyz").write_text(
                    f"2\nscan step {idx}\nH 0 0 0\nH 0 0 {idx}.0\n",
                    encoding="utf-8",
                )
            selected_inp.with_suffix(".out").write_text(
                "GEOMETRY OPTIMIZATION CYCLE 4\nRELAXED SURFACE SCAN STEP 4\n",
                encoding="utf-8",
            )
            state = new_state(reaction_dir, selected_inp, max_retries=1)
            runner = _CaptureSuccessRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=True,
                runner=runner,
                max_retries=1,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda _payload: None,
            )

            saved = load_state(reaction_dir)
            resume_inp = reaction_dir / "tsopt.resume.inp"
            resume_text = resume_inp.read_text(encoding="utf-8")

        self.assertEqual(rc, 0)
        self.assertEqual(runner.seen, [resume_inp])
        self.assertIn("ScanTS", resume_text)
        self.assertNotIn("OPTTS", resume_text)
        self.assertNotIn("B 4 20 = 1.86, 3.40, 32", resume_text)
        self.assertNotIn("A 5 6 7 = 90.00, 120.00, 32", resume_text)
        self.assertIn("B 4 20 = 2.05870968, 3.40, 28", resume_text)
        self.assertIn("A 5 6 7 = 93.87096774, 120.00, 28", resume_text)
        self.assertIn("* xyzfile 0 1 tsopt.xyz", resume_text)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertIn(
            "resume_checkpoint_restart_from_tsopt.gbw", saved["attempts"][0]["patch_actions"]
        )
        self.assertNotIn("resume_scants_resume_to_optts", saved["attempts"][0]["patch_actions"])
        self.assertIn(
            "resume_scants_scan_range_resumed_after_point_004",
            saved["attempts"][0]["patch_actions"],
        )
        self.assertIn(
            "resume_geometry_restart_from_tsopt.xyz", saved["attempts"][0]["patch_actions"]
        )

    def test_resumed_scants_after_scan_done_uses_highest_surface_xyz_not_tsopt_xyz(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "tsopt.inp"
            selected_inp.write_text(
                "\n".join(
                    [
                        "! ScanTS B3LYP def2-SVP D3BJ CPCM(THF) Freq",
                        "",
                        "%geom",
                        "  MaxIter 200",
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
            selected_inp.with_suffix(".gbw").write_bytes(b"checkpoint")
            selected_inp.with_suffix(".xyz").write_text(
                "2\nsame-stem relaxed scan geometry\nH 0 0 0\nH 0 0 3.00\n",
                encoding="utf-8",
            )
            for idx in range(1, 4):
                (reaction_dir / f"tsopt.{idx:03d}.xyz").write_text(
                    f"2\nscan step {idx}\nH 0 0 0\nH 0 0 {idx}.0\n",
                    encoding="utf-8",
                )
            selected_inp.with_suffix(".out").write_text(
                "\n".join(
                    [
                        "**** RELAXED SURFACE SCAN DONE ***",
                        "RELAXED SURFACE SCAN RESULTS",
                        "The Calculated Surface using the 'Actual Energy'",
                        "   1.86000000 -100.00000000",
                        "   1.91000000 -99.50000000",
                        "   1.96000000 -99.75000000",
                        "The Calculated Surface using the SCF energy",
                    ]
                ),
                encoding="utf-8",
            )
            state = new_state(reaction_dir, selected_inp, max_retries=1)
            runner = _CaptureSuccessRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=True,
                runner=runner,
                max_retries=1,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda _payload: None,
            )

            saved = load_state(reaction_dir)
            resume_inp = reaction_dir / "tsopt.resume.inp"
            resume_text = resume_inp.read_text(encoding="utf-8")

        self.assertEqual(rc, 0)
        self.assertEqual(runner.seen, [resume_inp])
        self.assertIn("OPTTS", resume_text)
        self.assertNotIn("ScanTS", resume_text)
        self.assertNotIn("B 4 20 =", resume_text)
        self.assertIn("* xyzfile 0 1 tsopt.002.xyz", resume_text)
        self.assertNotIn("* xyzfile 0 1 tsopt.xyz", resume_text)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertIn("resume_scants_resume_to_optts", saved["attempts"][0]["patch_actions"])
        self.assertIn(
            "resume_geometry_restart_from_tsopt.002.xyz", saved["attempts"][0]["patch_actions"]
        )

    def test_failed_scants_retries_as_optts_from_highest_surface_xyz(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text(
                "\n".join(
                    [
                        "! ScanTS B3LYP def2-SVP D3BJ CPCM(THF) Freq",
                        "",
                        "%geom",
                        "  MaxIter 200",
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
            state = new_state(reaction_dir, selected_inp, max_retries=1)
            runner = _ScanTsFallbackRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=runner,
                max_retries=1,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda _payload: None,
            )

            saved = load_state(reaction_dir)
            retry_inp = reaction_dir / "rxn.retry01.inp"
            retry_text = retry_inp.read_text(encoding="utf-8")

        self.assertEqual(rc, 0)
        self.assertEqual(runner.seen, [selected_inp, retry_inp])
        self.assertIn("OPTTS", retry_text)
        self.assertNotIn("ScanTS", retry_text)
        self.assertNotIn("B 4 20 =", retry_text)
        self.assertIn("* xyzfile 0 1 rxn.002.xyz", retry_text)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved["status"], "completed")
        self.assertIn("scants_fallback_to_optts", saved["attempts"][0]["patch_actions"])
        self.assertIn("scants_guess_from_rxn.002.xyz", saved["attempts"][0]["patch_actions"])


if __name__ == "__main__":
    unittest.main()
