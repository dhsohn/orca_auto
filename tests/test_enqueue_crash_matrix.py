"""One crash-scenario matrix over every enqueue-publication engine adapter.

The two engine adapters (workflow xtb/crest, ORCA) share the core
publication driver; their adapters are supposed to stay thin. This
suite drives each REAL adapter entry point through the same crash windows,
breaking the protocol at the shared layers only — the queue store's
``save_entries``, the driver's ``queue_record_publication_lock``, and the
engine's publish symbol — and asserts the invariants that must hold for
every engine: a committed row is never claimable without its published
record, no foreign lease state is overwritten, cancellation wins without a
publication, ambiguous rows are all fenced with an outcome-unknown report,
and notifications are at-most-once.

If an adapter ever re-implements protocol steps instead of delegating to
the driver, the shared interception points stop firing and these tests
fail structurally.

Known limitations (deliberate; the per-engine suites carry these):
the structural enforcement covers the publication window (the driver's
lock binding) — a faithful adapter-local re-implementation of the
commit-recovery or repair steps would pass on outcomes alone; snapshot
intent finalization is not observed by any scenario; and
``set_publish_failing`` only gates the submit-side publish for ORCA
(its repair publishes through an independent binding).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue import enqueue_publication as core_enqueue_publication
from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    queue_entry_is_claimable,
    queue_record_sync_metadata,
)
from orca_auto.core.queue.store import dequeue_next, enqueue, list_queue, request_cancel
from orca_auto.core.queue.types import QueueStatus

FOREIGN_TOKEN = "foreign-lease-token"


@dataclass(frozen=True)
class Outcome:
    succeeded: bool
    cancelled: bool
    outcome_unknown: bool
    detail: str


@dataclass
class Harness:
    """One engine adapter, normalized for the shared scenario matrix."""

    name: str
    queue_root: Path
    submit: Callable[[], Outcome]
    repair: Callable[[Any], bool]
    record_published: Callable[[], bool]
    set_publish_failing: Callable[[bool], None]
    notification_count: Callable[[], int]
    # Notifications a clean successful submit sends (exactly-once check).
    expected_notifications_after_clean_submit: int = 0
    # Notifications sent on the publish-failure path before the park; the
    # worker repair republishes suppressed, so this is also the final total.
    expected_notifications_after_publish_failure: int = 0


def _single_row(queue_root: Path) -> Any:
    [entry] = list_queue(queue_root)
    return entry


# --------------------------------------------------------------------------
# Engine harnesses
# --------------------------------------------------------------------------


def _make_flow_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    del monkeypatch
    from orca_auto.flow.submitters import internal_engine_submission

    queue_root = tmp_path / "flow-queue"
    job_dir = queue_root / "job"
    job_dir.mkdir(parents=True)
    submission = SimpleNamespace(
        queue_root=queue_root,
        app_name="orca_auto_crest",
        task_id="crest-matrix",
        task_kind="crest_conformer_search",
        engine="crest",
        priority=6,
        metadata={"job_dir": str(job_dir), "resource_request": {}},
        context={},
    )
    records: list[str] = []
    notifications: list[str] = []
    publish_failing = {"value": False}

    def record_queued(_cfg: Any, current_submission: Any, entry: Any) -> bool:
        if publish_failing["value"]:
            raise OSError("queued record write failed")
        records.append(str(entry.task_id))
        if not current_submission.context.get("suppress_queued_notification", False):
            notifications.append(str(entry.task_id))
        return True

    def submit() -> Outcome:
        payload = internal_engine_submission.submit_internal_engine_job_dir(
            load_config_fn=lambda _path: object(),
            resolve_job_dir_fn=lambda _cfg, _job_dir: job_dir,
            load_manifest_fn=lambda _job_dir: {},
            build_submission_fn=lambda *_args: submission,
            record_queued_fn=record_queued,
            enqueue_fn=enqueue,
            api_name="crest.run_dir",
            job_dir=str(job_dir),
            priority=6,
            config_path="",
        )
        status = str(payload.get("status") or "")
        stderr = str(payload.get("stderr") or "")
        parsed = payload.get("parsed_stdout") or {}
        return Outcome(
            succeeded=status == "submitted",
            cancelled=status == "blocked" and payload.get("reason") == "cancel_requested",
            outcome_unknown="EnqueuePublicationOutcomeUnknown" in stderr,
            detail="; ".join([stderr, str(parsed.get("warning") or "")]),
        )

    return Harness(
        name="flow",
        queue_root=queue_root,
        submit=submit,
        repair=lambda entry: internal_engine_submission.repair_internal_engine_queue_publication(
            cfg=object(),
            queue_root=queue_root,
            entry=entry,
            record_queued_fn=record_queued,
            entry_matches_fn=lambda current: current.engine == "crest",
        ),
        record_published=lambda: bool(records),
        set_publish_failing=lambda value: publish_failing.__setitem__("value", value),
        notification_count=lambda: len(notifications),
        expected_notifications_after_clean_submit=1,
        expected_notifications_after_publish_failure=0,
    )


def _make_orca_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    from orca_auto.orca import submission as orca_submission
    from orca_auto.orca.config import (
        AppConfig,
        CommonResourceConfig,
        OrcaRuntimeConfig,
        PathsConfig,
    )
    from orca_auto.orca.queue import publication_repair

    root = tmp_path / "orca"
    root.mkdir()
    fake_orca = root / "fake_orca"
    fake_orca.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_orca.chmod(0o755)
    cfg = AppConfig(
        runtime=OrcaRuntimeConfig(allowed_root=str(root)),
        paths=PathsConfig(orca_executable=str(fake_orca)),
        resources=CommonResourceConfig(max_cores_per_task=2, max_memory_gb_per_task=4),
    )
    reaction_dir = root / "rxn"
    reaction_dir.mkdir()
    (reaction_dir / "rxn.inp").write_text(
        "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )
    notifications: list[str] = []
    publish_failing = {"value": False}
    original_upsert = orca_submission.upsert_queued_job_record

    def controllable_upsert(*args: Any, **kwargs: Any) -> None:
        if publish_failing["value"]:
            raise OSError("queued job record write failed")
        original_upsert(*args, **kwargs)

    def count_notification(*_args: Any, **_kwargs: Any) -> bool:
        notifications.append("sent")
        return True

    monkeypatch.setattr(orca_submission, "load_config", lambda _path: cfg)
    monkeypatch.setattr(orca_submission, "notify_queue_enqueued_event", count_notification)
    monkeypatch.setattr(orca_submission, "read_worker_pid", lambda _root: None)
    monkeypatch.setattr(orca_submission, "upsert_queued_job_record", controllable_upsert)
    args = SimpleNamespace(
        config=str(root / "orca_auto.yaml"),
        reaction_dir=str(reaction_dir),
        force=False,
        priority=7,
    )

    def submit() -> Outcome:
        result = orca_submission.submit_reaction_dir_to_queue(args)
        queued = result.queued_result
        detail = str(result.stderr or "")
        if queued is not None and queued.worker_info.detail:
            detail = f"{detail}; {queued.worker_info.detail}"
        return Outcome(
            succeeded=result.status == "submitted",
            cancelled=result.reason == "submission_cancelled",
            outcome_unknown=result.reason == "queue_enqueue_outcome_unknown",
            detail=detail,
        )

    return Harness(
        name="orca",
        queue_root=root,
        submit=submit,
        repair=lambda entry: publication_repair.repair_queue_publication(cfg, root, entry),
        # Both the submit-side and the repair-side publication upsert the
        # tracking record, which materializes the job-locations index.
        record_published=lambda: (root / "job_locations.json").exists(),
        set_publish_failing=lambda value: publish_failing.__setitem__("value", value),
        notification_count=lambda: len(notifications),
        expected_notifications_after_clean_submit=1,
        # The queued notification is sent inside the publication lock before
        # the partial-publish park; the worker repair republishes suppressed.
        expected_notifications_after_publish_failure=1,
    )


_HARNESS_BUILDERS = {
    "flow": _make_flow_harness,
    "orca": _make_orca_harness,
}


@pytest.fixture(params=sorted(_HARNESS_BUILDERS))
def harness(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return _HARNESS_BUILDERS[request.param](tmp_path, monkeypatch)


# --------------------------------------------------------------------------
# Shared breakage points
# --------------------------------------------------------------------------


def _break_first_store_save(
    monkeypatch: pytest.MonkeyPatch,
    *,
    commit_first: bool,
    duplicate_row: bool = False,
) -> None:
    """Make the first durable queue save fail, optionally after committing."""

    original_save = queue_store.save_entries
    state = {"broken": False}

    def save_entries(root: Path, entries: Any) -> None:
        if state["broken"]:
            original_save(root, entries)
            return
        state["broken"] = True
        if commit_first:
            if duplicate_row:
                extra = replace(entries[-1], queue_id="q_matrix_duplicate")
                original_save(root, [*entries, extra])
            else:
                original_save(root, entries)
        raise RuntimeError("queue save durability barrier failed")

    monkeypatch.setattr(queue_store, "save_entries", save_entries)


def _hook_before_publication_lock(
    monkeypatch: pytest.MonkeyPatch,
    hook: Callable[[Path, str], None],
) -> None:
    """Run ``hook`` once, right before the driver's publication lock."""

    original_lock = core_enqueue_publication.queue_record_publication_lock
    state = {"fired": False}

    @contextmanager
    def locked(root: Path, queue_id: str):
        if not state["fired"]:
            state["fired"] = True
            hook(Path(root), queue_id)
        with original_lock(root, queue_id):
            yield

    monkeypatch.setattr(core_enqueue_publication, "queue_record_publication_lock", locked)


