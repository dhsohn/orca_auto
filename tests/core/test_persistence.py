from __future__ import annotations

import errno
import fcntl
import os
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.utils import persistence

FIXED_NOW = datetime(2026, 4, 19, 12, 34, 56, tzinfo=UTC)


@pytest.mark.parametrize("relative", [False, True])
def test_open_pinned_readonly_pins_bytes_and_leaves_fd_owned_by_caller(
    tmp_path: Path, relative: bool
) -> None:
    path = tmp_path / "input"
    path.write_bytes(b"original")
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        descriptor = persistence.open_pinned_readonly(
            path.name if relative else path, dir_fd=directory_fd if relative else None
        )
        try:
            assert not os.get_inheritable(descriptor)
            flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
            assert flags & os.O_ACCMODE == os.O_RDONLY
            assert flags & os.O_NONBLOCK
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"replacement")
            replacement.replace(path)
            assert os.read(descriptor, 100) == b"original"
            with pytest.raises(OSError) as failure:
                os.write(descriptor, b"overwrite")
            assert failure.value.errno == errno.EBADF
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("relative", [False, True])
def test_open_pinned_readonly_rejects_final_symlink(tmp_path: Path, relative: bool) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"target")
    link = tmp_path / "link"
    link.symlink_to(target)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError) as failure:
            persistence.open_pinned_readonly(
                link.name if relative else link, dir_fd=directory_fd if relative else None
            )
        assert failure.value.errno == errno.ELOOP
    finally:
        os.close(directory_fd)


class _FixedDatetime:
    @classmethod
    def now(cls, tz: timezone | None = None) -> datetime:
        assert tz is UTC
        return FIXED_NOW


def test_now_utc_iso_returns_utc_isoformat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(persistence, "datetime", _FixedDatetime)

    assert persistence.now_utc_iso() == "2026-04-19T12:34:56+00:00"


def test_timestamped_token_uses_timestamp_and_token_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(persistence, "datetime", _FixedDatetime)
    requested_bytes: list[int] = []

    def fake_token_hex(token_bytes: int) -> str:
        requested_bytes.append(token_bytes)
        return "abc123"

    monkeypatch.setattr(persistence, "token_hex", fake_token_hex)

    assert persistence.timestamped_token("job") == "job_20260419_123456_abc123"
    assert requested_bytes == [16]


def test_timestamped_token_pattern_matches_only_tokens_the_producer_mints() -> None:
    token = persistence.timestamped_token("snapshot_intent", token_bytes=16)
    pattern = persistence.timestamped_token_pattern("snapshot_intent", token_bytes=16)

    assert pattern.fullmatch(token) is not None
    assert pattern.fullmatch(f" {token} ") is None
    assert pattern.fullmatch(token.replace("snapshot_intent", "snapshot-intent")) is None
    assert pattern.fullmatch(token[:-1]) is None
    assert persistence.timestamped_token_pattern("snapshot.intent").fullmatch(token) is None
    assert (
        persistence.timestamped_token_pattern("snapshot_intent", token_bytes=8).fullmatch(token)
        is None
    )


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        ("7", 0, 7),
        (3.9, 0, 3),
        ("bad", 11, 11),
        (None, -1, -1),
    ],
)
def test_coerce_int(value: Any, default: int, expected: int) -> None:
    assert persistence.coerce_int(value, default=default) == expected


def test_resolve_root_path_expands_user_and_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = home / "chem-core-root"
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert persistence.resolve_root_path("~/chem-core-root/..") == home.resolve()
    assert persistence.resolve_root_path(root) == root.resolve()


