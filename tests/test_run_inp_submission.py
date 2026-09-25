"""Direct queue submission error handling."""

from __future__ import annotations

import threading
from dataclasses import replace
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Self

import pytest

import orca_auto.orca.submission as submission_mod
from orca_auto.core.config import DiscordConfig, MessengerConfig
from orca_auto.core.messaging import discord_bot as discord_bot_mod
from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    queue_entry_is_claimable,
)
from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca import submission as run_inp
from orca_auto.orca.config import CommonResourceConfig
from orca_auto.orca.input_artifacts import OrcaSelectedInputArtifacts
from orca_auto.orca.notifications import notify_queue_enqueued_event
from orca_auto.orca.queue import adapter as queue_adapter
from orca_auto.orca.queue import enqueue_publication as core_enqueue_publication
from orca_auto.orca.queue import notifications as queue_notifications
from orca_auto.orca.queue import publication_repair
from orca_auto.orca.run_dir_guard import use_run_dir_publication_guard
from tests.conftest import claim_next_entry, make_app_cfg, write_config_file, write_fake_orca


def test_submit_without_selectable_inp_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reaction dir without any .inp used to leak the ValueError from
    # resource-request resolution as a CLI traceback.
    context = SimpleNamespace(
        cfg=None,
        allowed_root=tmp_path,
        reaction_dir=tmp_path / "job",
        selected_inp=None,
    )

    def raise_value_error(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("No .inp file selected for ORCA queue submission.")

    monkeypatch.setattr(
        submission_mod, "resolve_submission_context", lambda *_args, **_kwargs: context
    )
    monkeypatch.setattr(submission_mod, "find_submission_conflict", lambda *_args: None)
    monkeypatch.setattr(submission_mod, "create_queued_submission", raise_value_error)

    result = submission_mod.submit_reaction_dir_to_queue(SimpleNamespace())

    assert result.status == "failed"
    assert result.reason == "invalid_submission_input"
    assert "No .inp file selected" in result.stderr


def _real_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    messenger: MessengerConfig | None = None,
) -> tuple[Path, Any]:
    """A real ``orca_auto.yaml`` under ``tmp_path`` that ``load_config`` reads for every call."""

    config = write_config_file(
        tmp_path / "orca_auto.yaml",
        make_app_cfg(
            tmp_path,
            orca_executable=write_fake_orca(tmp_path / "fake_orca", "#!/bin/sh\n"),
            resources=CommonResourceConfig(max_cores_per_task=2, max_memory_gb_per_task=4),
            messenger=messenger,
        ),
    )
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    (reaction_dir / "rxn.inp").write_text(
        "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        queue_notifications, "notify_queue_enqueued_event", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(run_inp, "read_worker_pid", lambda _root: None)
    args = SimpleNamespace(
        config=str(config),
        reaction_dir=str(reaction_dir),
        force=False,
        priority=7,
    )
    return reaction_dir, args


def test_queue_metadata_assembles_supplied_values_without_creating_files(tmp_path: Path) -> None:
    snapshot = {"selected_inp": str(tmp_path / "generation" / "sample.inp")}
    requested = {"max_cores": 2, "max_memory_gb": 4}
    metadata = run_inp.build_queue_metadata(
        artifacts=OrcaSelectedInputArtifacts(
            selected_inp=str(tmp_path / "sample.inp"),
            selected_input_xyz=str(tmp_path / "start.xyz"),
        ),
        job_type="opt",
        molecule_key="sample",
        resource_request=requested,
        execution_snapshot=snapshot,
    )
    assert metadata == {
        "submitted_via": "run_inp",
        "orca_queued_notification_pending": True,
        "job_type": "opt",
        "molecule_key": "sample",
        "resource_request": {"max_cores": 2, "max_memory_gb": 4},
        "resource_actual": {"max_cores": 2, "max_memory_gb": 4},
        "source_selected_inp": str(tmp_path / "sample.inp"),
        "selected_inp": str(tmp_path / "generation" / "sample.inp"),
        "selected_input_path": str(tmp_path / "start.xyz"),
        "selected_input_xyz": str(tmp_path / "start.xyz"),
        "execution_snapshot": snapshot,
    }
    assert list(tmp_path.iterdir()) == []
    metadata["resource_actual"]["max_cores"] = 99
    assert metadata["resource_request"] == requested == {"max_cores": 2, "max_memory_gb": 4}


@pytest.mark.parametrize("failure_stage", ["metadata", "task_id", "intent_transition"])
@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_submission_cleans_created_snapshot_on_pre_enqueue_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    failure_type: type[BaseException],
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    source = (reaction_dir / "rxn.inp").read_bytes()
    snapshots: list[dict[str, Any]] = []
    original_build = run_inp.build_orca_execution_snapshot

    def build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        snapshot = original_build(*args, **kwargs)
        snapshots.append(snapshot)
        return snapshot

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise failure_type("injected pre-enqueue failure")

    monkeypatch.setattr(run_inp, "build_orca_execution_snapshot", build)
    monkeypatch.setattr(
        run_inp, "run_enqueue_publication", lambda *_args: pytest.fail("must not enqueue")
    )
    if failure_stage == "metadata":
        monkeypatch.setattr(run_inp, "build_queue_metadata", fail)
    elif failure_stage == "intent_transition":
        monkeypatch.setattr(run_inp, "transition_snapshot_intent", fail)
    else:
        original_token = run_inp.timestamped_token

        def token(prefix: str, **kwargs: Any) -> str:
            if prefix == "orca":
                fail()
            return original_token(prefix, **kwargs)

        monkeypatch.setattr(run_inp, "timestamped_token", token)
    with pytest.raises(failure_type, match="injected pre-enqueue failure"):
        run_inp.create_queued_submission(run_inp.load_config(args.config), args, reaction_dir)
    assert len(snapshots) == 1
    assert not Path(snapshots[0]["execution_dir"]).exists()
    assert not list((tmp_path / ".orca_auto_snapshot_intents").glob("*.json"))
    assert (reaction_dir / "rxn.inp").read_bytes() == source


@pytest.mark.parametrize("snapshot", [None, {}])
def test_internal_snapshot_failure_is_not_invalid_user_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: dict[str, Any] | None,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    source_input = (reaction_dir / "rxn.inp").read_bytes()
    cleanup_calls: list[object] = []
    monkeypatch.setattr(
        run_inp, "build_orca_execution_snapshot", lambda *_args, **_kwargs: snapshot
    )
    monkeypatch.setattr(
        run_inp,
        "cleanup_unowned_orca_execution_snapshot",
        lambda *args, **kwargs: cleanup_calls.append((args, kwargs)),
    )

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "failed"
    assert result.reason == "queue_submission_failed"
    assert "RuntimeError: ORCA submission" in result.stderr
    assert len(cleanup_calls) == 1
    assert queue_adapter.list_queue(tmp_path) == []
    assert not (tmp_path / "queue.json").exists()
    assert (reaction_dir / "rxn.inp").read_bytes() == source_input


def test_enqueue_save_after_commit_recovers_exact_row_and_submits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    original_save = queue_store.save_entries
    first_save = True

    def save_then_raise(root: Path, entries: Any) -> None:
        nonlocal first_save
        original_save(root, entries)
        if first_save:
            first_save = False
            raise RuntimeError("enqueue fsync failed after replace")

    monkeypatch.setattr(queue_store, "save_entries", save_then_raise)

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "submitted"
    assert result.queued_result is not None
    # The recovered row is parked for the worker repair pass instead of being
    # published inline after an unknown enqueue failure.
    assert "parked for worker repair" in result.queued_result.worker_info.detail
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING

    cfg = run_inp.load_config(args.config)
    assert publication_repair.repair_queue_publication(cfg, tmp_path, entry)
    [repaired] = queue_adapter.list_queue(tmp_path)
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


def test_submission_normalizes_resources_only_in_private_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    source_inp = reaction_dir / "rxn.inp"
    source_payload = source_inp.read_bytes()

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "submitted"
    assert source_inp.read_bytes() == source_payload
    [entry] = queue_adapter.list_queue(tmp_path)
    private_input = Path(entry.metadata["selected_inp"])
    private_text = private_input.read_text(encoding="utf-8")
    assert "%pal" in private_text
    assert "nprocs 2" in private_text
    assert "%maxcore 2048" in private_text
    assert entry.metadata["resource_request"] == {
        "max_cores": 2,
        "max_memory_gb": 4,
    }


def test_notification_delivery_failure_does_not_park_queue_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(
        tmp_path,
        monkeypatch,
        messenger=MessengerConfig(
            discord=DiscordConfig(bot_token="synthetic-token", default_channel_id="123")
        ),
    )
    monkeypatch.setattr(
        queue_notifications, "notify_queue_enqueued_event", lambda *_args, **_kwargs: False
    )

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "submitted"
    assert result.queued_result is not None
    cfg = run_inp.load_config(args.config)
    queue_notifications.notify_queued_jobs(cfg)
    for thread in threading.enumerate():
        if thread.name == "orca-queued-notification":
            thread.join(timeout=5)
    assert not result.queued_result.worker_info.detail
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    assert queue_entry_is_claimable(entry)
    assert entry.metadata[queue_notifications.QUEUED_NOTIFICATION_PENDING_KEY] is False


def test_truncated_discord_response_does_not_park_queue_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(
        tmp_path,
        monkeypatch,
        messenger=MessengerConfig(
            discord=DiscordConfig(
                bot_token="synthetic-token",
                default_channel_id="123",
                max_attempts=1,
            )
        ),
    )

    class _TruncatedResponse:
        status = 200

        def getcode(self) -> int:
            return self.status

        def read(self) -> bytes:
            raise IncompleteRead(b'{"id":')

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> Literal[False]:
            return False

    monkeypatch.setattr(
        queue_notifications, "notify_queue_enqueued_event", notify_queue_enqueued_event
    )
    monkeypatch.setattr(
        discord_bot_mod,
        "urlopen",
        lambda *_args, **_kwargs: _TruncatedResponse(),
    )

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "submitted"
    assert result.queued_result is not None
    cfg = run_inp.load_config(args.config)
    queue_notifications.notify_queued_jobs(cfg)
    for thread in threading.enumerate():
        if thread.name == "orca-queued-notification":
            thread.join(timeout=5)
    assert not result.queued_result.worker_info.detail
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    assert queue_entry_is_claimable(entry)
    assert entry.metadata[queue_notifications.QUEUED_NOTIFICATION_PENDING_KEY] is False


def test_submission_rejects_distinct_sources_with_same_basename_before_enqueue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    reactant = reaction_dir / "reactant" / "input.xyz"
    product = reaction_dir / "product" / "input.xyz"
    reactant.parent.mkdir()
    product.parent.mkdir()
    payload = "1\nsame endpoint\nH 0 0 0\n"
    reactant.write_text(payload, encoding="utf-8")
    product.write_text(payload, encoding="utf-8")
    (reaction_dir / "rxn.inp").write_text(
        '! NEB-TS\n%neb\n  Product "product/input.xyz"\nend\n* xyzfile 0 1 reactant/input.xyz\n',
        encoding="utf-8",
    )

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "failed"
    assert result.reason == "invalid_submission_input"
    assert "different source paths use the same basename" in result.stderr
    assert "input.xyz" in result.stderr
    assert "product/input.xyz" in result.stderr
    assert "reactant/input.xyz" in result.stderr
    assert queue_adapter.list_queue(tmp_path) == []
    assert not [path for path in reaction_dir.iterdir() if is_visible_generation_name(path.name)]
    intent_root = tmp_path / ".orca_auto_snapshot_intents"
    assert not list(intent_root.glob("*.json"))


def test_closed_job_directory_resubmits_to_a_new_sibling_generation_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    first_result = run_inp.submit_reaction_dir_to_queue(args)
    assert first_result.status == "submitted"
    [first] = queue_adapter.list_queue(tmp_path)
    assert queue_adapter.mark_completed(tmp_path, first.queue_id)
    assert queue_adapter.update_metadata(
        tmp_path,
        first.queue_id,
        {queue_adapter.TERMINAL_REPLAY_METADATA_KEY: None},
    )

    second_result = run_inp.submit_reaction_dir_to_queue(args)

    assert second_result.status == "submitted"
    first_after, second = queue_adapter.list_queue(tmp_path)
    first_generation = Path(first_after.metadata["execution_snapshot"]["execution_dir"])
    second_generation = Path(second.metadata["execution_snapshot"]["execution_dir"])
    assert first_generation != second_generation
    assert first_generation.parent == second_generation.parent == reaction_dir.resolve()
    assert first_generation.is_dir()
    assert second_generation.is_dir()
    assert queue_adapter.queue_entry_force(second) is False


def test_complete_transition_after_commit_returns_submitted_with_truthful_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    original_save = queue_store.save_entries
    raised = False

    def save_complete_then_raise(root: Path, entries: Any) -> None:
        nonlocal raised
        original_save(root, entries)
        if not raised and any(
            entry.metadata.get(QUEUE_RECORD_SYNC_KEY) == QUEUE_RECORD_SYNC_COMPLETE
            for entry in entries
        ):
            raised = True
            raise RuntimeError("complete fsync failed after replace")

    monkeypatch.setattr(queue_store, "save_entries", save_complete_then_raise)

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "submitted"
    assert result.queued_result is not None
    # The COMPLETE committed before its durability barrier reported failure:
    # the row is durably COMPLETE (the token-gated park refused to touch it)
    # and the submitter defers honestly to the worker repair pass, which will
    # short-circuit on the durable COMPLETE.
    assert "worker repair will publish" in result.queued_result.worker_info.detail
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


def test_enqueue_save_without_commit_fails_cleanly_without_queue_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)

    def raise_without_save(_root: Path, _entries: Any) -> None:
        raise RuntimeError("enqueue write failed before commit")

    monkeypatch.setattr(queue_store, "save_entries", raise_without_save)

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "failed"
    assert result.reason == "queue_submission_failed"
    assert queue_adapter.list_queue(tmp_path) == []
    assert not (reaction_dir / ".orca_auto_orca_executions").exists()
    assert not [path for path in reaction_dir.iterdir() if is_visible_generation_name(path.name)]


