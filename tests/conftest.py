"""Shared test foundation: fixtures and the plain builders behind them.

Pytest-style tests take the fixtures; ``unittest.TestCase`` files import the
plain builders (``make_app_cfg``, ``write_fake_orca``, ``write_config_file``,
``make_queue_entry``, ``write_run_state``) directly from this module.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from orca_auto.core.config import CommonResourceConfig, MessengerConfig
from orca_auto.core.config.schema import DiscordConfig
from orca_auto.core.messaging.channel import SendResult
from orca_auto.core.queue import store as _core_queue_store
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    queue_record_sync_metadata,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.queue.worker.admission import select_next_claimable_entry
from orca_auto.core.utils.persistence import timestamped_token
from orca_auto.orca.attempt.reporting import build_final_result
from orca_auto.orca.config import AppConfig, OrcaRuntimeConfig, PathsConfig
from orca_auto.orca.queue import worker_tracking
from orca_auto.orca.queue.adapter import dequeue_entry_if_pending, list_queue, worker_log_path
from orca_auto.orca.queue.entries import (
    QUEUE_APP_NAME,
    QUEUE_ENGINE,
    QUEUE_TASK_KIND,
    entry_metadata,
    queue_entry_is_retired_workflow_owned,
)
from orca_auto.orca.queue.roots import accept_orca_entry
from orca_auto.orca.scratch_config import ScratchConfig
from orca_auto.orca.state import finalize_state, new_state, write_state
from orca_auto.orca.statuses import (
    TERMINAL_RUN_STATUSES,
    AnalyzerStatus,
    RunStatus,
    coerce_run_status,
)
from orca_auto.orca.types import AttemptRecord, RunFinalResult, RunState
from tests.process_helpers import preserved_signal_handlers

# ---------------------------------------------------------------------------
# fsync
# ---------------------------------------------------------------------------


def _no_sync(_fd: int) -> None:
    return None


@pytest.fixture(autouse=True)
def no_fsync(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn ``os.fsync``/``os.fdatasync`` into no-ops; ``@pytest.mark.real_fsync`` opts out."""

    if request.node.get_closest_marker("real_fsync") is not None:
        return
    monkeypatch.setattr(os, "fsync", _no_sync)
    monkeypatch.setattr(os, "fdatasync", _no_sync, raising=False)


# ---------------------------------------------------------------------------
# Fake ORCA executable
# ---------------------------------------------------------------------------

FAKE_ORCA_SCRIPT = "#!/bin/sh\nexit 0\n"


def write_fake_orca(path: Path, script: str = FAKE_ORCA_SCRIPT) -> Path:
    """Write an executable stand-in for ORCA at ``path`` and return it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def make_fake_orca(tmp_path: Path) -> Callable[..., Path]:
    """Factory: ``make_fake_orca(script=..., name="fake_orca") -> Path`` under ``tmp_path``."""

    def factory(script: str = FAKE_ORCA_SCRIPT, *, name: str = "fake_orca") -> Path:
        return write_fake_orca(tmp_path / name, script)

    return factory


@pytest.fixture
def fake_orca(make_fake_orca: Callable[..., Path]) -> Path:
    """``tmp_path/fake_orca``: ``#!/bin/sh`` that exits 0, mode 0o755."""

    return make_fake_orca()


# ---------------------------------------------------------------------------
# AppConfig and orca_auto.yaml
# ---------------------------------------------------------------------------


def make_app_cfg(
    runs_root: str | Path,
    *,
    orca_executable: str | Path = "",
    max_concurrent: int | None = None,
    admission_root: str | Path | None = None,
    admission_limit: int | None = None,
    resources: CommonResourceConfig | None = None,
    scratch: ScratchConfig | None = None,
    messenger: MessengerConfig | None = None,
) -> AppConfig:
    """Build an ``AppConfig`` from the real dataclasses with test-friendly defaults."""

    runtime_fields: dict[str, Any] = {"allowed_root": str(runs_root)}
    if max_concurrent is not None:
        runtime_fields["max_concurrent"] = max_concurrent
    if admission_root is not None:
        runtime_fields["admission_root"] = str(admission_root)
    if admission_limit is not None:
        runtime_fields["admission_limit"] = admission_limit
    return AppConfig(
        runtime=OrcaRuntimeConfig(**runtime_fields),
        paths=PathsConfig(orca_executable=str(orca_executable)),
        resources=resources or CommonResourceConfig(),
        scratch=scratch or ScratchConfig(),
        messenger=messenger or MessengerConfig(),
    )


@pytest.fixture
def app_cfg(tmp_path: Path, fake_orca: Path) -> Callable[..., AppConfig]:
    """Factory: ``app_cfg(**make_app_cfg kwargs)``; defaults ``runs_root=tmp_path`` and ``fake_orca``."""

    def factory(runs_root: str | Path | None = None, **kwargs: Any) -> AppConfig:
        kwargs.setdefault("orca_executable", fake_orca)
        return make_app_cfg(tmp_path if runs_root is None else runs_root, **kwargs)

    return factory