def test_atomic_write_text_success_path(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "payload.txt"

    persistence.atomic_write_text(path, "hello")
    persistence.atomic_write_text(path, "updated")

    assert path.parent.exists()
    assert path.read_text(encoding="utf-8") == "updated"


def test_atomic_write_json_success_path(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "payload.json"

    persistence.atomic_write_json(
        path,
        {"b": 1, "a": [1, 2], "text": "café"},
    )

    assert path.parent.exists()
    assert path.read_text(encoding="utf-8") == (
        '{\n  "b": 1,\n  "a": [\n    1,\n    2\n  ],\n  "text": "caf\\u00e9"\n}'
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_atomic_write_json_rejects_nonfinite_numbers(tmp_path: Path, value: float) -> None:
    path = tmp_path / "payload.json"

    with pytest.raises(ValueError, match="Out of range float values"):
        persistence.atomic_write_json(path, {"value": value})

    assert not path.exists()


def test_atomic_write_json_fsyncs_parent_dir_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "nested" / "payload.json"
    events: list[str] = []
    original_replace = os.replace

    def fake_replace(src: Path, dst: Path) -> None:
        events.append("replace")
        original_replace(src, dst)

    def fake_fsync_parent_dir(synced_path: Path) -> None:
        events.append("fsync_parent")
        assert synced_path == path
        assert path.exists()

    monkeypatch.setattr(persistence.os, "replace", fake_replace)
    monkeypatch.setattr(persistence, "_fsync_parent_dir", fake_fsync_parent_dir)

    persistence.atomic_write_json(path, {"ok": True})

    assert events == ["replace", "fsync_parent"]


def test_fsync_parent_dir_opens_fsyncs_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "payload.json"
    events: list[tuple[str, int | Path]] = []

    def fake_open(target: str | Path, flags: int) -> int:
        events.append(("open", Path(target)))
        expected_flags = os.O_RDONLY | os.O_DIRECTORY
        assert flags == expected_flags
        return 42

    def fake_fsync(fd: int) -> None:
        events.append(("fsync", fd))

    def fake_close(fd: int) -> None:
        events.append(("close", fd))

    monkeypatch.setattr(persistence.os, "open", fake_open)
    monkeypatch.setattr(persistence.os, "fsync", fake_fsync)
    monkeypatch.setattr(persistence.os, "close", fake_close)

    persistence._fsync_parent_dir(path)

    assert events == [("open", tmp_path), ("fsync", 42), ("close", 42)]


def test_fsync_parent_dir_propagates_open_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "payload.json"

    def fake_open(target: str | Path, flags: int) -> int:
        raise OSError(errno.EINVAL, "directory open refused")

    def fail_fsync(fd: int) -> None:
        raise AssertionError("fsync must not run when opening the directory fails")

    monkeypatch.setattr(persistence.os, "open", fake_open)
    monkeypatch.setattr(persistence.os, "fsync", fail_fsync)

    with pytest.raises(OSError):
        persistence._fsync_parent_dir(path)


def test_fsync_parent_dir_closes_after_fsync_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "payload.json"
    closed: list[int] = []

    def fake_open(target: str | Path, flags: int) -> int:
        return 42

    def fake_fsync(fd: int) -> None:
        raise OSError(errno.EIO, "directory fsync failed")

    def fake_close(fd: int) -> None:
        closed.append(fd)

    monkeypatch.setattr(persistence.os, "open", fake_open)
    monkeypatch.setattr(persistence.os, "fsync", fake_fsync)
    monkeypatch.setattr(persistence.os, "close", fake_close)

    # A durability barrier that cannot be established is reported, not skipped:
    # callers compensate for a write that may already be visible.
    with pytest.raises(OSError):
        persistence._fsync_parent_dir(path)

    assert closed == [42]


def test_atomic_write_json_cleans_up_tmp_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "nested" / "payload.json"
    created_tmp: list[Path] = []

    def fake_mkstemp(prefix: str, suffix: str, dir: str) -> tuple[int, str]:
        tmp_path_local = Path(dir) / f"{prefix}tmp{suffix}"
        fd = os.open(tmp_path_local, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        created_tmp.append(tmp_path_local)
        return fd, str(tmp_path_local)

    def fake_replace(src: Path, dst: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(persistence.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(persistence.os, "replace", fake_replace)

    with pytest.raises(OSError, match="replace failed"):
        persistence.atomic_write_json(path, {"ok": True})

    assert created_tmp
    assert not created_tmp[0].exists()
    assert not path.exists()


def test_atomic_write_json_swallows_unlink_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "nested" / "payload.json"
    created_tmp: list[Path] = []
    original_unlink = Path.unlink

    def fake_mkstemp(prefix: str, suffix: str, dir: str) -> tuple[int, str]:
        tmp_path_local = Path(dir) / f"{prefix}tmp{suffix}"
        fd = os.open(tmp_path_local, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        created_tmp.append(tmp_path_local)
        return fd, str(tmp_path_local)

    def fake_replace(src: Path, dst: Path) -> None:
        raise OSError("replace failed")

    def fake_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if created_tmp and self == created_tmp[0]:
            original_unlink(self, *args, **kwargs)
            raise OSError("unlink failed")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(persistence.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(persistence.os, "replace", fake_replace)
    monkeypatch.setattr(Path, "unlink", fake_unlink)

    with pytest.raises(OSError, match="replace failed"):
        persistence.atomic_write_json(path, {"ok": True})

    assert created_tmp
    assert not created_tmp[0].exists()
    assert not path.exists()