def test_public_run_dir_guard_aborts_orca_before_durable_queue_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    stages: list[str] = []

    def reject_publication(stage: str) -> None:
        stages.append(stage)
        raise RuntimeError("run-dir target moved into reserved smoke results")

    with use_run_dir_publication_guard(reject_publication):
        result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "failed"
    assert result.reason == "queue_submission_failed"
    assert stages == ["ORCA target mutation preflight"]
    assert queue_adapter.list_queue(tmp_path) == []
    assert not (tmp_path / "queue.json").exists()
    assert not (tmp_path / "job_locations.json").exists()
    assert not (reaction_dir / "job_state.json").exists()
    assert not (reaction_dir / "job_report.json").exists()
    assert not [path for path in reaction_dir.iterdir() if is_visible_generation_name(path.name)]


@pytest.mark.parametrize(
    "guard_error",
    [
        pytest.param(
            RuntimeError("run-dir target moved into reserved smoke results"),
            id="runtime-error",
        ),
        pytest.param(
            KeyboardInterrupt("run-dir guard interrupted after commit"),
            id="keyboard-interrupt",
        ),
        pytest.param(
            SystemExit("run-dir guard exited after commit"),
            id="system-exit",
        ),
    ],
)
def test_public_run_dir_guard_compensates_orca_post_commit_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard_error: BaseException,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    stages: list[str] = []

    def reject_after_commit(stage: str) -> None:
        stages.append(stage)
        if stage.endswith("post-commit"):
            raise guard_error

    with use_run_dir_publication_guard(reject_after_commit):
        result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "failed"
    assert result.reason == "queue_submission_failed"
    assert str(guard_error) in result.stderr
    assert stages == [
        "ORCA target mutation preflight",
        "ORCA durable queue pre-commit",
        "ORCA durable queue post-commit",
    ]
    assert queue_adapter.list_queue(tmp_path) == []
    assert not (tmp_path / "job_locations.json").exists()
    assert not (reaction_dir / "job_state.json").exists()
    assert not (reaction_dir / "job_report.json").exists()
    assert not [path for path in reaction_dir.iterdir() if is_visible_generation_name(path.name)]


