from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orca_auto.core.ingest import (
    UploadActionConsumedError,
    UploadBinding,
    UploadBindingMismatchError,
    UploadQuotaExceededError,
    UploadSessionStore,
    UploadSessionStoreCorruptError,
    UploadState,
    UploadStateConflictError,
    upload_idempotency_key,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def _binding(*, actor: str = "actor-1", thread: str | None = "thread-1") -> UploadBinding:
    return UploadBinding(
        provider="Discord",
        channel_id="channel-1",
        thread_id=thread,
        actor_id=actor,
    )


def _reserve_with_bytes(
    store: UploadSessionStore,
    *,
    binding: UploadBinding | None = None,
    message_id: str = "message-1",
    attachment_id: str = "attachment-1",
    payload: bytes = b"archive bytes",
) -> str:
    reservation = store.reserve(
        binding or _binding(),
        message_id=message_id,
        attachment_ids=[attachment_id],
        expected_bytes=len(payload),
    )
    reservation.session.archive_path.write_bytes(payload)
    return reservation.session.upload_id


def _awaiting_session(
    store: UploadSessionStore,
    *,
    binding: UploadBinding | None = None,
    message_id: str = "message-1",
) -> tuple[str, str, str]:
    upload_id = _reserve_with_bytes(store, binding=binding, message_id=message_id)
    store.finalize_upload(upload_id)
    store.mark_verified(upload_id, verification={"engine": "orca", "entries": 2})
    actions = store.await_confirmation(upload_id)
    return upload_id, actions.confirm_action_id, actions.dismiss_action_id


def test_reservation_is_contained_opaque_durable_and_idempotent(tmp_path: Path) -> None:
    clock = MutableClock()
    store = UploadSessionStore(tmp_path / "staging", now_fn=clock)
    binding = _binding()

    first = store.reserve(
        binding,
        message_id="message-9",
        attachment_ids=["attachment-b", "attachment-a", "attachment-a"],
        expected_bytes=42,
    )
    retry = store.reserve(
        binding,
        message_id="message-9",
        attachment_ids=["attachment-a", "attachment-b"],
        expected_bytes=42,
    )

    assert first.created is True
    assert retry.created is False
    assert retry.session.upload_id == first.session.upload_id
    assert first.session.upload_id.startswith("upl_")
    assert first.session.archive_path == store.root / first.session.upload_id / "archive"
    assert first.session.archive_path.parent.parent == store.root
    assert first.session.attachment_ids == ("attachment-a", "attachment-b")
    assert first.session.idempotency_key == upload_idempotency_key(
        binding,
        message_id="message-9",
        attachment_ids=["attachment-b", "attachment-a"],
    )

    reloaded = UploadSessionStore(store.root, now_fn=clock)
    assert reloaded.get(first.session.upload_id) == first.session


def test_idempotent_retry_rejects_declared_size_drift(tmp_path: Path) -> None:
    store = UploadSessionStore(tmp_path)
    binding = _binding()
    store.reserve(
        binding,
        message_id="message",
        attachment_ids=["attachment"],
        expected_bytes=4,
    )

    with pytest.raises(UploadStateConflictError, match="declared attachment bytes"):
        store.reserve(
            binding,
            message_id="message",
            attachment_ids=["attachment"],
            expected_bytes=5,
        )


def test_idempotent_source_cannot_be_rebound_to_another_actor(tmp_path: Path) -> None:
    store = UploadSessionStore(tmp_path)
    store.reserve(
        _binding(actor="alice"),
        message_id="same-message",
        attachment_ids=["same-attachment"],
        expected_bytes=1,
    )

    with pytest.raises(UploadBindingMismatchError):
        store.reserve(
            _binding(actor="mallory"),
            message_id="same-message",
            attachment_ids=["same-attachment"],
            expected_bytes=1,
        )


def test_reservation_enforces_count_bytes_and_per_actor_quotas(tmp_path: Path) -> None:
    store = UploadSessionStore(
        tmp_path,
        max_staged_count=2,
        max_staged_bytes=10,
        max_staged_per_actor=1,
        max_staged_bytes_per_actor=6,
    )
    store.reserve(
        _binding(actor="alice"),
        message_id="m1",
        attachment_ids=["a1"],
        expected_bytes=6,
    )

    with pytest.raises(UploadQuotaExceededError, match="for actor"):
        store.reserve(
            _binding(actor="alice"),
            message_id="m2",
            attachment_ids=["a2"],
            expected_bytes=1,
        )

    store.reserve(
        _binding(actor="bob"),
        message_id="m3",
        attachment_ids=["a3"],
        expected_bytes=4,
    )
    with pytest.raises(UploadQuotaExceededError, match="count"):
        store.reserve(
            _binding(actor="carol"),
            message_id="m4",
            attachment_ids=["a4"],
            expected_bytes=1,
        )


def test_finalize_records_actual_size_and_hash_and_rechecks_quota(tmp_path: Path) -> None:
    store = UploadSessionStore(
        tmp_path,
        max_staged_count=3,
        max_staged_bytes=5,
        max_staged_per_actor=3,
    )
    reservation = store.reserve(
        _binding(),
        message_id="message",
        attachment_ids=["attachment"],
        expected_bytes=1,
    )
    payload = b"12345"
    reservation.session.archive_path.write_bytes(payload)

    finalized = store.finalize_upload(reservation.session.upload_id)

    assert finalized.actual_bytes == len(payload)
    assert finalized.sha256 == hashlib.sha256(payload).hexdigest()
    assert finalized.state is UploadState.RECEIVING

    second = store.reserve(
        _binding(actor="actor-2"),
        message_id="message-2",
        attachment_ids=["attachment-2"],
        expected_bytes=0,
    )
    second.session.archive_path.write_bytes(b"x")
    with pytest.raises(UploadQuotaExceededError, match="bytes"):
        store.finalize_upload(second.session.upload_id)
    assert store.get(second.session.upload_id).state is UploadState.FAILED
    assert not second.session.archive_path.parent.exists()


def test_reservation_charges_bytes_already_written_before_finalize(tmp_path: Path) -> None:
    store = UploadSessionStore(
        tmp_path,
        max_staged_count=3,
        max_staged_bytes=5,
        max_staged_per_actor=3,
    )
    first = store.reserve(
        _binding(actor="alice"),
        message_id="first",
        attachment_ids=["a1"],
        expected_bytes=1,
    )
    first.session.archive_path.write_bytes(b"12345")

    with pytest.raises(UploadQuotaExceededError, match="bytes"):
        store.reserve(
            _binding(actor="bob"),
            message_id="second",
            attachment_ids=["a2"],
            expected_bytes=1,
        )


def test_finalize_charges_stable_hash_size_after_preflight_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = UploadSessionStore(
        tmp_path,
        max_staged_count=2,
        max_staged_bytes=5,
        max_staged_per_actor=2,
    )
    reservation = store.reserve(
        _binding(),
        message_id="message",
        attachment_ids=["attachment"],
        expected_bytes=1,
    )
    reservation.session.archive_path.write_bytes(b"x")
    stable_hash = store._hash_stable_archive

    def swap_then_hash(path: Path) -> tuple[int, str]:
        path.write_bytes(b"123456")
        return stable_hash(path)

    monkeypatch.setattr(store, "_hash_stable_archive", swap_then_hash)

    with pytest.raises(UploadQuotaExceededError, match="bytes"):
        store.finalize_upload(reservation.session.upload_id)

    assert store.get(reservation.session.upload_id).state is UploadState.FAILED
    assert not reservation.session.archive_path.parent.exists()


def test_cleanup_failure_remains_charged_against_count_quota(tmp_path: Path) -> None:
    store = UploadSessionStore(
        tmp_path,
        max_staged_count=1,
        max_staged_bytes=10,
        max_staged_per_actor=1,
    )
    reservation = store.reserve(
        _binding(actor="alice"),
        message_id="first",
        attachment_ids=["a1"],
        expected_bytes=1,
    )
    marker = reservation.session.archive_path.parent / ".upload-session"
    marker.write_text("tampered\n", encoding="ascii")
    store.mark_discarded(reservation.session.upload_id)

    with pytest.raises(UploadQuotaExceededError, match="count"):
        store.reserve(
            _binding(actor="bob"),
            message_id="second",
            attachment_ids=["a2"],
            expected_bytes=1,
        )


def test_actions_are_opaque_bound_durable_and_compare_and_set(tmp_path: Path) -> None:
    clock = MutableClock()
    binding = _binding()
    store = UploadSessionStore(tmp_path, now_fn=clock)
    upload_id, confirm_id, dismiss_id = _awaiting_session(store, binding=binding)

    assert confirm_id != dismiss_id
    assert len(confirm_id) < 100
    assert len(dismiss_id) < 100
    assert "confirm" not in confirm_id
    assert "dismiss" not in dismiss_id

    retried_actions = store.await_confirmation(upload_id)
    assert retried_actions.confirm_action_id == confirm_id
    assert retried_actions.dismiss_action_id == dismiss_id

    reloaded = UploadSessionStore(tmp_path, now_fn=clock)
    with pytest.raises(UploadBindingMismatchError):
        reloaded.consume_action(confirm_id, binding=_binding(actor="another-actor"))

    consumed = reloaded.consume_action(confirm_id, binding=binding)
    assert consumed.session.upload_id == upload_id
    assert consumed.session.state is UploadState.PROCESSING
    assert consumed.session.consumed_action_id == confirm_id

    with pytest.raises(UploadActionConsumedError):
        reloaded.consume_action(dismiss_id, binding=binding)


def test_only_one_concurrent_action_consumer_wins(tmp_path: Path) -> None:
    clock = MutableClock()
    binding = _binding()
    first_store = UploadSessionStore(tmp_path, now_fn=clock)
    _, confirm_id, dismiss_id = _awaiting_session(first_store, binding=binding)
    second_store = UploadSessionStore(tmp_path, now_fn=clock)

    def consume(store: UploadSessionStore, action_id: str) -> str:
        try:
            return store.consume_action(action_id, binding=binding).session.state.value
        except (UploadActionConsumedError, UploadStateConflictError):
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: consume(*item),
                [(first_store, confirm_id), (second_store, dismiss_id)],
            )
        )

    assert sorted(results) in (["discarded", "lost"], ["lost", "processing"])


