from __future__ import annotations

from pathlib import Path

from orca_auto.orca.parser.io import read_orca_text


def test_read_orca_text_handles_empty_files(tmp_path: Path) -> None:
    path = tmp_path / "empty.out"
    path.write_bytes(b"")

    assert read_orca_text(str(path)) == ""


def test_read_orca_text_uses_bom_when_present(tmp_path: Path) -> None:
    utf16_path = tmp_path / "utf16.out"
    utf16_path.write_bytes("energy = -1.0".encode("utf-16"))

    utf8_path = tmp_path / "utf8.out"
    utf8_path.write_bytes("\ufeffnormal termination".encode())

    assert read_orca_text(str(utf16_path)) == "energy = -1.0"
    assert read_orca_text(str(utf8_path)) == "normal termination"


def test_read_orca_text_detects_utf16_without_bom(tmp_path: Path) -> None:
    path = tmp_path / "utf16le_no_bom.out"
    path.write_bytes("SCF CONVERGED".encode("utf-16-le"))

    assert read_orca_text(str(path)) == "SCF CONVERGED"


def test_read_orca_text_replaces_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "invalid.out"
    path.write_bytes(b"valid\xfftext")

    assert read_orca_text(str(path)) == "valid\ufffdtext"
