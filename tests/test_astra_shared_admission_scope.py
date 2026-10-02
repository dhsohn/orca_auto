"""Astra B2: a worker bound to a shared admission store must recover only its own work.

Two temporary runs roots share one temporary admission store the way the
acceptance helper binds them: each ``<runs_root>/.admission`` is a symlink to
the same directory. A real ``OrcaQueueWorker`` for the own runs root starts on
an empty store; afterwards a foreign submitter occupies slots, and its slot
owners die while one foreign engine group is still alive. The real recovery
pass (``_periodic_upkeep`` -> ``_reconcile_worker_state`` ->
``recover_orphaned_engine_slots`` / ``list_slots`` / ``reconcile_stale_slots``),
the real store, lock, normalization and counting, and the real admission path
(``_admit_next`` -> ``reserve_slot``) all run unchanged.

Only the host is faked: boot id, ``/proc`` start ticks, ``os.kill``/``os.killpg``
probes and ``signal_process_group_stable``. Every signalling entry is a
recording fake that never delivers a signal; the stable group signal simulates
the group's exit. No real process, ORCA, CLI, network or operational store is
touched.

Unscoped recovery sent SIGTERM to the foreign dead-owner engine 7002 and
cleared or deleted foreign records (ADR 0015). The worker must leave foreign
engines and records exactly as they were, keep counting foreign occupancy
against the shared limit, and still recover its own dead-owner engine. The
foreign root ``runs_foreign`` shares the string prefix of the own root
``runs``, so a prefix comparison cannot pass for ownership.
"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from orca_auto.core import admission
from orca_auto.core.admission.records import slot_to_dict
from orca_auto.core.utils import process as process_utils
from orca_auto.orca.queue.adapter import enqueue
from orca_auto.orca.queue.worker import OrcaQueueWorker
from tests.conftest import make_app_cfg, write_fake_orca
from tests.queue_worker_helpers import current_orca_queue_metadata

BOOT = "boot-astra-b2-synthetic"
OWN_WORKER_TICKS = 111

FOREIGN_ENGINE_OWNER, FOREIGN_ENGINE = 7001, 7002
FOREIGN_IDLE_OWNER = 7003
FOREIGN_HEALTHY_OWNER, FOREIGN_HEALTHY_ENGINE = 7005, 7006
FOREIGN_PENDING_OWNER = 7007
OWN_CHILD, OWN_ENGINE = 7011, 7012
FOREIGN_ENGINES = {FOREIGN_ENGINE, FOREIGN_HEALTHY_ENGINE}


@dataclass
class _Host:
    """Process table seen by the product; signals are only recorded."""

    alive: dict[int, int] = field(default_factory=dict)  # pid -> start ticks
    groups: set[int] = field(default_factory=set)
    signals: list[tuple[str, int, int]] = field(default_factory=list)

    def kill(self, pid: int, signum: int) -> None:
        if signum == 0:
            if pid in self.alive:
                return
            raise ProcessLookupError(pid)
        self.signals.append(("kill", pid, signum))

    def killpg(self, pgid: int, signum: int) -> None:
        if signum == 0:
            if pgid in self.groups:
                return
            raise ProcessLookupError(pgid)
        self.signals.append(("killpg", pgid, signum))

    def stable_group_signal(self, pid: int, pgid: int, ticks: int, signum: int) -> bool:
        self.signals.append(("stable_group", pgid, signum))
        assert self.alive.get(pid) == ticks, "recording fake reached with a stale identity"
        # Simulate the group's exit; nothing is delivered to any real process.
        self.groups.discard(pgid)
        self.alive.pop(pid, None)
        return True


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    fake = _Host(alive={os.getpid(): OWN_WORKER_TICKS})
    monkeypatch.setattr(process_utils, "linux_boot_id", lambda **_kwargs: BOOT)
    monkeypatch.setattr(
        process_utils, "process_start_ticks", lambda pid, **_kwargs: fake.alive.get(pid)
    )
    monkeypatch.setattr(os, "kill", fake.kill)
    monkeypatch.setattr(os, "killpg", fake.killpg)
    monkeypatch.setattr(process_utils, "signal_process_group_stable", fake.stable_group_signal)
    return fake


def _bind_shared(runs_root: Path, shared: Path) -> Path:
    runs_root.mkdir()
    (runs_root / ".admission").symlink_to(shared, target_is_directory=True)
    return admission.admission_dir(runs_root)


def _slot(
    host: _Host,
    root: Path,
    *,
    owner: int,
    queue_id: str,
    work_dir: Path,
    engine: int | None = None,
    pending: bool = False,
) -> str:
    host.alive[owner] = owner * 10 + 1
    work_dir.mkdir(parents=True, exist_ok=True)
    token = admission.reserve_slot(
        root,
        100,
        source="queue_run",
        owner_pid=owner,
        state="active",
        queue_id=queue_id,
        work_dir=work_dir,
        engine_launch_gated=True,
    )
    assert token is not None
    if engine is not None or pending:
        admission.prepare_slot_engine_process(root, token)
    if engine is not None:
        host.alive[engine] = engine * 10 + 2
        host.groups.add(engine)
        admission.set_slot_engine_process(
            root, token, pid=engine, pgid=engine, process_start_ticks=engine * 10 + 2
        )
    return token


def _records(shared: Path, tokens: set[str]) -> dict[str, dict[str, object]]:
    return {
        slot.token: slot_to_dict(slot)
        for slot in admission.list_all_slots(shared)
        if slot.token in tokens
    }


def test_shared_store_worker_recovers_only_its_own_slots_and_keeps_foreign_capacity(
    tmp_path: Path, host: _Host
) -> None:
    shared = tmp_path / "shared_admission"
    shared.mkdir()
    own_root = tmp_path / "runs"
    foreign_root = tmp_path / "runs_foreign"
    own_admission = _bind_shared(own_root, shared)
    foreign_admission = _bind_shared(foreign_root, shared)

    cfg = make_app_cfg(
        own_root, orca_executable=write_fake_orca(tmp_path / "fake_orca"), max_concurrent=3
    )
    worker = OrcaQueueWorker(cfg, str(tmp_path / "orca_auto.yaml"), sleep_fn=lambda _s: None)
    assert worker.admission_root == own_admission
    # Startup on an empty shared store (the helper's preflight condition).
    worker._before_run()
    assert admission.list_all_slots(shared) == []

    # A foreign submitter is admitted after startup.
    foreign = {
        "engine_dead_owner": _slot(
            host,
            foreign_admission,
            owner=FOREIGN_ENGINE_OWNER,
            queue_id="q_foreign_engine",
            work_dir=foreign_root / "job_engine",
            engine=FOREIGN_ENGINE,
        ),
        "idle_dead_owner": _slot(
            host,
            foreign_admission,
            owner=FOREIGN_IDLE_OWNER,
            queue_id="q_foreign_idle",
            work_dir=foreign_root / "job_idle",
        ),
        "healthy": _slot(
            host,
            foreign_admission,
            owner=FOREIGN_HEALTHY_OWNER,
            queue_id="q_foreign_healthy",
            work_dir=foreign_root / "job_healthy",
            engine=FOREIGN_HEALTHY_ENGINE,
        ),
        "pending_dead_owner": _slot(
            host,
            foreign_admission,
            owner=FOREIGN_PENDING_OWNER,
            queue_id="q_foreign_pending",
            work_dir=foreign_root / "job_pending",
            pending=True,
        ),
    }
    own_token = _slot(
        host,
        own_admission,
        owner=OWN_CHILD,
        queue_id="q_own",
        work_dir=own_root / "job_own",
        engine=OWN_ENGINE,
    )
    # Foreign and own slot owners die; their engines (except none for idle/pending) live on.
    for owner in (FOREIGN_ENGINE_OWNER, FOREIGN_IDLE_OWNER, FOREIGN_PENDING_OWNER, OWN_CHILD):
        host.alive.pop(owner)
    foreign_tokens = set(foreign.values())
    foreign_before = _records(shared, foreign_tokens)
    assert len(foreign_before) == 4

    # The actual periodic recovery pass, as due.
    worker._worker_state_last_reconcile = None
    worker._periodic_upkeep()

    foreign_signals = [entry for entry in host.signals if entry[1] in FOREIGN_ENGINES]
    assert foreign_signals == [], f"foreign engine signalled: {foreign_signals}"
    assert _records(shared, foreign_tokens) == foreign_before, "foreign records changed"
    # Own scope is still repaired: its orphaned engine is stopped and its slot cleared.
    assert ("stable_group", OWN_ENGINE, signal.SIGTERM) in host.signals
    assert own_token not in {slot.token for slot in admission.list_all_slots(shared)}

    # Foreign occupancy still counts under the shared lock and limit: engine/pending
    # records and the healthy owner (3) fill a limit of 3, so admission is refused.
    assert admission.read_active_slot_count(shared) == 3
    rxn = own_root / "rxn_own"
    rxn.mkdir()
    # A claimable own row carries the bound job-directory identity that the
    # worker's publication repair verifies before admission (the canonical
    # fixture of tests/orca/queue/test_worker_admission.py::_enqueue_ready).
    enqueue(own_root, str(rxn), metadata=current_orca_queue_metadata(rxn))
    status, reserved = worker._admit_next()
    assert (status, reserved) == ("blocked", None)
    assert _records(shared, foreign_tokens) == foreign_before

    # With room for one more, the own reservation succeeds without normalizing foreign records.
    worker.max_concurrent = 4
    status, reserved = worker._admit_next()
    assert status == "processed" and reserved is not None
    assert _records(shared, foreign_tokens) == foreign_before
    assert worker._release_admission_slot(reserved.admission_token) is True
    assert [entry for entry in host.signals if entry[1] in FOREIGN_ENGINES] == []
    worker._after_run()


def _work_slot(work_dir: str) -> admission.AdmissionSlot:
    return admission.AdmissionSlot(
        token="slot",
        owner_pid=1,
        process_start_ticks=1,
        owner_boot_id=BOOT,
        source="queue_run",
        acquired_at="",
        work_dir=work_dir,
    )


def test_runs_root_ownership_compares_resolved_paths_not_text_prefixes(tmp_path: Path) -> None:
    own_root = tmp_path / "runs"
    (own_root / "job").mkdir(parents=True)
    sibling = tmp_path / "runs_foreign" / "job"
    sibling.mkdir(parents=True)
    (own_root / "link_out").symlink_to(sibling, target_is_directory=True)
    owned = admission.runs_root_ownership(own_root)

    assert owned(_work_slot(str(own_root / "job")))
    assert owned(_work_slot(str(own_root / "not_created_yet")))
    assert not owned(_work_slot(str(sibling)))
    assert not owned(_work_slot(str(own_root / "link_out")))
    assert not owned(_work_slot(""))


def test_child_activation_keeps_foreign_dead_owner_records(tmp_path: Path, host: _Host) -> None:
    shared = tmp_path / "shared_admission"
    shared.mkdir()
    own_root = tmp_path / "runs"
    foreign_root = tmp_path / "runs_foreign"
    own_admission = _bind_shared(own_root, shared)
    foreign_admission = _bind_shared(foreign_root, shared)
    own_token = admission.reserve_slot(own_admission, 10, source="orca", state="reserved")
    assert own_token is not None
    foreign_token = _slot(
        host,
        foreign_admission,
        owner=FOREIGN_IDLE_OWNER,
        queue_id="q_foreign_idle",
        work_dir=foreign_root / "job_idle",
    )
    host.alive.pop(FOREIGN_IDLE_OWNER)
    foreign_before = _records(shared, {foreign_token})

    activated = admission.activate_reserved_slot(
        own_admission,
        own_token,
        work_dir=own_root / "job",
        owned=admission.runs_root_ownership(own_root),
    )

    assert activated is not None
    assert _records(shared, {foreign_token}) == foreign_before
    # Without an owner predicate the store keeps its single-root behavior.
    admission.update_slot_metadata(own_admission, own_token, task_id="t")
    assert _records(shared, {foreign_token}) == {}
