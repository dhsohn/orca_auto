from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from _pytest.tmpdir import TempPathFactory

_PINNED_TMP_PATH_ENV = "ORCA_AUTO_SMOKE_PYTEST_TMP_PATH"
_PINNED_TMP_DEVICE_ENV = "ORCA_AUTO_SMOKE_PYTEST_TMP_DEVICE"
_PINNED_TMP_INODE_ENV = "ORCA_AUTO_SMOKE_PYTEST_TMP_INODE"


class _PinnedTempPathFactory(TempPathFactory):  # type: ignore[misc]
    def _ensure_relative_to_basetemp(self, basename: str) -> str:
        normalized = os.path.normpath(basename)
        separators = tuple(item for item in (os.sep, os.altsep) if item)
        if (
            not normalized
            or normalized in {".", ".."}
            or os.path.isabs(normalized)
            or any(separator in normalized for separator in separators)
        ):
            raise ValueError(f"{basename} is not a normalized and relative path")
        return normalized


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """Bind pytest's temp factory to a directory already pinned by the runner."""

    raw_path = os.environ.get(_PINNED_TMP_PATH_ENV, "")
    if not raw_path:
        raise pytest.UsageError(f"{_PINNED_TMP_PATH_ENV} is required")
    try:
        expected = (
            int(os.environ[_PINNED_TMP_DEVICE_ENV]),
            int(os.environ[_PINNED_TMP_INODE_ENV]),
        )
    except (KeyError, ValueError) as exc:
        raise pytest.UsageError("pinned smoke pytest directory identity is invalid") from exc

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
    try:
        descriptor = os.open(raw_path, flags)
    except OSError as exc:
        raise pytest.UsageError("pinned smoke pytest directory is unavailable") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode) or (details.st_dev, details.st_ino) != expected:
            raise pytest.UsageError("pinned smoke pytest directory identity changed")
    finally:
        os.close(descriptor)

    factory = getattr(config, "_tmp_path_factory", None)
    if not isinstance(factory, TempPathFactory):
        raise pytest.UsageError("pytest temporary path factory is unavailable")
    # Do not resolve this procfs path: its purpose is to retain the runner's
    # already-open directory identity even if the advertised namespace moves.
    pinned_factory = _PinnedTempPathFactory(
        given_basetemp=None,
        retention_count=factory._retention_count,
        retention_policy=factory._retention_policy,
        trace=factory._trace,
        basetemp=Path(raw_path),
        _ispytest=True,
    )
    vars(config)["_tmp_path_factory"] = pinned_factory
