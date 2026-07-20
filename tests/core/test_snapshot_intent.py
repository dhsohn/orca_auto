from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from orca_auto.core.queue import enqueue
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    SNAPSHOT_INTENT_TOKEN_KEY,
    bind_snapshot_intent_generation_identities,
    create_snapshot_intent,
    discard_snapshot_intent,
    discard_snapshot_intent_if_generations_absent,
    finalize_queued_snapshot_intent,
    reconcile_orphaned_snapshot_generations,
    transition_snapshot_intent,
)


def _generation_path(queue_root: Path, name: str = "generation-0001") -> Path:
    job_dir = queue_root / "job"
    job_dir.mkdir(exist_ok=True)
    return job_dir / ".orca_auto_input_snapshots" / name


def _visible_generation_path(
    queue_root: Path,
    name: str = "20260714-224054-959479f2",
) -> Path:
    job_dir = queue_root / "job"
    job_dir.mkdir(exist_ok=True)
    return job_dir / name


def _create_generation(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True)


def _intent_path(queue_root: Path, token: str) -> Path:
    return queue_root / ".orca_auto_snapshot_intents" / f"{token}.json"


def test_reconcile_removes_dead_intent_that_crashed_before_mkdir(tmp_path: Path) -> None:
    generation = _generation_path(tmp_path)
    token = "snapshot-intent-before-mkdir"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="input_snapshot_namespace",
        generation_paths=[generation],
    )

    removed = reconcile_orphaned_snapshot_generations(
        [tmp_path],
        list_queue_fn=lambda _root: [],
        owner_is_alive_fn=lambda _marker: False,
    )

    assert removed == 1
    assert not generation.exists()
    assert not _intent_path(tmp_path, token).exists()


def test_reconcile_removes_dead_orca_generation_pair(tmp_path: Path) -> None:
    input_generation = _generation_path(tmp_path)
    execution_generation = tmp_path / "job" / ".orca_auto_orca_executions" / input_generation.name
    token = "snapshot-intent-orca-pair"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="orca_execution_pair",
        generation_paths=[execution_generation, input_generation],
    )
    _create_generation(execution_generation)
    _create_generation(input_generation)

    removed = reconcile_orphaned_snapshot_generations(
        [tmp_path],
        list_queue_fn=lambda _root: [],
        owner_is_alive_fn=lambda _marker: False,
    )

    assert removed == 1
    assert not execution_generation.exists()
    assert not input_generation.exists()


@pytest.mark.parametrize("kind", ["orca_visible_generation", "xtb_md_visible_generation"])
def test_reconcile_removes_bound_dead_visible_generation(tmp_path: Path, kind: str) -> None:
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-visible-generation"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind=kind,
        generation_paths=[generation],
    )
    _create_generation(generation)
    bind_snapshot_intent_generation_identities(tmp_path, token)

    removed = reconcile_orphaned_snapshot_generations(
        [tmp_path],
        list_queue_fn=lambda _root: [],
        owner_is_alive_fn=lambda _marker: False,
    )

    assert removed == 1
    assert not generation.exists()
    assert not _intent_path(tmp_path, token).exists()


@pytest.mark.parametrize("kind", ["orca_visible_generation", "xtb_md_visible_generation"])
def test_visible_generation_rejects_invalid_name_and_outside_path(
    tmp_path: Path,
    kind: str,
) -> None:
    invalid_name = _visible_generation_path(tmp_path, "generation-0001")
    outside = tmp_path.parent / "outside-job" / "20260714-224054-959479f2"

    for token, generation in (
        ("snapshot-intent-visible-invalid-name", invalid_name),
        ("snapshot-intent-visible-outside-root", outside),
    ):
        with pytest.raises(ValueError, match="escapes"):
            create_snapshot_intent(
                tmp_path,
                token=token,
                kind=kind,
                generation_paths=[generation],
            )