def test_expiry_discards_unconfirmed_but_preserves_ambiguous_processing(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    binding = _binding()
    store = UploadSessionStore(
        tmp_path,
        now_fn=clock,
        action_ttl_seconds=5,
        processing_ttl_seconds=10,
    )
    expired_id, _, _ = _awaiting_session(store, binding=binding, message_id="expires")
    clock.advance(seconds=6)

    expired_sweep = store.sweep()

    assert expired_sweep.expired_upload_ids == (expired_id,)
    assert store.get(expired_id).state is UploadState.DISCARDED
    assert not (store.root / expired_id).exists()

    processing_id, confirm_id, _ = _awaiting_session(
        store,
        binding=binding,
        message_id="processing",
    )
    store.consume_action(confirm_id, binding=binding)
    processing_dir = store.root / processing_id
    clock.advance(seconds=11)

    processing_sweep = store.sweep()

    assert processing_sweep.ambiguous_upload_ids == (processing_id,)
    assert store.get(processing_id).state is UploadState.AMBIGUOUS
    assert processing_dir.is_dir()


def test_late_publish_after_processing_expiry_remains_reconcilable(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    binding = _binding()
    store = UploadSessionStore(
        tmp_path / "staging",
        now_fn=clock,
        processing_ttl_seconds=5,
    )
    upload_id, confirm_id, _ = _awaiting_session(store, binding=binding)
    store.consume_action(confirm_id, binding=binding)
    clock.advance(seconds=6)
    assert store.sweep().ambiguous_upload_ids == (upload_id,)

    published = tmp_path / "runs" / "late-job"
    published.mkdir(parents=True)
    late = store.mark_published(upload_id, published_path=published)
    committed = store.mark_committed(upload_id, queue_id="queue-late")

    assert late.state is UploadState.AMBIGUOUS
    assert late.published_path == published.resolve()
    assert committed.state is UploadState.COMMITTED
    assert committed.receipt is not None
    assert committed.receipt.queue_id == "queue-late"
    assert published.is_dir()


def test_published_and_ambiguous_paths_survive_sweep_and_commit(tmp_path: Path) -> None:
    clock = MutableClock()
    binding = _binding()
    store = UploadSessionStore(tmp_path / "staging", now_fn=clock)
    upload_id, confirm_id, _ = _awaiting_session(store, binding=binding)
    store.consume_action(confirm_id, binding=binding)
    published = tmp_path / "runs" / "job-1"
    published.mkdir(parents=True)
    (published / "input.inp").write_text("! B3LYP", encoding="utf-8")
    store.mark_published(upload_id, published_path=published)
    staging_dir = store.root / upload_id
    clock.advance(seconds=24 * 60 * 60)

    store.sweep()

    assert store.get(upload_id).state is UploadState.PUBLISHED
    assert published.is_dir()
    assert staging_dir.is_dir()

    committed = store.mark_committed(upload_id, queue_id="queue-42")

    assert committed.state is UploadState.COMMITTED
    assert committed.receipt is not None
    assert committed.receipt.queue_id == "queue-42"
    assert published.is_dir()
    assert (published / "input.inp").is_file()
    assert not staging_dir.exists()
    assert UploadSessionStore(store.root, now_fn=clock).get(upload_id) == committed
    assert store.mark_committed(upload_id, queue_id="queue-42") == committed
    assert store.mark_published(upload_id, published_path=published) == committed


def test_ambiguous_path_and_staging_are_preserved(tmp_path: Path) -> None:
    store = UploadSessionStore(tmp_path / "staging")
    binding = _binding()
    upload_id, confirm_id, _ = _awaiting_session(store, binding=binding)
    store.consume_action(confirm_id, binding=binding)
    published = tmp_path / "runs" / "job"
    published.mkdir(parents=True)
    store.mark_published(upload_id, published_path=published)
    store.mark_ambiguous(upload_id, reason="queue response was lost")

    store.sweep()

    assert store.get(upload_id).state is UploadState.AMBIGUOUS
    assert published.is_dir()
    assert (store.root / upload_id / "archive").is_file()


def test_known_post_publish_precommit_failure_releases_only_staging(tmp_path: Path) -> None:
    store = UploadSessionStore(tmp_path / "staging")
    binding = _binding()
    upload_id, confirm_id, _ = _awaiting_session(store, binding=binding)
    store.consume_action(confirm_id, binding=binding)
    published = tmp_path / "runs" / "failed-job"
    published.mkdir(parents=True)
    store.mark_published(upload_id, published_path=published)

    failed = store.mark_failed(upload_id, reason="queue rejected before commit")

    assert failed.state is UploadState.FAILED
    assert published.is_dir()
    assert not (store.root / upload_id).exists()


def test_committed_receipt_is_pruned_only_after_retention_without_touching_run(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    binding = _binding()
    store = UploadSessionStore(
        tmp_path / "staging",
        now_fn=clock,
        committed_retention_seconds=5,
    )
    upload_id, confirm_id, _ = _awaiting_session(store, binding=binding)
    store.consume_action(confirm_id, binding=binding)
    published = tmp_path / "runs" / "retained-job"
    published.mkdir(parents=True)
    store.mark_published(upload_id, published_path=published)
    store.mark_committed(upload_id, queue_id="queue-retained")

    clock.advance(seconds=6)
    result = store.sweep()

    assert result.pruned_upload_ids == (upload_id,)
    assert store.list_sessions() == ()
    assert published.is_dir()


def test_failed_published_record_is_retained_while_run_path_exists(tmp_path: Path) -> None:
    clock = MutableClock()
    binding = _binding()
    store = UploadSessionStore(
        tmp_path / "staging",
        now_fn=clock,
        committed_retention_seconds=5,
    )
    upload_id, confirm_id, _ = _awaiting_session(store, binding=binding)
    store.consume_action(confirm_id, binding=binding)
    published = tmp_path / "runs" / "failed-job"
    published.mkdir(parents=True)
    store.mark_published(upload_id, published_path=published)
    store.mark_failed(upload_id, reason="known pre-commit failure")
    clock.advance(seconds=6)

    retained = store.sweep()

    assert retained.pruned_upload_ids == ()
    assert store.get(upload_id).state is UploadState.FAILED
    published.rmdir()
    assert store.sweep().pruned_upload_ids == (upload_id,)


def test_sweep_only_removes_marker_proven_orphans(tmp_path: Path) -> None:
    store = UploadSessionStore(tmp_path)
    owned_id = "upl_abcdefghijklmnop"
    owned = tmp_path / owned_id
    owned.mkdir()
    (owned / ".upload-session").write_text(f"{owned_id}\n", encoding="ascii")
    unowned_id = "upl_ponmlkjihgfedcba"
    unowned = tmp_path / unowned_id
    unowned.mkdir()
    (unowned / "important.txt").write_text("keep", encoding="utf-8")
    unrelated = tmp_path / "user-data"
    unrelated.mkdir()

    result = store.sweep()

    assert result.orphaned_upload_ids == (owned_id,)
    assert not owned.exists()
    assert unowned.is_dir()
    assert unrelated.is_dir()


def test_corrupt_store_fails_closed(tmp_path: Path) -> None:
    store = UploadSessionStore(tmp_path, sweep_on_startup=False)
    store.state_path.write_text(json.dumps({"schema_version": 999, "sessions": []}))

    with pytest.raises(UploadSessionStoreCorruptError):
        store.list_sessions()


def test_state_machine_rejects_out_of_order_publication(tmp_path: Path) -> None:
    store = UploadSessionStore(tmp_path)
    upload_id = _reserve_with_bytes(store)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(UploadStateConflictError):
        store.mark_published(upload_id, published_path=run_dir)


def test_lock_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    root.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("do not truncate", encoding="utf-8")
    (root / ".upload_sessions.lock").symlink_to(victim)
    store = UploadSessionStore(root, sweep_on_startup=False)

    with pytest.raises(UploadSessionStoreCorruptError, match="lock"):
        store.list_sessions()

    assert victim.read_text(encoding="utf-8") == "do not truncate"


def test_state_symlink_is_rejected_without_following_target(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    root.mkdir()
    victim = tmp_path / "state.json"
    payload = json.dumps({"schema_version": 1, "sessions": []})
    victim.write_text(payload, encoding="utf-8")
    (root / "upload_sessions.json").symlink_to(victim)
    store = UploadSessionStore(root, sweep_on_startup=False)

    with pytest.raises(UploadSessionStoreCorruptError):
        store.list_sessions()

    assert victim.read_text(encoding="utf-8") == payload


def test_ambiguous_save_failure_preserves_new_owned_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = UploadSessionStore(tmp_path, sweep_on_startup=False)

    def corrupt_then_fail(_sessions: object) -> None:
        store.state_path.write_text("{", encoding="utf-8")
        raise OSError("simulated uncertain atomic replace")

    monkeypatch.setattr(store, "_save", corrupt_then_fail)

    with pytest.raises(OSError, match="uncertain atomic replace"):
        store.reserve(
            _binding(),
            message_id="message",
            attachment_ids=["attachment"],
            expected_bytes=1,
        )

    owned_dirs = [path for path in tmp_path.glob("upl_*") if path.is_dir()]
    assert len(owned_dirs) == 1
    assert (owned_dirs[0] / ".upload-session").is_file()


def test_cleanup_inode_check_refuses_swapped_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = UploadSessionStore(tmp_path / "staging")
    reservation = store.reserve(
        _binding(),
        message_id="message",
        attachment_ids=["attachment"],
        expected_bytes=1,
    )
    upload_id = reservation.session.upload_id
    session_dir = reservation.session.archive_path.parent
    original_dir = tmp_path / "original-session"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / ".upload-session").write_text(f"{upload_id}\n", encoding="ascii")
    (victim / "important.txt").write_text("keep", encoding="utf-8")
    open_owned = store._open_owned_session_dir
    swapped = False

    def open_then_swap(value: str) -> tuple[int, tuple[int, int]] | None:
        nonlocal swapped
        result = open_owned(value)
        if result is not None and not swapped:
            swapped = True
            session_dir.rename(original_dir)
            victim.rename(session_dir)
        return result

    monkeypatch.setattr(store, "_open_owned_session_dir", open_then_swap)

    store.mark_discarded(upload_id)

    assert (session_dir / "important.txt").read_text(encoding="utf-8") == "keep"
    assert (original_dir / ".upload-session").is_file()


def test_persisted_malformed_action_id_fails_closed(tmp_path: Path) -> None:
    store = UploadSessionStore(tmp_path)
    _awaiting_session(store)
    raw = json.loads(store.state_path.read_text(encoding="utf-8"))
    raw["sessions"][0]["actions"][0]["action_id"] = "../confirm"
    store.state_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(UploadSessionStoreCorruptError):
        store.list_sessions()


def test_persisted_session_timestamp_corruption_fails_closed(tmp_path: Path) -> None:
    store = UploadSessionStore(tmp_path)
    store.reserve(
        _binding(),
        message_id="message",
        attachment_ids=["attachment"],
        expected_bytes=1,
    )
    raw = json.loads(store.state_path.read_text(encoding="utf-8"))
    raw["sessions"][0]["updated_at"] = "2020-01-01T00:00:00+00:00"
    store.state_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(UploadSessionStoreCorruptError):
        store.list_sessions()


def test_lifecycle_timestamps_stay_monotonic_when_wall_clock_regresses(tmp_path: Path) -> None:
    clock = MutableClock()
    store = UploadSessionStore(tmp_path / "staging", now_fn=clock)
    upload_id = _reserve_with_bytes(store)
    created_at = store.get(upload_id).created_at

    clock.advance(seconds=-5)
    store.finalize_upload(upload_id)
    store.mark_verified(upload_id, verification={"engine": "orca"})
    action = store.await_confirmation(upload_id)
    store.consume_action(action.confirm_action_id, binding=_binding())
    published = tmp_path / "runs" / "job"
    published.mkdir(parents=True)
    store.mark_published(upload_id, published_path=published)

    clock.advance(seconds=-5)
    failed = store.mark_failed(upload_id, reason="known pre-commit failure")

    assert failed.updated_at >= created_at
    reloaded = UploadSessionStore(store.root, now_fn=clock, sweep_on_startup=False)
    assert reloaded.get(upload_id).state is UploadState.FAILED


def test_persisted_receipt_timestamp_corruption_fails_closed(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = UploadSessionStore(tmp_path / "staging", now_fn=clock)
    upload_id, confirm_id, _ = _awaiting_session(store)
    store.consume_action(confirm_id, binding=_binding())
    published = tmp_path / "runs" / "job"
    published.mkdir(parents=True)
    store.mark_published(upload_id, published_path=published)
    store.mark_committed(upload_id, queue_id="queue")
    raw = json.loads(store.state_path.read_text(encoding="utf-8"))
    raw["sessions"][0]["receipt"]["committed_at"] = "2027-01-01T00:00:00+00:00"
    store.state_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(UploadSessionStoreCorruptError):
        store.list_sessions()


@pytest.mark.parametrize("duration", [float("nan"), float("inf")])
def test_non_finite_store_durations_are_rejected(tmp_path: Path, duration: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        UploadSessionStore(
            tmp_path,
            session_ttl_seconds=duration,
            sweep_on_startup=False,
        )
