from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.engine import snapshot_intent
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    SNAPSHOT_INTENT_TOKEN_KEY,
    bind_snapshot_intent_generation_identities,
    create_snapshot_intent,
    discard_snapshot_intent,
    discard_snapshot_intent_if_generations_absent,
    reconcile_orphaned_snapshot_generations,
    retire_snapshot_intent,
    transition_snapshot_intent,
)
from orca_auto.core.queue.store import mutate_entries
from orca_auto.core.queue.types import QueueEntry


@pytest.fixture
def dead_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot_intent, "_owner_is_alive", lambda _marker: False)


def _retire(queue_root: Path, token: str, generation: Path | None) -> None:
    """Retire as a worker does for a row that records ``generation``."""
    details = generation.stat() if generation is not None else None
    retire_snapshot_intent(
        queue_root,
        token,
        intent_queue_root=str(queue_root.resolve()),
        execution_dir=str(generation.resolve()) if generation is not None else "",
        execution_dir_identity=(
            {"device": details.st_dev, "inode": details.st_ino} if details is not None else None
        ),
    )


def _visible_generation_path(
    queue_root: Path,
    name: str = "20260714-224054-959479f2",
) -> Path:
    job_dir = queue_root / "job"
    job_dir.mkdir(exist_ok=True)
    return job_dir / name


def _enqueue_foreign_row(queue_root: Path, token: str) -> None:
    """Append a non-ORCA row that references ``token`` under the queue lock."""
    row = QueueEntry(
        queue_id="q-foreign",
        app_name="foreign-app",
        task_id="foreign-task",
        task_kind="foreign-kind",
        engine="foreign-engine",
        metadata={"execution_snapshot": {SNAPSHOT_INTENT_TOKEN_KEY: token}},
    )

    def append(entries: list[QueueEntry]) -> tuple[None, bool]:
        entries.append(row)
        return None, True

    mutate_entries(queue_root, append)


def _create_generation(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True)


def _intent_path(queue_root: Path, token: str) -> Path:
    return queue_root / ".orca_auto_snapshot_intents" / f"{token}.json"


def test_reconcile_retires_dead_visible_intent_that_crashed_before_mkdir(
    tmp_path: Path, dead_owner: None
) -> None:
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-before-mkdir"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="orca_visible_generation",
        generation_paths=[generation],
    )

    removed = reconcile_orphaned_snapshot_generations(tmp_path)

    assert removed == 0
    assert not generation.exists()
    assert not _intent_path(tmp_path, token).exists()


@pytest.mark.parametrize("kind", ["input_snapshot_namespace", "orca_execution_pair"])
def test_legacy_snapshot_intents_are_preserved_but_cannot_be_created(
    tmp_path: Path, kind: str, dead_owner: None
) -> None:
    input_generation = tmp_path / "job" / ".orca_auto_input_snapshots" / "generation-0001"
    execution_generation = tmp_path / "job" / ".orca_auto_orca_executions" / input_generation.name
    generations = [input_generation]
    if kind == "orca_execution_pair":
        generations.append(execution_generation)
    token = "snapshot-intent-legacy-preserved"
    with pytest.raises(ValueError, match="Unsupported snapshot intent kind"):
        create_snapshot_intent(tmp_path, token=token, kind=kind, generation_paths=generations)
    assert not _intent_path(tmp_path, token).parent.exists()
    for generation in generations:
        _create_generation(generation)
        (generation / "legacy.txt").write_text("preserve original data", encoding="utf-8")
    intent = _intent_path(tmp_path, token)
    intent.parent.mkdir(mode=0o700)
    intent.write_text(
        json.dumps(
            {
                "version": 1,
                "token": token,
                "kind": kind,
                "state": SNAPSHOT_INTENT_STATE_CREATING,
                "queue_root": str(tmp_path.resolve()),
                "generation_paths": [str(path.resolve()) for path in generations],
            }
        ),
        encoding="utf-8",
    )
    before = intent.read_bytes()
    assert reconcile_orphaned_snapshot_generations(tmp_path) == 0
    _retire(tmp_path, token, None)
    assert intent.read_bytes() == before
    for generation in generations:
        assert (generation / "legacy.txt").read_text(encoding="utf-8") == "preserve original data"