def config_yaml_text(cfg: AppConfig) -> str:
    """Render ``cfg`` as the ``orca_auto.yaml`` text ``load_config`` reads back.

    ``scheduler`` is written whenever ``max_concurrent`` or ``admission_root``
    leaves its default, which (as in production) pins ``admission_limit`` to
    ``max_active_simulations`` on reload.
    """

    payload: dict[str, Any] = {
        "runs_root": cfg.runtime.allowed_root,
        "resources": {
            "max_cores_per_task": cfg.resources.max_cores_per_task,
            "max_memory_gb_per_task": cfg.resources.max_memory_gb_per_task,
        },
    }
    scheduler: dict[str, Any] = {}
    if cfg.runtime.max_concurrent != OrcaRuntimeConfig.max_concurrent:
        scheduler["max_active_simulations"] = cfg.runtime.max_concurrent
    if cfg.runtime.admission_root:
        scheduler["admission_root"] = cfg.runtime.admission_root
    if scheduler:
        payload["scheduler"] = scheduler
    orca: dict[str, Any] = {"paths": {"orca_executable": cfg.paths.orca_executable}}
    if cfg.scratch.root:
        orca["runtime"] = {
            "scratch_root": cfg.scratch.root,
            "scratch_min_free_gb": cfg.scratch.min_free_gb,
        }
    payload["orca"] = orca
    discord = cfg.messenger.discord
    if discord != DiscordConfig():
        payload["messenger"] = {
            "provider": "discord",
            "discord": {
                "bot_token": discord.bot_token,
                "default_channel_id": discord.default_channel_id,
                "timeout_seconds": discord.timeout_seconds,
                "max_attempts": discord.max_attempts,
                "retry_backoff_seconds": discord.retry_backoff_seconds,
            },
        }
    return yaml.safe_dump(payload, sort_keys=False)


def write_config_file(path: Path, cfg: AppConfig) -> Path:
    """Write ``cfg`` as YAML to ``path`` (creating ``runs_root``) and return ``path``."""

    Path(cfg.runtime.allowed_root).mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_yaml_text(cfg), encoding="utf-8")
    return path


@pytest.fixture
def config_path(tmp_path: Path, app_cfg: Callable[..., AppConfig]) -> Callable[..., Path]:
    """Factory: ``config_path(cfg=None, **app_cfg kwargs) -> tmp_path/orca_auto.yaml``."""

    def factory(cfg: AppConfig | None = None, **kwargs: Any) -> Path:
        return write_config_file(tmp_path / "orca_auto.yaml", cfg or app_cfg(**kwargs))

    return factory


# ---------------------------------------------------------------------------
# Queue rows
# ---------------------------------------------------------------------------


def make_queue_entry(
    *,
    queue_id: str | None = None,
    task_id: str | None = None,
    reaction_dir: str | Path = "",
    force: bool = False,
    status: QueueStatus = QueueStatus.PENDING,
    priority: int = 10,
    metadata: dict[str, Any] | None = None,
    **fields: Any,
) -> QueueEntry:
    """Build a ``QueueEntry`` carrying the identity ``orca.queue.adapter.enqueue`` persists."""

    resolved_queue_id = queue_id or timestamped_token("q")
    row_metadata = entry_metadata(reaction_dir=str(reaction_dir), force=force, extra=metadata)
    if QUEUE_RECORD_SYNC_KEY not in row_metadata:
        row_metadata.update(
            queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_COMPLETE, token=resolved_queue_id, owner_pid=0
            )
        )
    return QueueEntry(
        queue_id=resolved_queue_id,
        app_name=QUEUE_APP_NAME,
        task_id=task_id or timestamped_token("orca"),
        task_kind=QUEUE_TASK_KIND,
        engine=QUEUE_ENGINE,
        status=status,
        priority=priority,
        metadata=row_metadata,
        **fields,
    )


def enqueue_entry(root: Path, entry: QueueEntry) -> QueueEntry:
    """Append ``entry`` to the queue file under ``root`` through the real store."""

    if "worker_log" not in entry.metadata:
        entry = replace(
            entry,
            metadata={**entry.metadata, "worker_log": str(worker_log_path(root, entry.queue_id))},
        )

    def append(entries: list[QueueEntry]) -> tuple[QueueEntry, bool]:
        entries.append(entry)
        return entry, True

    return _core_queue_store.mutate_entries(root, append)


def claim_next_entry(root: Path) -> QueueEntry | None:
    """Claim the row the ORCA worker would take next from one queue root.

    The worker's own path: preview the head of ``root`` through
    ``select_next_claimable_entry`` under the ORCA identity filter and the
    worker's retired-workflow skip (``OrcaQueueWorker._skip_entry``), then
    claim it by id fenced on the previewed row. Returns the running row, or
    ``None`` when nothing is claimable or the claim was lost.
    """

    def accept(entry: QueueEntry) -> bool:
        return accept_orca_entry(entry) and not queue_entry_is_retired_workflow_owned(entry, root)

    entry = select_next_claimable_entry(list_queue(root), accept_entry_fn=accept)
    if entry is None:
        return None
    return dequeue_entry_if_pending(root, entry.queue_id, expected_entry=entry)