@pytest.mark.parametrize("kind", ["orca_visible_generation", "xtb_md_visible_generation"])
def test_reconcile_refuses_to_delete_substituted_visible_generation(
    tmp_path: Path,
    kind: str,
) -> None:
    generation = _visible_generation_path(tmp_path)
    original_generation = generation.with_name(f"{generation.name}-original")
    token = "snapshot-intent-visible-substituted"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind=kind,
        generation_paths=[generation],
    )
    _create_generation(generation)
    bind_snapshot_intent_generation_identities(tmp_path, token)
    generation.rename(original_generation)
    _create_generation(generation)

    removed = reconcile_orphaned_snapshot_generations(
        [tmp_path],
        list_queue_fn=lambda _root: [],
        owner_is_alive_fn=lambda _marker: False,
    )

    assert removed == 0
    assert generation.is_dir()
    assert original_generation.is_dir()
    assert _intent_path(tmp_path, token).is_file()


@pytest.mark.parametrize("kind", ["orca_visible_generation", "xtb_md_visible_generation"])
def test_dead_creator_with_unbound_visible_generation_retires_intent_only(
    tmp_path: Path,
    kind: str,
) -> None:
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-visible-unbound"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind=kind,
        generation_paths=[generation],
    )
    _create_generation(generation)

    removed = reconcile_orphaned_snapshot_generations(
        [tmp_path],
        list_queue_fn=lambda _root: [],
        owner_is_alive_fn=lambda _marker: False,
    )

    assert removed == 0
    assert generation.is_dir()
    assert not _intent_path(tmp_path, token).exists()


@pytest.mark.parametrize("kind", ["orca_visible_generation", "xtb_md_visible_generation"])
def test_visible_generation_finalize_requires_matching_queue_snapshot_identity(
    tmp_path: Path,
    kind: str,
) -> None:
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-visible-finalize"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind=kind,
        generation_paths=[generation],
    )
    _create_generation(generation)
    bind_snapshot_intent_generation_identities(tmp_path, token)
    details = generation.stat()
    job_details = generation.parent.stat()
    matching_entry = SimpleNamespace(
        metadata={
            "execution_snapshot": {
                "version": 2,
                "execution_dir": str(generation.resolve()),
                "execution_dir_identity": {
                    "device": details.st_dev,
                    "inode": details.st_ino,
                },
                "job_dir_identity": {
                    "device": job_details.st_dev,
                    "inode": job_details.st_ino,
                },
                SNAPSHOT_INTENT_TOKEN_KEY: token,
                SNAPSHOT_INTENT_QUEUE_ROOT_KEY: str(tmp_path.resolve()),
            }
        }
    )

    mismatched = SimpleNamespace(
        metadata={
            "execution_snapshot": {
                **matching_entry.metadata["execution_snapshot"],
                "execution_dir_identity": {
                    "device": details.st_dev,
                    "inode": details.st_ino + 1,
                },
            }
        }
    )
    with pytest.raises(ValueError, match="does not match metadata"):
        finalize_queued_snapshot_intent(tmp_path, mismatched)
    assert _intent_path(tmp_path, token).is_file()

    finalize_queued_snapshot_intent(tmp_path, matching_entry)
    assert not _intent_path(tmp_path, token).exists()


def test_reconcile_preserves_live_creator_without_queue_row(tmp_path: Path) -> None:
    generation = _generation_path(tmp_path)
    token = "snapshot-intent-live-owner"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="input_snapshot_namespace",
        generation_paths=[generation],
    )
    _create_generation(generation)

    removed = reconcile_orphaned_snapshot_generations(
        [tmp_path],
        list_queue_fn=lambda _root: [],
        owner_is_alive_fn=lambda _marker: True,
    )

    assert removed == 0
    assert generation.is_dir()
    assert _intent_path(tmp_path, token).is_file()


def test_raw_queue_token_finalizes_intent_and_preserves_generation(tmp_path: Path) -> None:
    generation = _generation_path(tmp_path)
    token = "snapshot-intent-queue-owned"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="input_snapshot_namespace",
        generation_paths=[generation],
    )
    _create_generation(generation)
    entry = SimpleNamespace(metadata={"execution_snapshot": {SNAPSHOT_INTENT_TOKEN_KEY: token}})

    removed = reconcile_orphaned_snapshot_generations(
        [tmp_path],
        list_queue_fn=lambda _root: [entry],
        owner_is_alive_fn=lambda _marker: False,
    )

    assert removed == 0
    assert generation.is_dir()
    assert not _intent_path(tmp_path, token).exists()


