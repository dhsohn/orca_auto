from __future__ import annotations

import os
from pathlib import Path

import pytest

from orca_auto.smoke import manifest as smoke_manifest
from orca_auto.smoke._dirfd import (
    PinnedReadError,
    assert_named_regular_file,
    directory_open_flags,
    file_open_flags,
    read_pinned_regular_file,
    stat_identity,
)


def _open_file(directory_fd: int, name: str) -> int:
    return os.open(name, file_open_flags(), dir_fd=directory_fd)


@pytest.fixture
def pinned_dir(tmp_path: Path):
    directory_fd = os.open(tmp_path, directory_open_flags())
    try:
        yield tmp_path, directory_fd
    finally:
        os.close(directory_fd)


def test_read_pinned_regular_file_round_trip(pinned_dir) -> None:
    tmp_path, directory_fd = pinned_dir
    (tmp_path / "payload.json").write_bytes(b'{"ok": true}')
    descriptor = _open_file(directory_fd, "payload.json")
    try:
        payload = read_pinned_regular_file(
            descriptor,
            max_bytes=1024,
            named_location=(directory_fd, "payload.json"),
        )
    finally:
        os.close(descriptor)
    assert payload == b'{"ok": true}'


def test_read_pinned_regular_file_rejects_oversized(pinned_dir) -> None:
    tmp_path, directory_fd = pinned_dir
    (tmp_path / "big.bin").write_bytes(b"x" * 32)
    descriptor = _open_file(directory_fd, "big.bin")
    try:
        with pytest.raises(PinnedReadError) as excinfo:
            read_pinned_regular_file(descriptor, max_bytes=31)
    finally:
        os.close(descriptor)
    assert excinfo.value.reason == "too-large"


def test_read_pinned_regular_file_rejects_hardlinked(pinned_dir) -> None:
    tmp_path, directory_fd = pinned_dir
    (tmp_path / "original.txt").write_bytes(b"payload")
    os.link(tmp_path / "original.txt", tmp_path / "alias.txt")
    descriptor = _open_file(directory_fd, "original.txt")
    try:
        with pytest.raises(PinnedReadError) as excinfo:
            read_pinned_regular_file(descriptor, max_bytes=1024)
    finally:
        os.close(descriptor)
    assert excinfo.value.reason == "multi-link"


def test_read_pinned_regular_file_rejects_identity_mismatch(pinned_dir) -> None:
    tmp_path, directory_fd = pinned_dir
    (tmp_path / "expected.txt").write_bytes(b"expected")
    (tmp_path / "actual.txt").write_bytes(b"actual!!")
    expected = os.stat(tmp_path / "expected.txt")
    descriptor = _open_file(directory_fd, "actual.txt")
    try:
        with pytest.raises(PinnedReadError) as excinfo:
            read_pinned_regular_file(
                descriptor,
                max_bytes=1024,
                expected_identity=stat_identity(expected),
            )
    finally:
        os.close(descriptor)
    assert excinfo.value.reason == "changed-before"


def test_read_pinned_regular_file_rejects_named_substitution(pinned_dir) -> None:
    # The post-read named re-stat is the defense against the file being
    # swapped behind the still-open descriptor.
    tmp_path, directory_fd = pinned_dir
    (tmp_path / "steady.txt").write_bytes(b"steady")
    (tmp_path / "swapped.txt").write_bytes(b"swapped")
    descriptor = _open_file(directory_fd, "steady.txt")
    try:
        with pytest.raises(PinnedReadError) as excinfo:
            read_pinned_regular_file(
                descriptor,
                max_bytes=1024,
                named_location=(directory_fd, "swapped.txt"),
            )
    finally:
        os.close(descriptor)
    assert excinfo.value.reason == "changed-during"


def test_read_pinned_regular_file_reports_truncation_as_short_read(
    pinned_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path, directory_fd = pinned_dir
    (tmp_path / "shrinking.txt").write_bytes(b"content")
    descriptor = _open_file(directory_fd, "shrinking.txt")
    monkeypatch.setattr(os, "read", lambda fd, count: b"")
    try:
        with pytest.raises(PinnedReadError) as excinfo:
            read_pinned_regular_file(descriptor, max_bytes=1024)
    finally:
        os.close(descriptor)
    assert excinfo.value.reason == "short-read"


def test_read_pinned_regular_file_rejects_group_readable_private(pinned_dir) -> None:
    tmp_path, directory_fd = pinned_dir
    private = tmp_path / "private.json"
    private.write_bytes(b"{}")
    os.chmod(private, 0o644)
    descriptor = _open_file(directory_fd, "private.json")
    try:
        with pytest.raises(PinnedReadError) as excinfo:
            read_pinned_regular_file(
                descriptor,
                max_bytes=1024,
                require_owner_private=True,
            )
    finally:
        os.close(descriptor)
    assert excinfo.value.reason == "not-owner-private"


def test_fifo_opens_without_blocking_and_is_rejected(pinned_dir) -> None:
    # file_open_flags carries O_NONBLOCK, so a FIFO swapped in for a regular
    # file opens immediately (no writer needed) and fails the S_ISREG check
    # instead of hanging the reader.
    tmp_path, directory_fd = pinned_dir
    os.mkfifo(tmp_path / "trap.fifo")
    descriptor = _open_file(directory_fd, "trap.fifo")
    try:
        with pytest.raises(PinnedReadError) as excinfo:
            read_pinned_regular_file(descriptor, max_bytes=1024)
    finally:
        os.close(descriptor)
    assert excinfo.value.reason == "not-regular"


def test_assert_named_regular_file_rejects_identity_mismatch(pinned_dir) -> None:
    tmp_path, directory_fd = pinned_dir
    (tmp_path / "one.txt").write_bytes(b"one")
    (tmp_path / "two.txt").write_bytes(b"two")
    expected = os.stat(tmp_path / "two.txt")
    with pytest.raises(PinnedReadError) as excinfo:
        assert_named_regular_file(directory_fd, "one.txt", stat_identity(expected))
    assert excinfo.value.reason == "changed-during"


def test_scanned_read_maps_identity_change_to_value_error(pinned_dir) -> None:
    # The manifest wrapper's contract: a pre-read identity mismatch surfaces
    # as the "changed before" ValueError, never as a raw reason code.
    tmp_path, directory_fd = pinned_dir
    (tmp_path / "case.json").write_bytes(b"{}")
    (tmp_path / "other.json").write_bytes(b"[]")
    stale = os.stat(tmp_path / "other.json")
    with pytest.raises(ValueError, match="changed before it was read"):
        smoke_manifest._read_scanned_regular_file(directory_fd, "case.json", stale)


def test_bounded_json_mapping_refuses_group_readable_file(pinned_dir) -> None:
    tmp_path, directory_fd = pinned_dir
    owner = tmp_path / ".owner.json"
    owner.write_text('{"owner": "me"}', encoding="utf-8")
    os.chmod(owner, 0o600)
    assert smoke_manifest._bounded_json_mapping_at(directory_fd, ".owner.json") == {"owner": "me"}
    os.chmod(owner, 0o640)
    assert smoke_manifest._bounded_json_mapping_at(directory_fd, ".owner.json") is None
