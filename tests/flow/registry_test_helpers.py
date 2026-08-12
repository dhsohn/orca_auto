from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from orca_auto.flow.registry import journal as workflow_journal
from orca_auto.flow.registry import store as registry_store
from orca_auto.flow.registry import worker_state_store


@contextmanager
def _no_lock(*_args: Any, **_kwargs: Any) -> Iterator[None]:
    yield


def patch_file_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_store, "file_lock", _no_lock)
    monkeypatch.setattr(workflow_journal, "file_lock", _no_lock)
    monkeypatch.setattr(worker_state_store, "file_lock", _no_lock)


def patch_now_utc_iso(
    monkeypatch: pytest.MonkeyPatch,
    now_utc_iso: Callable[[], str],
) -> None:
    monkeypatch.setattr(registry_store, "now_utc_iso", now_utc_iso)
    monkeypatch.setattr(workflow_journal, "now_utc_iso", now_utc_iso)
    monkeypatch.setattr(worker_state_store, "now_utc_iso", now_utc_iso)
