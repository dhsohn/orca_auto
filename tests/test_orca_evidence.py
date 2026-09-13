"""Final-output evidence reuses one decoded snapshot per cache key."""

from __future__ import annotations

import builtins
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

import pytest

from orca_auto.orca import evidence
from orca_auto.orca.frequencies import parse_frequency_analysis
from orca_auto.orca.parser import parse_orca_output

_ENERGY = "FINAL SINGLE POINT ENERGY -1.0\n"
_FREQUENCIES = "VIBRATIONAL FREQUENCIES\n0: -410.20 cm**-1\n1: 100.00 cm**-1\n"


@pytest.fixture(autouse=True)
def _empty_evidence_cache() -> Iterator[None]:
    evidence._parsed_output_cached.cache_clear()
    yield
    evidence._parsed_output_cached.cache_clear()


def _record_output_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    reads: list[tuple[str, int]] = []
    original_open = builtins.open

    class CountedFile:
        def __init__(self, handle: Any, path: str) -> None:
            self.handle = handle
            self.path = path

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: Any) -> Any:
            return self.handle.__exit__(*args)

        def read(self, *args: Any) -> bytes:
            contents: bytes = self.handle.read(*args)
            reads.append((self.path, len(contents)))
            return contents

    def counted_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = original_open(file, mode, *args, **kwargs)
        if mode == "rb" and str(file).endswith(".out"):
            return CountedFile(handle, str(file))
        return handle

    monkeypatch.setattr(builtins, "open", counted_open)
    return reads


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le"])
@pytest.mark.parametrize("contents", [_ENERGY + _FREQUENCIES, _FREQUENCIES + _ENERGY, ""])
def test_cached_output_reads_once_and_preserves_both_parsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoding: str, contents: str
) -> None:
    out = tmp_path / "final.out"
    encoded = contents.encode(encoding)
    out.write_bytes(encoded)
    expected = parse_orca_output(str(out)), parse_frequency_analysis(out)
    reads = _record_output_reads(monkeypatch)

    actual = evidence.parsed_final_output(out)

    assert actual == expected
    assert actual[0].source_path == str(out)
    assert evidence.parsed_final_output(out) is actual
    assert reads == [(str(out), len(encoded))]


@pytest.mark.parametrize("changed_key", ["path", "mtime_ns", "size"])
def test_cached_output_invalidates_each_file_identity_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_key: str
) -> None:
    out = tmp_path / "first.out"
    out.write_text(_ENERGY, encoding="utf-8")
    os.utime(out, ns=(1_000_000_000, 1_000_000_000))
    reads = _record_output_reads(monkeypatch)
    first = evidence.parsed_final_output(out)
    replacement = _ENERGY.replace("-1.0", "-2.0")
    if changed_key == "path":
        out = tmp_path / "other.out"
    elif changed_key == "size":
        replacement += "\n"
    out.write_text(replacement, encoding="utf-8")
    mtime = 2_000_000_000 if changed_key == "mtime_ns" else 1_000_000_000
    os.utime(out, ns=(mtime, mtime))

    second = evidence.parsed_final_output(out)

    assert first[0].energy_hartree == -1.0
    assert second[0].energy_hartree == -2.0
    assert second[0].source_path == str(out)
    assert second is not first
    assert evidence.parsed_final_output(out) is second
    assert len(reads) == 2


def test_cached_output_retains_the_32_most_recent_file_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [tmp_path / f"version_{index}.out" for index in range(33)]
    for path in paths:
        path.write_text(_ENERGY, encoding="utf-8")
    reads = _record_output_reads(monkeypatch)
    pairs = [evidence.parsed_final_output(path) for path in paths]

    assert evidence.parsed_final_output(paths[1]) is pairs[1]
    assert evidence.parsed_final_output(paths[-1]) is pairs[-1]
    assert evidence.parsed_final_output(paths[0]) == pairs[0]
    assert evidence.parsed_final_output(paths[0]) is not pairs[0]
    assert len(reads) == 34


def test_missing_output_preserves_file_api_errors(tmp_path: Path) -> None:
    out = tmp_path / "missing.out"

    with pytest.raises(FileNotFoundError) as result_error:
        parse_orca_output(str(out))
    assert result_error.value.filename == str(out)
    assert parse_frequency_analysis(out) is None
    with pytest.raises(FileNotFoundError) as evidence_error:
        evidence.parsed_final_output(out)
    assert evidence_error.value.filename == str(out)


def test_unreadable_output_preserves_errors_and_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "unreadable.out"
    out.write_text(_ENERGY + _FREQUENCIES, encoding="utf-8")
    read_error = PermissionError(13, "synthetic permission denied", str(out))
    original_open = builtins.open

    def denied_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if str(file) == str(out):
            raise read_error
        return original_open(file, *args, **kwargs)

    with monkeypatch.context() as denied:
        denied.setattr(builtins, "open", denied_open)
        with pytest.raises(PermissionError) as result_error:
            parse_orca_output(str(out))
        assert result_error.value is read_error
        assert parse_frequency_analysis(out) is None
        with pytest.raises(PermissionError) as evidence_error:
            evidence.parsed_final_output(out)
        assert evidence_error.value is read_error

    reads = _record_output_reads(monkeypatch)
    recovered = evidence.parsed_final_output(out)
    assert recovered[0].energy_hartree == -1.0
    assert recovered[1] is not None
    assert recovered[1].frequencies == (-410.20, 100.0)
    assert evidence.parsed_final_output(out) is recovered
    assert len(reads) == 1
