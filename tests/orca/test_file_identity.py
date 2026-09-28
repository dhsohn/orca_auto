"""The content identity of a pinned regular file."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca import file_identity
from orca_auto.orca.file_identity import file_content_identity


class _OsWithRead:
    """``os`` as ``file_identity`` sees it, with ``read`` replaced."""

    def __init__(self, read: Callable[[int, int], bytes]) -> None:
        self.read = read

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


def test_identity_is_the_resolved_path_digest_and_size(tmp_path: Path) -> None:
    target = tmp_path / "input.inp"
    target.write_bytes(b"! SP\n")
    link = tmp_path / "link.inp"
    link.symlink_to(target)

    assert file_content_identity(link) == {
        "path": str(target.resolve()),
        "sha256": hashlib.sha256(b"! SP\n").hexdigest(),
        "size_bytes": 5,
    }


def test_identity_refuses_a_file_that_is_not_regular(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="File is not a regular file"):
        file_content_identity(fifo)
    with pytest.raises(ValueError, match="Engine executable is not a regular file"):
        file_content_identity(fifo, label="Engine executable")


def test_identity_refuses_a_file_that_changes_while_it_is_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "output.out"
    target.write_bytes(b"x" * 16)
    appended: list[bool] = []

    def read_then_append(descriptor: int, size: int) -> bytes:
        chunk = os.read(descriptor, size)
        if not appended:
            appended.append(True)
            with target.open("ab") as handle:
                handle.write(b"more")
        return chunk

    monkeypatch.setattr(file_identity, "os", _OsWithRead(read_then_append))

    with pytest.raises(ValueError, match="File changed while it was hashed"):
        file_content_identity(target)


def test_identity_refuses_a_read_that_does_not_cover_the_file_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file's status stays the same; only the bytes read fall short of its
    # size, as when a file reports a size its reads do not return.
    target = tmp_path / "output.out"
    target.write_bytes(b"x" * 16)
    monkeypatch.setattr(file_identity, "os", _OsWithRead(lambda _descriptor, _size: b""))

    with pytest.raises(ValueError, match="File changed while it was hashed"):
        file_content_identity(target)
