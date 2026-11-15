"""Concurrent native scratch preparation; no engine or Linux admission gate."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from orca_auto.core.engine_scratch import (
    EngineScratchCapacityError,
    EngineScratchError,
    EngineScratchWorkspace,
    _workspace,
)
from orca_auto.core.utils import process
from orca_auto.orca.engine_moinp import engine_moinp_name, prepare_engine_moinp
from orca_auto.orca.scratch import OrcaScratchPolicy


@pytest.fixture
def copy_pool(tmp_path, fake_shm, monkeypatch):
    monkeypatch.setattr(_workspace, "_linux_available_memory_bytes", lambda: 2**63)
    monkeypatch.setattr(process, "linux_boot_id", lambda: "native-reservation-test")
    monkeypatch.setattr(process, "current_process_start_ticks", lambda: 123)
    monkeypatch.setattr(process, "owner_identity_state", lambda *args: "live")
    policy = OrcaScratchPolicy(root=fake_shm / "pool", min_free_bytes=8192, max_task_memory_bytes=1)
    # Avoid macOS's unrelated simultaneous O_CREAT|O_NOFOLLOW first-open race;
    # both preparations still contend on the real shared flock below.
    policy.root.mkdir(mode=0o700)
    (policy.root / ".orca_auto_scratch.lock").touch(mode=0o600)
    inputs = []
    for index in range(2):
        directory = tmp_path / f"generation-{index}"
        directory.mkdir()
        source = directory / "donor.gbw"
        source.write_bytes(b"x" * 4096)
        source.chmod(0o400)
        inp = directory / "probe.inp"
        inp.write_text(f'%moinp "{engine_moinp_name(source.name)}"\n')
        inp.chmod(0o400)
        inputs.append(inp)
    closure = inputs[0].stat().st_size + 4096
    budget = {"total": policy.min_free_bytes + 2 * closure + 4096}

    def free_bytes(_fd=0):
        # Measure real files in the native test pool, with a deterministic byte
        # limit in place of tmpfs capacity. Deferred bytes do not exist yet.
        return budget["total"] - sum(
            p.stat().st_size
            for p in policy.root.glob("attempt-*/*")
            if p.suffix in {".inp", ".gbw"}
        )

    monkeypatch.setattr(_workspace, "_filesystem_free_bytes", free_bytes)
    return policy, inputs, closure, budget, free_bytes


def _reserved(workspace):
    return json.loads((workspace.path / ".orca_auto_scratch.json").read_text())[
        "private_input_reserved_bytes"
    ]


@pytest.mark.parametrize("both_fit", [False, True])
def test_two_concurrent_preparations_cannot_spend_the_same_deferred_bytes(copy_pool, both_fit):
    policy, inputs, closure, budget, free_bytes = copy_pool
    if both_fit:
        budget["total"] += 4096
    start = Barrier(2)

    def prepare(inp):
        start.wait(timeout=5)
        try:
            return EngineScratchWorkspace.create(policy, inp)
        except EngineScratchCapacityError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, inputs))
    workspaces = [item for item in results if isinstance(item, EngineScratchWorkspace)]
    try:
        assert len(workspaces) == (2 if both_fit else 1)
        assert len(list(policy.root.glob("attempt-*"))) == len(workspaces)
        assert sum(isinstance(item, EngineScratchCapacityError) for item in results) == (
            0 if both_fit else 1
        )
        assert free_bytes() - sum(_reserved(w) for w in workspaces) >= policy.min_free_bytes
        for workspace in workspaces:
            assert _reserved(workspace) == 4096
            assert not (workspace.path / engine_moinp_name("donor.gbw")).exists()
            with workspace.consume_private_input_reservation():
                prepare_engine_moinp(
                    workspace.scratch_input, directory_fd=workspace.workspace_dir_fd
                )
            assert _reserved(workspace) == 0
            assert free_bytes() - sum(_reserved(w) for w in workspaces) >= policy.min_free_bytes
        if both_fit:
            assert free_bytes() == policy.min_free_bytes
        else:
            # Consumed bytes must not remain charged as a reservation too.
            budget["total"] = policy.min_free_bytes + 2 * (closure + 4096)
            other = inputs[1] if workspaces[0].durable_input == inputs[0] else inputs[0]
            workspaces.append(EngineScratchWorkspace.create(policy, other))
    finally:
        for workspace in workspaces:
            workspace.discard_unlaunched()
    assert not list(policy.root.glob("attempt-*"))
    assert free_bytes() == budget["total"]


def test_failed_create_and_unlaunched_cleanup_release_reservations(copy_pool, monkeypatch):
    policy, inputs, _, budget, free_bytes = copy_pool
    stage = _workspace._stage_input_closure

    def fail_after_staging(*args, **kwargs):
        stage(*args, **kwargs)
        raise OSError("failed create after reservation")

    with monkeypatch.context() as patch:
        patch.setattr(_workspace, "_stage_input_closure", fail_after_staging)
        with pytest.raises(OSError, match="failed create"):
            EngineScratchWorkspace.create(policy, inputs[0])
    assert not list(policy.root.glob("attempt-*"))
    assert free_bytes() == budget["total"]
    first = EngineScratchWorkspace.create(policy, inputs[0])
    assert _reserved(first) == 4096
    first.discard_unlaunched()
    second = EngineScratchWorkspace.create(policy, inputs[1])
    second.discard_unlaunched()
    assert free_bytes() == budget["total"]


def test_failed_private_copy_retains_budget_until_publication_cleanup(copy_pool):
    policy, inputs, _, budget, free_bytes = copy_pool
    workspace = EngineScratchWorkspace.create(policy, inputs[0])
    target = engine_moinp_name("donor.gbw")
    with pytest.raises(OSError, match="partial copy"):
        with workspace.consume_private_input_reservation():
            (workspace.path / target).write_bytes(b"x" * 2048)
            raise OSError("partial copy")
    assert _reserved(workspace) == 4096
    with pytest.raises(EngineScratchCapacityError):
        EngineScratchWorkspace.create(policy, inputs[1])
    workspace.publish()
    workspace.cleanup()
    assert (inputs[0].parent / target).read_bytes() == b"x" * 2048
    assert free_bytes() == budget["total"]
    retry = EngineScratchWorkspace.create(policy, inputs[1])
    retry.discard_unlaunched()


def test_deferred_copy_reservation_covers_the_last_filesystem_block(copy_pool, monkeypatch):
    policy, inputs, _, _, _ = copy_pool
    unit = os.statvfs(policy.root).f_frsize
    source = inputs[0].parent / "donor.gbw"
    source.chmod(0o600)
    source.write_bytes(b"x" * (unit + 1))
    source.chmod(0o400)
    total = policy.min_free_bytes + 5 * unit  # input + source + private copy

    def free_bytes(_fd=0):
        return total - sum(
            ((p.stat().st_size + unit - 1) // unit) * unit
            for p in policy.root.glob("attempt-*/*")
            if p.suffix in {".inp", ".gbw"}
        )

    monkeypatch.setattr(_workspace, "_filesystem_free_bytes", free_bytes)
    workspace = EngineScratchWorkspace.create(policy, inputs[0])
    try:
        assert _reserved(workspace) == 2 * unit
        with workspace.consume_private_input_reservation():
            prepare_engine_moinp(workspace.scratch_input, directory_fd=workspace.workspace_dir_fd)
        assert free_bytes() == policy.min_free_bytes
    finally:
        workspace.discard_unlaunched()


@pytest.mark.parametrize("invalid", [-1, True, "4096"])
def test_invalid_reservation_blocks_peer_admission_without_removing_evidence(copy_pool, invalid):
    policy, inputs, _, _, _ = copy_pool
    workspace = EngineScratchWorkspace.create(policy, inputs[0])
    try:
        manifest = workspace.path / ".orca_auto_scratch.json"
        payload = json.loads(manifest.read_text())
        payload["private_input_reserved_bytes"] = invalid
        manifest.write_text(json.dumps(payload))
        with pytest.raises(EngineScratchError, match="without valid ownership"):
            EngineScratchWorkspace.create(policy, inputs[1])
        assert workspace.path.exists()
    finally:
        workspace.discard_unlaunched()