def _fence_foreign_complete(root: Path, queue_id: str) -> None:
    def fence(entries: list[Any]) -> tuple[None, bool]:
        for index, current in enumerate(entries):
            if current.queue_id != queue_id:
                continue
            metadata = dict(current.metadata)
            metadata.update(
                queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_COMPLETE,
                    token=FOREIGN_TOKEN,
                    owner_pid=0,
                )
            )
            entries[index] = replace(current, metadata=metadata)
            return None, True
        return None, False

    queue_store.mutate_entries(root, fence)


# --------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------


def test_clean_submit_publishes_exactly_once(
    harness: Harness,
) -> None:
    outcome = harness.submit()

    assert outcome.succeeded
    assert not outcome.cancelled
    current = _single_row(harness.queue_root)
    assert current.status == QueueStatus.PENDING
    assert current.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    assert queue_entry_is_claimable(current) is True
    assert harness.record_published()
    assert harness.notification_count() == harness.expected_notifications_after_clean_submit


def test_precommit_failure_reports_failed_and_leaves_no_row(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _break_first_store_save(monkeypatch, commit_first=False)

    outcome = harness.submit()

    assert not outcome.succeeded
    assert not outcome.outcome_unknown
    assert list_queue(harness.queue_root) == []
    assert not harness.record_published()
    assert harness.notification_count() == 0


def test_committed_but_lost_enqueue_parks_and_worker_repair_publishes(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _break_first_store_save(monkeypatch, commit_first=True)

    outcome = harness.submit()

    assert outcome.succeeded
    parked = _single_row(harness.queue_root)
    assert parked.status == QueueStatus.PENDING
    assert parked.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING
    assert queue_entry_is_claimable(parked) is False
    assert dequeue_next(harness.queue_root) is None
    assert not harness.record_published()
    assert harness.notification_count() == 0

    assert harness.repair(parked)
    repaired = _single_row(harness.queue_root)
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    assert queue_entry_is_claimable(repaired) is True
    assert harness.record_published()
    # The repair republishes without a notification: at-most-once holds even
    # though the original submitter never announced the row.
    assert harness.notification_count() == 0


def test_publish_failure_parks_then_worker_repair_completes(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del monkeypatch
    harness.set_publish_failing(True)

    outcome = harness.submit()

    assert outcome.succeeded
    parked = _single_row(harness.queue_root)
    assert parked.status == QueueStatus.PENDING
    assert parked.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING
    assert queue_entry_is_claimable(parked) is False
    assert dequeue_next(harness.queue_root) is None

    harness.set_publish_failing(False)
    assert harness.repair(parked)
    repaired = _single_row(harness.queue_root)
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    assert queue_entry_is_claimable(repaired) is True
    assert harness.record_published()
    assert harness.notification_count() == harness.expected_notifications_after_publish_failure


def test_foreign_complete_is_never_adopted_or_republished(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hook_before_publication_lock(monkeypatch, _fence_foreign_complete)

    outcome = harness.submit()

    # The foreign COMPLETE is ownership loss: the submission still succeeds
    # durably, but this publisher neither republishes over the foreign lease
    # nor claims the publication as its own.
    assert outcome.succeeded
    current = _single_row(harness.queue_root)
    assert current.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    assert current.metadata[QUEUE_RECORD_SYNC_TOKEN_KEY] == FOREIGN_TOKEN
    assert not harness.record_published()
    assert harness.notification_count() == 0


def test_cancel_before_publication_wins_without_any_publish(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cancel_row(root: Path, queue_id: str) -> None:
        request_cancel(root, queue_id)

    _hook_before_publication_lock(monkeypatch, cancel_row)

    outcome = harness.submit()

    assert outcome.cancelled
    current = _single_row(harness.queue_root)
    assert current.status == QueueStatus.CANCELLED
    assert current.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED
    assert not harness.record_published()
    assert harness.notification_count() == 0


def test_ambiguous_rows_are_all_fenced_and_reported_outcome_unknown(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _break_first_store_save(monkeypatch, commit_first=True, duplicate_row=True)

    outcome = harness.submit()

    assert not outcome.succeeded
    assert outcome.outcome_unknown
    rows = list_queue(harness.queue_root)
    assert len(rows) == 2
    assert all(row.status == QueueStatus.CANCELLED for row in rows)
    assert all(row.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED for row in rows)
    assert dequeue_next(harness.queue_root) is None
    assert not harness.record_published()
    assert harness.notification_count() == 0
