from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from orca_auto.orca.attempt_engine import run_attempts
from orca_auto.orca.state import load_state, new_state


class _ScanTsFallbackRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

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