def test_default_reconcile_reads_raw_queue_rows_under_the_core_store(tmp_path: Path) -> None:
    generation = _generation_path(tmp_path)
    token = "snapshot-intent-raw-core-store"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="input_snapshot_namespace",
        generation_paths=[generation],
    )
    _create_generation(generation)
    enqueue(
        tmp_path,
        app_name="foreign-app",
        task_id="foreign-task",
        task_kind="foreign-kind",
        engine="foreign-engine",
        metadata={"execution_snapshot": {SNAPSHOT_INTENT_TOKEN_KEY: token}},
    )

    removed = reconcile_orphaned_snapshot_generations(
        [tmp_path],
        owner_is_alive_fn=lambda _marker: False,
    )

    assert removed == 0
    assert generation.is_dir()
    assert not _intent_path(tmp_path, token).exists()


def test_reserved_queue_entry_finalizes_intent_before_fast_terminal_cleanup(
    tmp_path: Path,
) -> None:
    generation = _generation_path(tmp_path)
    token = "snapshot-intent-worker-finalize"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="input_snapshot_namespace",
        generation_paths=[generation],
    )
    _create_generation(generation)
    entry = SimpleNamespace(
        metadata={
            "execution_snapshot": {
                SNAPSHOT_INTENT_TOKEN_KEY: token,
                SNAPSHOT_INTENT_QUEUE_ROOT_KEY: str(tmp_path.resolve()),
            }
        }
    )

    finalize_queued_snapshot_intent(tmp_path, entry)
    removed = reconcile_orphaned_snapshot_generations(
        [tmp_path],
        list_queue_fn=lambda _root: [],
        owner_is_alive_fn=lambda _marker: False,
    )

    assert removed == 0
    assert generation.is_dir()
    assert not _intent_path(tmp_path, token).exists()


def test_enqueueing_without_a_queue_row_is_reclaimable_after_creator_death(
    tmp_path: Path,
) -> None:
    generation = _generation_path(tmp_path)
    token = "snapshot-intent-enqueueing"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="input_snapshot_namespace",
        generation_paths=[generation],
    )
    _create_generation(generation)
    transition_snapshot_intent(
        tmp_path,
        token,
        target_state=SNAPSHOT_INTENT_STATE_ENQUEUEING,
        expected_states={SNAPSHOT_INTENT_STATE_CREATING},
    )

    removed = reconcile_orphaned_snapshot_generations(
        [tmp_path],
        list_queue_fn=lambda _root: [],
        owner_is_alive_fn=lambda _marker: False,
    )

    assert removed == 1
    assert not generation.exists()


def test_queue_read_failure_retains_every_intent(tmp_path: Path) -> None:
    generation = _generation_path(tmp_path)
    token = "snapshot-intent-queue-error"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="input_snapshot_namespace",
        generation_paths=[generation],
    )
    _create_generation(generation)

    with pytest.raises(RuntimeError, match="queue unreadable"):
        reconcile_orphaned_snapshot_generations(
            [tmp_path],
            list_queue_fn=lambda _root: (_ for _ in ()).throw(RuntimeError("queue unreadable")),
            owner_is_alive_fn=lambda _marker: False,
        )

    assert generation.is_dir()
    assert _intent_path(tmp_path, token).is_file()


def test_cleanup_discard_requires_every_generation_to_be_absent(tmp_path: Path) -> None:
    generation = _generation_path(tmp_path)
    token = "snapshot-intent-cleanup-guard"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="input_snapshot_namespace",
        generation_paths=[generation],
    )
    _create_generation(generation)

    assert not discard_snapshot_intent_if_generations_absent(tmp_path, token)
    assert _intent_path(tmp_path, token).is_file()
    generation.rmdir()
    assert discard_snapshot_intent_if_generations_absent(tmp_path, token)
    assert not _intent_path(tmp_path, token).exists()