@pytest.mark.parametrize(
    "compensation_error",
    [
        pytest.param(
            OSError("queue compensation write failed before replace"),
            id="os-error",
        ),
        pytest.param(
            KeyboardInterrupt("queue compensation interrupted before replace"),
            id="keyboard-interrupt",
        ),
        pytest.param(
            SystemExit("queue compensation exited before replace"),
            id="system-exit",
        ),
    ],
)
def test_orca_compensation_failure_fences_row_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compensation_error: BaseException,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    stages: list[str] = []
    save_count = 0
    original_save = queue_store.save_entries

    def fail_compensation_before_replace(root: Path, entries: Any) -> None:
        nonlocal save_count
        save_count += 1
        if save_count == 2:
            raise compensation_error
        original_save(root, entries)

    def reject_after_commit(stage: str) -> None:
        stages.append(stage)
        if stage.endswith("post-commit"):
            raise RuntimeError("run-dir target moved into reserved smoke results")

    def reject_normal_recovery(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("guard-origin compensation failure must not use normal enqueue recovery")

    def reject_publication(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("guard-origin compensation failure must not publish a queued record")

    monkeypatch.setattr(queue_store, "save_entries", fail_compensation_before_replace)
    monkeypatch.setattr(
        core_enqueue_publication, "_recover_committed_enqueue", reject_normal_recovery
    )
    monkeypatch.setattr(submission_mod, "upsert_queued_job_record", reject_publication)

    with use_run_dir_publication_guard(reject_after_commit):
        result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "failed"
    assert result.reason == "queue_enqueue_outcome_unknown"
    assert "run-dir target moved into reserved smoke results" in result.stderr
    assert "queue compensation outcome=not_restored" in result.stderr
    assert str(compensation_error) in result.stderr
    assert stages == [
        "ORCA target mutation preflight",
        "ORCA durable queue pre-commit",
        "ORCA durable queue post-commit",
    ]
    assert save_count == 3
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.status == QueueStatus.FAILED
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED
    assert entry.metadata[queue_adapter.TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY] is True
    assert entry.metadata.get(queue_adapter.TERMINAL_REPLAY_METADATA_KEY) is None
    assert "queue_after_commit_guard_failed" in entry.error
    assert queue_entry_is_claimable(entry) is False
    assert not (tmp_path / "job_locations.json").exists()
    assert not (reaction_dir / "job_state.json").exists()
    assert not (reaction_dir / "job_report.json").exists()
    assert Path(entry.metadata["execution_snapshot"]["execution_dir"]).is_dir()


def test_orca_adapter_rejects_fractional_priority_before_persistence(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()

    with pytest.raises(ValueError, match="priority must be an integer"):
        queue_adapter.enqueue(tmp_path, str(reaction_dir), priority=1.5)  # type: ignore[arg-type]

    assert queue_adapter.list_queue(tmp_path) == []


def test_ambiguous_postcommit_rows_fail_closed_and_remain_unclaimable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    original_save = queue_store.save_entries
    first_save = True

    def save_ambiguous_then_raise(root: Path, entries: Any) -> None:
        nonlocal first_save
        if first_save:
            first_save = False
            duplicate = replace(entries[-1], queue_id="q_ambiguous_duplicate")
            original_save(root, [*entries, duplicate])
            raise RuntimeError("enqueue fsync failed with duplicate durable rows")
        original_save(root, entries)

    monkeypatch.setattr(queue_store, "save_entries", save_ambiguous_then_raise)

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "failed"
    assert result.reason == "queue_enqueue_outcome_unknown"
    entries = queue_adapter.list_queue(tmp_path)
    assert len(entries) == 2
    assert all(entry.status == QueueStatus.CANCELLED for entry in entries)
    assert all(
        entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED for entry in entries
    )
    assert claim_next_entry(tmp_path) is None


def test_duplicate_error_after_commit_is_recovered_as_same_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    original_enqueue = queue_adapter.enqueue

    def enqueue_then_report_duplicate(*enqueue_args: Any, **enqueue_kwargs: Any) -> Any:
        entry = original_enqueue(*enqueue_args, **enqueue_kwargs)
        raise queue_adapter.DuplicateEntryError(str(reaction_dir), entry)

    monkeypatch.setattr(queue_adapter, "enqueue", enqueue_then_report_duplicate)

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "submitted"
    assert result.queued_result is not None
    assert "DuplicateEntryError" in result.queued_result.worker_info.detail
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING

    cfg = run_inp.load_config(args.config)
    assert publication_repair.repair_queue_publication(cfg, tmp_path, entry)
    [repaired] = queue_adapter.list_queue(tmp_path)
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


def test_cancellation_waits_for_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    publication_started = threading.Event()
    allow_publication = threading.Event()
    cancel_finished = threading.Event()
    original_upsert = submission_mod.upsert_queued_job_record
    submission_result: list[Any] = []
    cancellation_result: list[Any] = []

    def blocking_upsert(*upsert_args: Any, **upsert_kwargs: Any) -> None:
        publication_started.set()
        assert allow_publication.wait(timeout=5)
        original_upsert(*upsert_args, **upsert_kwargs)

    monkeypatch.setattr(submission_mod, "upsert_queued_job_record", blocking_upsert)

    submit_thread = threading.Thread(
        target=lambda: submission_result.append(run_inp.submit_reaction_dir_to_queue(args))
    )
    submit_thread.start()
    assert publication_started.wait(timeout=5)
    [preparing_entry] = queue_adapter.list_queue(tmp_path)

    def cancel_entry() -> None:
        cancellation_result.append(queue_adapter.cancel(tmp_path, preparing_entry.queue_id))
        cancel_finished.set()

    cancel_thread = threading.Thread(target=cancel_entry)
    cancel_thread.start()
    assert not cancel_finished.wait(timeout=0.1)
    allow_publication.set()
    submit_thread.join(timeout=5)
    cancel_thread.join(timeout=5)

    assert submission_result[0].status == "submitted"
    assert cancellation_result[0].status == QueueStatus.CANCELLED
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.status == QueueStatus.CANCELLED
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


def test_submit_reports_an_unjudgeable_dead_running_row_as_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orca_auto.orca.queue.orphans import DeadRunningRowUnjudgeableError

    context = SimpleNamespace(
        cfg=None,
        allowed_root=tmp_path,
        reaction_dir=tmp_path / "job",
        selected_inp=None,
    )
    message = (
        f"{tmp_path / 'job'} has a RUNNING queue row left by a dead worker, and whether a "
        f"live slot still protects it cannot be judged: Admission slot file is not valid JSON: "
        f"{tmp_path / 'admission_slots.json'}. Repair or remove "
        f"{tmp_path / 'admission_slots.json'} before resubmitting."
    )

    def raise_unjudgeable(*_args: Any, **_kwargs: Any) -> Any:
        raise DeadRunningRowUnjudgeableError(message)

    monkeypatch.setattr(
        submission_mod, "resolve_submission_context", lambda *_args, **_kwargs: context
    )
    monkeypatch.setattr(submission_mod, "find_submission_conflict", lambda *_args: None)
    monkeypatch.setattr(submission_mod, "create_queued_submission", raise_unjudgeable)

    result = submission_mod.submit_reaction_dir_to_queue(SimpleNamespace())

    assert result.status == "failed"
    assert result.reason == "submission_conflict"
    assert result.stderr == message