@pytest.mark.parametrize("kind", ["orca_visible_generation"])
def test_reconcile_removes_bound_dead_visible_generation(
    tmp_path: Path, kind: str, dead_owner: None
) -> None:
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

    removed = reconcile_orphaned_snapshot_generations(tmp_path)

    assert removed == 1
    assert not generation.exists()
    assert not _intent_path(tmp_path, token).exists()


@pytest.mark.parametrize("kind", ["orca_visible_generation"])
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


@pytest.mark.parametrize("kind", ["orca_visible_generation"])
def test_reconcile_refuses_to_delete_substituted_visible_generation(
    tmp_path: Path,
    kind: str,
    dead_owner: None,
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

    removed = reconcile_orphaned_snapshot_generations(tmp_path)

    assert removed == 0
    assert generation.is_dir()
    assert original_generation.is_dir()
    assert _intent_path(tmp_path, token).is_file()


@pytest.mark.parametrize("kind", ["orca_visible_generation"])
def test_dead_creator_with_unbound_visible_generation_retires_intent_only(
    tmp_path: Path,
    kind: str,
    dead_owner: None,
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

    removed = reconcile_orphaned_snapshot_generations(tmp_path)

    assert removed == 0
    assert generation.is_dir()
    assert not _intent_path(tmp_path, token).exists()


def test_retire_requires_the_recorded_generation_identity(tmp_path: Path) -> None:
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-visible-finalize"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="orca_visible_generation",
        generation_paths=[generation],
    )
    _create_generation(generation)
    bind_snapshot_intent_generation_identities(tmp_path, token)
    details = generation.stat()

    with pytest.raises(ValueError, match="does not match metadata"):
        retire_snapshot_intent(
            tmp_path,
            token,
            intent_queue_root=str(tmp_path.resolve()),
            execution_dir=str(generation.resolve()),
            execution_dir_identity={"device": details.st_dev, "inode": details.st_ino + 1},
        )
    assert _intent_path(tmp_path, token).is_file()

    _retire(tmp_path, token, generation)
    assert not _intent_path(tmp_path, token).exists()


def test_reconcile_preserves_live_creator_without_queue_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-live-owner"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="orca_visible_generation",
        generation_paths=[generation],
    )
    _create_generation(generation)

    monkeypatch.setattr(snapshot_intent, "_owner_is_alive", lambda _marker: True)

    removed = reconcile_orphaned_snapshot_generations(tmp_path)

    assert removed == 0
    assert generation.is_dir()
    assert _intent_path(tmp_path, token).is_file()


def test_raw_queue_token_finalizes_intent_and_preserves_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dead_owner: None
) -> None:
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-queue-owned"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="orca_visible_generation",
        generation_paths=[generation],
    )
    _create_generation(generation)
    entry = SimpleNamespace(metadata={"execution_snapshot": {SNAPSHOT_INTENT_TOKEN_KEY: token}})

    monkeypatch.setattr(queue_store, "load_entries", lambda _root: [entry])

    removed = reconcile_orphaned_snapshot_generations(tmp_path)

    assert removed == 0
    assert generation.is_dir()
    assert not _intent_path(tmp_path, token).exists()


def test_default_reconcile_reads_raw_queue_rows_under_the_core_store(
    tmp_path: Path, dead_owner: None
) -> None:
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-raw-core-store"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="orca_visible_generation",
        generation_paths=[generation],
    )
    _create_generation(generation)
    _enqueue_foreign_row(tmp_path, token)

    removed = reconcile_orphaned_snapshot_generations(tmp_path)

    assert removed == 0
    assert generation.is_dir()
    assert not _intent_path(tmp_path, token).exists()


def test_reserved_queue_entry_finalizes_intent_before_fast_terminal_cleanup(
    tmp_path: Path, dead_owner: None
) -> None:
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-worker-finalize"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="orca_visible_generation",
        generation_paths=[generation],
    )
    _create_generation(generation)
    bind_snapshot_intent_generation_identities(tmp_path, token)

    _retire(tmp_path, token, generation)
    removed = reconcile_orphaned_snapshot_generations(tmp_path)

    assert removed == 0
    assert generation.is_dir()
    assert not _intent_path(tmp_path, token).exists()


