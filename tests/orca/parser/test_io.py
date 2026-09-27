from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.parser.io import (
    ENCODING_SNIFF_BYTES,
    detect_orca_encoding,
    open_orca_text,
    read_orca_text,
)


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


def test_open_orca_text_streams_the_same_lines_read_orca_text_decodes(tmp_path: Path) -> None:
    text = "energy = -1.0\r\nSCF CONVERGED\nvalid\ufffdtext\n"
    files = {
        "utf16_bom.out": text.encode("utf-16"),
        "utf8_bom.out": ("\ufeff" + text).encode("utf-8"),
        "utf16le_no_bom.out": text.encode("utf-16-le"),
        "invalid_utf8.out": text.replace("\ufffd", "\xff").encode("utf-8", errors="ignore")
        + b"\xff",
    }
    for name, raw in files.items():
        path = tmp_path / name
        path.write_bytes(raw)
        whole = read_orca_text(str(path))
        with open_orca_text(path) as handle:
            streamed = list(handle)
        # The stream applies universal newlines; the whole read keeps the CRLF.
        assert "".join(streamed) == whole.replace("\r\n", "\n"), name
        assert streamed[1] == "SCF CONVERGED\n", name


def test_open_orca_text_decides_the_codec_from_the_prefix_only(tmp_path: Path) -> None:
    # A BOM-less UTF-16 output longer than the sniff window must still stream
    # entirely under the codec chosen from its first bytes.
    lines = [f"ordinary output line {index}" for index in range(6000)] + ["ORCA TERMINATED"]
    text = "\n".join(lines) + "\n"
    raw = text.encode("utf-16-le")
    assert len(raw) > ENCODING_SNIFF_BYTES
    path = tmp_path / "long_utf16le.out"
    path.write_bytes(raw)

    assert detect_orca_encoding(raw[:ENCODING_SNIFF_BYTES]) == "utf-16-le"
    with open_orca_text(path) as handle:
        streamed = list(handle)
    assert streamed[-1] == "ORCA TERMINATED\n"
    assert len(streamed) == len(lines)
    assert "".join(streamed) == read_orca_text(str(path))


def test_open_orca_text_propagates_open_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        open_orca_text(tmp_path / "missing.out")


def test_detect_orca_encoding_keeps_utf8_with_a_nul_run_in_the_prefix() -> None:
    # A zero-filled hole (crash, sparse write) puts NUL bytes on both parities;
    # only a UTF-16 sample confines them to one parity.
    text = b"ORCA TERMINATED NORMALLY\n" * 400
    prefix = b"\x00" * 20 * 1024 + text
    assert detect_orca_encoding(prefix[:ENCODING_SNIFF_BYTES]) == "utf-8"
    assert detect_orca_encoding((b"\x00" * 2048 + text)[:ENCODING_SNIFF_BYTES]) == "utf-8"


def test_detect_orca_encoding_reads_bomless_utf16_in_either_byte_order() -> None:
    text = "ORCA TERMINATED NORMALLY\n" * 400
    assert detect_orca_encoding(text.encode("utf-16-le")) == "utf-16-le"
    assert detect_orca_encoding(text.encode("utf-16-be")) == "utf-16-be"