@pytest.fixture
def queue_root(tmp_path: Path) -> Path:
    """An existing directory that serves as the queue/allowed root."""

    root = tmp_path / "runs"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# job_state.json
# ---------------------------------------------------------------------------


def write_run_state(
    reaction_dir: Path,
    *,
    status: RunStatus | str,
    job_id: str | None = None,
    run_id: str | None = None,
    selected_inp: Path | None = None,
    attempts: list[AttemptRecord] | None = None,
    final_result: RunFinalResult | None = None,
    inp_text: str = "! Opt\n",
) -> RunState:
    """Persist a ``job_state.json`` for ``reaction_dir`` through the real state writers.

    Terminal statuses go through ``finalize_state`` (with a matching
    ``build_final_result`` default); other statuses through ``write_state``.
    The selected input file is created when missing.
    """

    reaction_dir.mkdir(parents=True, exist_ok=True)
    inp = selected_inp or reaction_dir / "rxn.inp"
    if not inp.exists():
        inp.write_text(inp_text, encoding="utf-8")
    state = new_state(reaction_dir, inp)
    if job_id is not None:
        state["job_id"] = job_id
    if run_id is not None:
        state["run_id"] = run_id
    state["attempts"] = list(attempts or [])
    run_status = coerce_run_status(status)
    if run_status in TERMINAL_RUN_STATUSES:
        finalize_state(
            reaction_dir,
            state,
            status=run_status,
            final_result=final_result
            or build_final_result(
                status=run_status,
                analyzer_status=AnalyzerStatus.COMPLETED
                if run_status is RunStatus.COMPLETED
                else AnalyzerStatus.UNKNOWN_FAILURE,
                reason="normal_termination" if run_status is RunStatus.COMPLETED else "test",
                last_out_path=str(inp.with_suffix(".out")),
                resumed=False,
            ),
        )
    else:
        state["status"] = run_status.value
        if final_result is not None:
            state["final_result"] = final_result
        write_state(reaction_dir, state)
    return state


# ---------------------------------------------------------------------------
# Process identity
# ---------------------------------------------------------------------------


@dataclass
class ProcessIdentity:
    """Values the process-identity seams return; mutate fields to change later answers."""

    pid: int = field(default_factory=os.getpid)
    start_ticks: int = 111
    boot_id: str = "test-boot-id"
    alive: bool = True


@pytest.fixture
def stable_process_identity(monkeypatch: pytest.MonkeyPatch) -> ProcessIdentity:
    """Pin every process-identity seam (start ticks, boot id, liveness) to one ``ProcessIdentity``."""

    from orca_auto.core.admission import store as admission_store
    from orca_auto.core.queue import processes as queue_processes
    from orca_auto.core.queue import publication as queue_publication
    from orca_auto.core.utils import process as process_utils

    identity = ProcessIdentity()

    def start_ticks(_pid: int, **_kwargs: Any) -> int | None:
        return identity.start_ticks

    def boot_id(**_kwargs: Any) -> str | None:
        return identity.boot_id

    def alive(_pid: int) -> bool:
        return identity.alive

    monkeypatch.setattr(process_utils, "process_start_ticks", start_ticks)
    monkeypatch.setattr(process_utils, "linux_boot_id", boot_id)
    monkeypatch.setattr(process_utils, "is_process_alive", alive)
    monkeypatch.setattr(admission_store, "_process_start_ticks", start_ticks)
    monkeypatch.setattr(admission_store, "_linux_boot_id", boot_id)
    monkeypatch.setattr(queue_processes, "_pid_exists", alive)
    monkeypatch.setattr(queue_publication, "_linux_boot_id", boot_id)
    return identity


# ---------------------------------------------------------------------------
# Notifications and signals
# ---------------------------------------------------------------------------


@dataclass
class RecordingChannel:
    """A ``MessageChannel`` that records every message; ``on_send`` may override the result."""

    enabled: bool = True
    sends: list[object] = field(default_factory=list)
    on_send: Callable[[object], SendResult | None] | None = None

    def send(self, message: object) -> SendResult:
        self.sends.append(message)
        if self.on_send is not None:
            result = self.on_send(message)
            if result is not None:
                return result
        return SendResult(sent=True)


@pytest.fixture
def recording_channel(monkeypatch: pytest.MonkeyPatch) -> RecordingChannel:
    """Install a ``RecordingChannel`` as the worker's terminal notification channel."""

    channel = RecordingChannel()
    monkeypatch.setattr(worker_tracking, "notification_channel", lambda *_args, **_kwargs: channel)
    return channel


@pytest.fixture
def preserved_signals() -> Iterator[None]:
    """Restore the SIGTERM/SIGINT handlers after the test."""

    with preserved_signal_handlers(signal.SIGTERM, signal.SIGINT):
        yield
