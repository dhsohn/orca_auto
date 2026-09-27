"""Fixtures for the ORCA queue worker tests: fake children and configured workers."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.admission import store as admission_store
from orca_auto.core.queue import processes as queue_processes
from orca_auto.orca.config import AppConfig
from orca_auto.orca.queue.worker import OrcaQueueWorker
from tests.process_helpers import FakeManagedProcess
from tests.queue_worker_helpers import ChildStarter, FakeChildren, SpawnCall

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_children(monkeypatch: pytest.MonkeyPatch) -> FakeChildren:
    children = FakeChildren()
    real_start_ticks = admission_store._process_start_ticks

    def start_ticks(pid: int) -> int | None:
        # A fake pid has no /proc entry; give it a stable identity so the
        # admission store can attach it as a slot owner. Real pids stay real.
        return pid if pid in children.by_pid else real_start_ticks(pid)

    real_kill = os.kill

    def kill(pid: int, signum: int) -> None:
        # The admission store probes slot owners with ``os.kill(pid, 0)``; a
        # fake child is one process, so signalling it is signalling its group.
        if pid not in children.by_pid:
            real_kill(pid, signum)
            return
        children.killpg(pid, signum)

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", children.killpg)
    monkeypatch.setattr(queue_processes, "_pid_exists", children.pid_exists)
    monkeypatch.setattr(admission_store, "_process_start_ticks", start_ticks)
    return children


@pytest.fixture
def child_starter(fake_children: FakeChildren) -> ChildStarter:
    return ChildStarter(fake_children)


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch, fake_children: FakeChildren) -> list[SpawnCall]:
    """Replace ``subprocess.Popen`` (the spawn itself) and record what the worker launched."""

    calls: list[SpawnCall] = []

    def popen(args: list[str], **kwargs: Any) -> FakeManagedProcess:
        fileno = getattr(kwargs.get("stdout"), "fileno", None)
        log_path = Path(os.readlink(f"/proc/self/fd/{fileno()}")) if fileno else None
        process = fake_children.spawn()
        calls.append(SpawnCall(list(args), log_path, process))
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    return calls


@pytest.fixture
def sleeping_child() -> Iterator[Callable[[], subprocess.Popen[bytes]]]:
    """Spawn real ``sleep`` children in their own session; whatever survives is killed."""

    children: list[subprocess.Popen[bytes]] = []

    def spawn() -> subprocess.Popen[bytes]:
        process = subprocess.Popen(["sleep", "60"], start_new_session=True)
        children.append(process)
        return process

    yield spawn
    for process in children:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.fixture
def worker_cfg(app_cfg: Callable[..., AppConfig], queue_root: Path) -> AppConfig:
    return app_cfg(runs_root=queue_root)


@pytest.fixture
def make_worker(
    worker_cfg: AppConfig,
    queue_root: Path,
    preserved_signals: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., OrcaQueueWorker]:
    """Factory: ``make_worker(max_concurrent=2, cfg=None, start=None, sleep=None)``."""

    del preserved_signals

    def factory(
        *,
        max_concurrent: int = 2,
        cfg: AppConfig | None = None,
        start: ChildStarter | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> OrcaQueueWorker:
        config_path = str(queue_root / "config.yaml")
        worker = OrcaQueueWorker(
            cfg or worker_cfg, config_path, max_concurrent=max_concurrent, sleep_fn=sleep
        )
        if start is not None:
            monkeypatch.setattr(worker, "_start_background_process", start)
        return worker

    return factory


@pytest.fixture
def worker(make_worker: Callable[..., OrcaQueueWorker]) -> OrcaQueueWorker:
    return make_worker()