def test_enqueueing_without_a_queue_row_is_reclaimable_after_creator_death(
    tmp_path: Path, dead_owner: None
) -> None:
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-enqueueing"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="orca_visible_generation",
        generation_paths=[generation],
    )
    _create_generation(generation)
    bind_snapshot_intent_generation_identities(tmp_path, token)
    transition_snapshot_intent(
        tmp_path,
        token,
        target_state=SNAPSHOT_INTENT_STATE_ENQUEUEING,
        expected_states={SNAPSHOT_INTENT_STATE_CREATING},
    )

    removed = reconcile_orphaned_snapshot_generations(tmp_path)

    assert removed == 1
    assert not generation.exists()


def test_queue_read_failure_retains_every_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dead_owner: None
) -> None:
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-queue-error"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="orca_visible_generation",
        generation_paths=[generation],
    )
    _create_generation(generation)

    def unreadable_queue(_root: Path) -> list[QueueEntry]:
        raise RuntimeError("queue unreadable")

    monkeypatch.setattr(queue_store, "load_entries", unreadable_queue)

    with pytest.raises(RuntimeError, match="queue unreadable"):
        reconcile_orphaned_snapshot_generations(tmp_path)

    assert generation.is_dir()
    assert _intent_path(tmp_path, token).is_file()


def test_cleanup_discard_requires_every_generation_to_be_absent(tmp_path: Path) -> None:
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-cleanup-guard"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="orca_visible_generation",
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
    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-queue-lock-race"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="orca_visible_generation",
        generation_paths=[generation],
    )
    _create_generation(generation)
    bind_snapshot_intent_generation_identities(tmp_path, token)
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
    monkeypatch.setattr(
        snapshot_intent, "_owner_is_alive", lambda _marker: not enqueue_done.wait(timeout=0.2)
    )
    reconcile_errors: list[BaseException] = []

    def reconcile() -> None:
        try:
            reconcile_orphaned_snapshot_generations(tmp_path)
        except BaseException as exc:  # noqa: BLE001
            reconcile_errors.append(exc)

    reconcile_thread = Thread(target=reconcile)
    reconcile_thread.start()
    assert queue_loaded.wait(timeout=2)

    def publish() -> None:
        _enqueue_foreign_row(tmp_path, token)
        enqueue_done.set()

    enqueue_thread = Thread(target=publish)
    enqueue_thread.start()
    allow_queue_read.set()
    reconcile_thread.join(timeout=2)
    enqueue_thread.join(timeout=2)

    assert not reconcile_errors
    assert enqueue_done.is_set()
    assert generation.is_dir()


def test_busy_maintenance_lock_skips_the_pass_without_blocking(
    tmp_path: Path, dead_owner: None
) -> None:
    from orca_auto.core.utils.lock import file_lock

    generation = _visible_generation_path(tmp_path)
    token = "snapshot-intent-busy-root"
    create_snapshot_intent(
        tmp_path,
        token=token,
        kind="orca_visible_generation",
        generation_paths=[generation],
    )
    _create_generation(generation)
    bind_snapshot_intent_generation_identities(tmp_path, token)

    lock_held = Event()
    release_lock = Event()
    holder_errors: list[BaseException] = []

    def hold_maintenance_lock() -> None:
        try:
            with file_lock(tmp_path / ".orca_auto_snapshot_intents.lock"):
                lock_held.set()
                release_lock.wait(timeout=5)
        except BaseException as exc:  # noqa: BLE001
            holder_errors.append(exc)

    holder = Thread(target=hold_maintenance_lock)
    holder.start()
    try:
        assert lock_held.wait(timeout=2)
        busy_removed = reconcile_orphaned_snapshot_generations(tmp_path)
    finally:
        release_lock.set()
        holder.join(timeout=2)

    assert not holder.is_alive()
    assert holder_errors == []
    assert busy_removed == 0
    assert generation.is_dir()
    assert reconcile_orphaned_snapshot_generations(tmp_path) == 1
    assert not generation.exists()


def test_create_intent_rejects_generation_outside_queue_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-job" / "20260714-224054-959479f2"

    with pytest.raises(ValueError, match="escapes"):
        create_snapshot_intent(
            tmp_path,
            token="snapshot-intent-path-escape",
            kind="orca_visible_generation",
            generation_paths=[outside],
        )