def test_discard_is_idempotent_when_intent_directory_is_missing(tmp_path: Path) -> None:
    discard_snapshot_intent(tmp_path, "snapshot-intent-never-created")
    assert discard_snapshot_intent_if_generations_absent(
        tmp_path,
        "snapshot-intent-never-created",
    )


def test_reconcile_keeps_queue_lock_through_owner_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.core.queue import store as queue_store

    generation = _generation_path(tmp_path)
    token = "snapshot-intent-queue-lock-race"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="input_snapshot_namespace",
        generation_paths=[generation],
    )
    _create_generation(generation)
    queue_loaded = Event()
    allow_queue_read = Event()
    enqueue_done = Event()
    real_load_entries = queue_store.load_entries

    def paused_load_entries(root: Path) -> list[object]:
        rows: list[object] = list(real_load_entries(root))
        queue_loaded.set()
        assert allow_queue_read.wait(timeout=2)
        return rows

    monkeypatch.setattr(queue_store, "load_entries", paused_load_entries)
    reconcile_errors: list[BaseException] = []

    def reconcile() -> None:
        try:
            reconcile_orphaned_snapshot_generations(
                [tmp_path],
                owner_is_alive_fn=lambda _marker: not enqueue_done.wait(timeout=0.2),
            )
        except BaseException as exc:  # noqa: BLE001
            reconcile_errors.append(exc)

    reconcile_thread = Thread(target=reconcile)
    reconcile_thread.start()
    assert queue_loaded.wait(timeout=2)

    def publish() -> None:
        enqueue(
            tmp_path,
            app_name="foreign-app",
            task_id="foreign-task",
            task_kind="foreign-kind",
            engine="foreign-engine",
            metadata={"execution_snapshot": {SNAPSHOT_INTENT_TOKEN_KEY: token}},
        )
        enqueue_done.set()

    enqueue_thread = Thread(target=publish)
    enqueue_thread.start()
    allow_queue_read.set()
    reconcile_thread.join(timeout=2)
    enqueue_thread.join(timeout=2)

    assert not reconcile_errors
    assert enqueue_done.is_set()
    assert generation.is_dir()


def test_busy_maintenance_root_does_not_block_other_roots(tmp_path: Path) -> None:
    from orca_auto.core.utils.lock import file_lock

    busy_root = tmp_path / "busy"
    ready_root = tmp_path / "ready"
    busy_root.mkdir()
    ready_root.mkdir()
    busy_generation = _generation_path(busy_root)
    ready_generation = _generation_path(ready_root)
    for root, generation, token in (
        (busy_root, busy_generation, "snapshot-intent-busy-root"),
        (ready_root, ready_generation, "snapshot-intent-ready-root"),
    ):
        create_snapshot_intent(
            root,
            token=token,
            kind="input_snapshot_namespace",
            generation_paths=[generation],
        )
        _create_generation(generation)

    lock_held = Event()
    release_lock = Event()
    holder_errors: list[BaseException] = []

    def hold_busy_maintenance_lock() -> None:
        try:
            with file_lock(busy_root / ".orca_auto_snapshot_intents.lock"):
                lock_held.set()
                release_lock.wait(timeout=5)
        except BaseException as exc:  # noqa: BLE001
            holder_errors.append(exc)

    holder = Thread(target=hold_busy_maintenance_lock)
    holder.start()
    try:
        assert lock_held.wait(timeout=2)
        removed = reconcile_orphaned_snapshot_generations(
            [busy_root, ready_root],
            list_queue_fn=lambda _root: [],
            owner_is_alive_fn=lambda _marker: False,
        )
    finally:
        release_lock.set()
        holder.join(timeout=2)

    assert not holder.is_alive()
    assert holder_errors == []
    assert removed == 1
    assert busy_generation.is_dir()
    assert not ready_generation.exists()


def test_create_intent_rejects_generation_outside_queue_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / ".orca_auto_input_snapshots" / "generation-outside"

    with pytest.raises(ValueError, match="escapes"):
        create_snapshot_intent(
            tmp_path,
            token="snapshot-intent-path-escape",
            kind="input_snapshot_namespace",
            generation_paths=[outside],
        )
