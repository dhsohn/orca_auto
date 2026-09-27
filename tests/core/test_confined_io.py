from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core import confined_io


def test_stable_read_enforces_cumulative_cap_for_growing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "growing.bin"
    source.write_bytes(b"x")
    chunks = iter((b"abc", b"de", b"must-not-be-read"))
    read_sizes: list[int] = []

    def growing_read(_descriptor: int, size: int) -> bytes:
        read_sizes.append(size)
        return next(chunks)

    monkeypatch.setattr(confined_io.os, "read", growing_read)

    with pytest.raises(ValueError, match="exceeds 4 bytes"):
        confined_io.read_stable_regular_file(source, max_bytes=4)

    assert read_sizes == [5, 2]
