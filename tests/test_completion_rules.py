import tempfile
import unittest
from pathlib import Path

from orca_auto.orca.completion_rules import detect_completion_mode


class TestCompletionRules(unittest.TestCase):
    def test_detect_ts_and_irc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "rxn.inp"
            inp.write_text(
                "! OptTS Freq IRC\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8"
            )
            mode = detect_completion_mode(inp)
        self.assertEqual(mode.kind, "ts")
        self.assertTrue(mode.require_irc)

    def test_detect_scants_as_ts_without_irc_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "rxn.inp"
            inp.write_text(
                "! ScanTS B3LYP def2-SVP Freq\n* xyzfile 0 1 input.xyz\n",
                encoding="utf-8",
            )
            mode = detect_completion_mode(inp)
        self.assertEqual(mode.kind, "ts")
        self.assertFalse(mode.require_irc)
        self.assertEqual(mode.route_line, "! ScanTS B3LYP def2-SVP Freq")

    def test_detect_opt_without_irc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "rxn.inp"
            inp.write_text("! Opt Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            mode = detect_completion_mode(inp)
        self.assertEqual(mode.kind, "opt")
        self.assertFalse(mode.require_irc)

    def test_detect_completion_mode_skips_blank_and_comment_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "rxn.inp"
            inp.write_text(
                "\n\n# comment line\n   \n! NEB-TS IRC TightSCF\n* xyz 0 1\n",
                encoding="utf-8",
            )
            mode = detect_completion_mode(inp)
        self.assertEqual(mode.kind, "ts")
        self.assertTrue(mode.require_irc)
        self.assertEqual(mode.route_line, "! NEB-TS IRC TightSCF")

    def test_detect_completion_mode_defaults_to_opt_when_no_ts_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "rxn.inp"
            inp.write_text("! SP IRC\n* xyz 0 1\n", encoding="utf-8")
            mode = detect_completion_mode(inp)
        self.assertEqual(mode.kind, "opt")
        self.assertTrue(mode.require_irc)
        self.assertEqual(mode.route_line, "! SP IRC")

    def test_detect_completion_mode_returns_empty_route_when_no_route_found(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "rxn.inp"
            inp.write_text("\n# comment only\n* xyz 0 1\nH 0 0 0\n", encoding="utf-8")
            mode = detect_completion_mode(inp)
        self.assertEqual(mode.kind, "opt")
        self.assertFalse(mode.require_irc)
        self.assertEqual(mode.route_line, "")
