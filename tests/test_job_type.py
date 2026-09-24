from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.job_type import detect_job_type


def _inp(tmp_path: Path, route: str) -> Path:
    path = tmp_path / "rxn.inp"
    path.write_text(f"{route}\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        ("! OptTS Freq", "ts"),
        ("! NEB-TS", "ts"),
        ("! Opt Freq", "opt"),
        ("! SP def2-SVP", "sp"),
        ("! Energy", "sp"),
        ("! Freq", "freq"),
        ("! NumFreq", "freq"),
        ("! AnFreq", "freq"),
        ("! B3LYP def2-SVP", "other"),
        # OptTS must never be classified as a plain optimization.
        ("! OptTS IRC", "ts"),
        ("# hidden # ! Freq", "freq"),
    ],
)
def test_route_classification(tmp_path: Path, route: str, expected: str) -> None:
    assert detect_job_type(_inp(tmp_path, route)) == expected


def test_comment_and_blank_lines_skipped(tmp_path: Path) -> None:
    path = tmp_path / "rxn.inp"
    path.write_text("# comment\n\n! Opt Freq\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    assert detect_job_type(path) == "opt"


def test_missing_file_is_other(tmp_path: Path) -> None:
    assert detect_job_type(tmp_path / "missing.inp") == "other"
