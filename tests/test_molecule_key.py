from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.molecule_key import (
    _atoms_to_hill_formula,
    _directory_name_fallback,
    _find_user_tag,
    _parse_formula_from_inp,
    _parse_xyz_file,
    _sanitize_key,
    resolve_molecule_key,
)


def _inp(tmp_path: Path, text: str) -> Path:
    inp = tmp_path / "rxn.inp"
    inp.write_text(text)
    return inp


def _xyz(tmp_path: Path, text: str) -> Path:
    xyz = tmp_path / "mol.xyz"
    xyz.write_text(text)
    return xyz


# --- user tag -------------------------------------------------------------------------------


def test_finds_tag(tmp_path: Path) -> None:
    assert _find_user_tag(_inp(tmp_path, "# TAG: my_molecule\n! Opt\n* xyz 0 1\nH 0 0 0\n*\n")) == (
        "my_molecule"
    )


def test_sanitizes_special_chars(tmp_path: Path) -> None:
    assert _find_user_tag(_inp(tmp_path, "# TAG: my molecule/v2\n! Opt\n")) == "my_molecule_v2"


def test_returns_none_when_no_tag(tmp_path: Path) -> None:
    assert _find_user_tag(_inp(tmp_path, "! Opt\n* xyz 0 1\nH 0 0 0\n*\n")) is None


def test_case_insensitive(tmp_path: Path) -> None:
    assert _find_user_tag(_inp(tmp_path, "# tag: MyTag\n! Opt\n")) == "MyTag"


# --- formula from input ---------------------------------------------------------------------


def test_inline_xyz(tmp_path: Path) -> None:
    inp = _inp(tmp_path, "! Opt\n* xyz 0 1\nC 0 0 0\nC 1 0 0\nH 2 0 0\nH 3 0 0\nO 4 0 0\n*\n")
    assert _parse_formula_from_inp(inp) == "C2H2O"


def test_xyzfile_reference(tmp_path: Path) -> None:
    _xyz(tmp_path, "4\ncomment\nC 0 0 0\nH 1 0 0\nH 2 0 0\nH 3 0 0\n")
    assert _parse_formula_from_inp(_inp(tmp_path, "! Opt\n* xyzfile 0 1 mol.xyz\n")) == "CH3"


def test_xyzfile_missing_returns_none(tmp_path: Path) -> None:
    assert _parse_formula_from_inp(_inp(tmp_path, "! Opt\n* xyzfile 0 1 nonexistent.xyz\n")) is None


def test_no_geometry_block(tmp_path: Path) -> None:
    assert _parse_formula_from_inp(_inp(tmp_path, "! Opt\n")) is None


# --- xyz file -------------------------------------------------------------------------------


def test_standard_xyz(tmp_path: Path) -> None:
    xyz = _xyz(tmp_path, "3\ncomment line\nO 0.0 0.0 0.0\nH 0.0 0.0 1.0\nH 0.0 1.0 0.0\n")
    assert _parse_xyz_file(xyz) == ["O", "H", "H"]


def test_missing_file() -> None:
    assert _parse_xyz_file(Path("/nonexistent/mol.xyz")) == []


# --- Hill formula ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("atoms", "expected"),
    [
        pytest.param(["O", "H", "H", "C"], "CH2O", id="carbon_first"),
        pytest.param(["Na", "Cl"], "ClNa", id="no_carbon_alphabetical"),
        pytest.param([], None, id="empty_returns_none"),
        pytest.param(["C"], "C", id="single_element_no_count"),
        pytest.param(["C", "C", "C"], "C3", id="multiple_same"),
        pytest.param(["C"] * 8 + ["H"] * 10 + ["O"] * 2, "C8H10O2", id="complex_molecule"),
    ],
)
def test_hill_formula(atoms: list[str], expected: str | None) -> None:
    assert _atoms_to_hill_formula(atoms) == expected


# --- key sanitizing -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("C8H10O2", "C8H10O2", id="safe_string"),
        pytest.param("my molecule", "my_molecule", id="spaces_replaced"),
        pytest.param("path/to/mol", "path_to_mol", id="slashes_replaced"),
        pytest.param("", "unknown", id="empty_returns_unknown"),
        pytest.param("///", "unknown", id="all_special_returns_unknown"),
    ],
)
def test_sanitize_key(raw: str, expected: str) -> None:
    assert _sanitize_key(raw) == expected


# --- directory fallback and resolution ------------------------------------------------------


def test_uses_parent_name(tmp_path: Path) -> None:
    directory = tmp_path / "Int1_DMSO"
    directory.mkdir()
    assert _directory_name_fallback(_inp(directory, "! Opt\n")) == "Int1_DMSO"


def test_tag_takes_priority(tmp_path: Path) -> None:
    inp = _inp(tmp_path, "# TAG: custom_name\n! Opt\n* xyz 0 1\nC 0 0 0\n*\n")
    assert resolve_molecule_key(inp).key == "custom_name"


def test_formula_when_no_tag(tmp_path: Path) -> None:
    assert (
        resolve_molecule_key(_inp(tmp_path, "! Opt\n* xyz 0 1\nC 0 0 0\nH 1 0 0\n*\n")).key == "CH"
    )


def test_dirname_when_no_geometry(tmp_path: Path) -> None:
    directory = tmp_path / "TS1_acetone"
    directory.mkdir()
    assert resolve_molecule_key(_inp(directory, "! Opt\n")).key == "TS1_acetone"


# --- xyz parser fails closed ----------------------------------------------------------------
# A silently skipped or missing atom line would yield a plausible but wrong
# Hill formula; the parser must yield no atoms instead so the molecule key
# falls back to honest directory-name provenance.


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("3\ncomment\nO 0.0 0.0 0.0\nH 0.0 0.0 1.0\n", id="truncated_file"),
        pytest.param("not-a-count\ncomment\nO 0.0 0.0 0.0\n", id="invalid_count_header"),
        pytest.param("2\ncomment\nO 0.0 0.0 0.0\n???GARBAGE\n", id="corrupt_atom_line"),
    ],
)
def test_parse_xyz_file_fails_closed(tmp_path: Path, text: str) -> None:
    assert _parse_xyz_file(_xyz(tmp_path, text)) == []


# --- inline geometry comments ---------------------------------------------------------------
# The formula parser shares the comment-aware geometry scanner, so ORCA
# comment lines inside ``* xyz ... *`` are neither atoms nor parse errors.


def test_comment_lines_inside_inline_xyz_are_not_atoms(tmp_path: Path) -> None:
    inp = _inp(
        tmp_path,
        "! Opt\n* xyz 0 1 # neutral singlet\n# fragment A\nC 0 0 0\n"
        "C 1 0 0 # note\n  # fragment B #\nH 2 0 0\nH 3 0 0\nO 4 0 0\n* # done\n",
    )
    assert _parse_formula_from_inp(inp) == "C2H2O"


def test_xyzfile_reference_ignores_trailing_comment(tmp_path: Path) -> None:
    _xyz(tmp_path, "2\ncomment\nC 0 0 0\nH 1 0 0\n")
    inp = _inp(tmp_path, '! Opt\n* xyzfile 0 1 "mol.xyz" # geometry\n')
    assert _parse_formula_from_inp(inp) == "CH"
