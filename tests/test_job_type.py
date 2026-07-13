import tempfile
import unittest
from pathlib import Path

from orca_auto.orca.job_type import detect_job_type


class TestDetectJobType(unittest.TestCase):
    def _inp(self, td: str, route: str) -> Path:
        p = Path(td) / "rxn.inp"
        p.write_text(f"{route}\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
        return p

    def test_optts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! OptTS Freq")), "ts")

    def test_neb_ts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! NEB-TS")), "ts")

    def test_opt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! Opt Freq")), "opt")

    def test_sp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! SP def2-SVP")), "sp")

    def test_energy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! Energy")), "sp")

    def test_freq(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! Freq")), "freq")

    def test_numfreq(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! NumFreq")), "freq")

    def test_anfreq(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! AnFreq")), "freq")

    def test_other(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! B3LYP def2-SVP")), "other")

    def test_optts_not_classified_as_opt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! OptTS IRC")), "ts")

    def test_comment_and_blank_lines_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rxn.inp"
            p.write_text(
                "# comment\n\n! Opt Freq\n* xyz 0 1\nH 0 0 0\n*\n",
                encoding="utf-8",
            )
            self.assertEqual(detect_job_type(p), "opt")

    def test_route_after_closed_comment_is_classified(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "# hidden # ! Freq")), "freq")

    def test_missing_file_is_other(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(Path(td) / "missing.inp"), "other")


if __name__ == "__main__":
    unittest.main()
