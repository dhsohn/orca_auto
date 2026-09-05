from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
    DuplicateQueueEntryError,
    QueueEntry,
    QueueStatus,
    dequeue_next,
    enqueue,
    list_queue,
    mark_cancelled,
    request_cancel,
    update_metadata,
)
from orca_auto.core.queue import enqueue_publication as core_enqueue_publication
from orca_auto.core.queue.engine.input_snapshot import snapshot_input_file
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    SNAPSHOT_INTENT_TOKEN_KEY,
    create_snapshot_intent,
    discard_snapshot_intent,
    transition_snapshot_intent,
)
from orca_auto.core.queue.publication import queue_record_sync_metadata
from orca_auto.flow.engines.crest.submission import _build_submission_impl
from orca_auto.flow.manifest import resolve_engine_manifest
from orca_auto.flow.orchestration.lifecycle import recompute_workflow_status_impl
from orca_auto.flow.orchestration.stage_runtime.shared import _apply_submission_result
from orca_auto.flow.submitters import crest as crest_submitter
from orca_auto.flow.submitters import internal_engine_submission
from orca_auto.flow.submitters import xtb as xtb_submitter


def _wait_for_thread_event(event: Event, thread: Thread) -> bool:
    for _ in range(100):
        if event.wait(timeout=0.1):
            return True
        if not thread.is_alive():
            return event.is_set()
    return event.is_set()


def _append_and_return(items: Any, value: Any, result: Any) -> Any:
    items.append(value)
    return result


def _join_thread(thread: Thread) -> None:
    for _ in range(100):
        thread.join(timeout=0.1)
        if not thread.is_alive():
            return


def _concurrent_internal_enqueue(
    queue_root: str,
    job_dir: str,
    task_id: str,
    start_event: Any,
    result_queue: Any,
) -> None:
    start_event.wait()
    try:
        entry = enqueue(
            queue_root,
            app_name="orca_auto_crest",
            task_id=task_id,
            task_kind="crest_conformer_search",
            engine="crest",
            metadata={"job_dir": job_dir},
            duplicate_policy=internal_engine_submission._reject_active_job_dir_duplicate,
        )
    except Exception as exc:  # noqa: BLE001
        result_queue.put((exc.__class__.__name__, str(exc)))
    else:
        result_queue.put(("enqueued", entry.queue_id))


@pytest.mark.parametrize("unknown_key", ["max_active_simulations", "ordinary_bad_key"])
def test_invalid_crest_manifest_fails_workflow_instead_of_waiting_for_slot(
    tmp_path: Path, unknown_key: str
) -> None:
    manifest = resolve_engine_manifest(
        tmp_path,
        {"workflow_type": "conformer_screening", "crest": {unknown_key: 2}},
        "crest",
    )

    def build(cfg: Any, job_dir: Path, manifest: Any, args: Any) -> Any:
        return _build_submission_impl(
            cfg, job_dir, manifest, args, job_id="probe", snapshot_namespace="unused"
        )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Invalid manifest must not publish a queue entry or queued record")

    result = internal_engine_submission.submit_internal_engine_job_dir(
        load_config_fn=lambda _: None,
        resolve_job_dir_fn=lambda _, path: Path(path),
        load_manifest_fn=lambda _: manifest,
        build_submission_fn=build,
        record_queued_fn=forbidden,
        enqueue_fn=forbidden,
        api_name="orca_auto.crest.direct_submit",
        job_dir=str(tmp_path / "01_crest" / "crest_1"),
        priority=0,
        config_path=str(tmp_path / "config.yaml"),
    )
    stage: dict[str, Any] = {
        "stage_id": "crest_1",
        "status": "planned",
        "task": {"engine": "crest", "status": "planned"},
        "metadata": {},
    }
    _apply_submission_result(
        stage=stage,
        task=stage["task"],
        stage_metadata=stage["metadata"],
        submission=result,
    )
    workflow = {"status": "running", "template_name": "conformer_screening", "stages": [stage]}
    status = recompute_workflow_status_impl(
        workflow, effective_stage_status_fn=lambda stage: stage["status"]
    )

    assert result["status"] == "failed"
    assert f"Unknown CREST manifest fields: ['{unknown_key}']" in result["stderr"]
    assert stage["task"]["status"] == "submission_failed"
    assert status == "failed"
    assert "submission_deferred_reason" not in stage["metadata"]


@pytest.mark.parametrize(
    "diagnostic", ["Admission limit reached", "waiting_for_slot", "no admission slot"]
)
def test_submission_failure_diagnostics_do_not_control_status(diagnostic: str) -> None:
    result = internal_engine_submission._submission_failure_payload(
        command_trace=[], job_dir="/unused", stderr=diagnostic
    )
    assert result["status"] == "failed"
    assert not result.get("reason")
    assert result["stderr"] == diagnostic


@pytest.mark.parametrize(
    ("module", "engine", "job_dir", "priority", "job_id", "queue_id", "extras"),
    [
        (
            xtb_submitter,
            "xtb",
            "/jobs/xtb-1",
            7,
            "xtb-job-1",
            "q-xtb-1",
            {"job_type": "path", "reaction_key": "rxn-1"},
        ),
        (
            crest_submitter,
            "crest",
            "/jobs/crest-1",
            3,
            "crest-job-1",
            "q-crest-1",
            {},
        ),
    ],
)
def test_submit_job_dir_uses_structured_engine_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    engine: str,
    job_dir: str,
    priority: int,
    job_id: str,
    queue_id: str,
    extras: dict[str, str],
) -> None:
    captured: dict[str, Any] = {}
    cfg = SimpleNamespace(name=f"{engine}-config")
    resolved_job_dir = tmp_path / engine / "organized-job"
    manifest = {"manifest": True}

    def fake_load_config(config_path: str) -> Any:
        captured["config_path"] = config_path
        return cfg

    def fake_resolve_job_dir(cfg_arg: Any, raw_job_dir: str) -> Path:
        captured["resolve"] = (cfg_arg, raw_job_dir)
        return resolved_job_dir

    def fake_load_manifest(job_dir_arg: Path) -> dict[str, Any]:
        captured["manifest_job_dir"] = job_dir_arg
        return manifest

    def fake_build_submission(
        cfg_arg: Any,
        job_dir_arg: Path,
        manifest_arg: dict[str, Any],
        args: Any,
    ) -> Any:
        captured["build"] = (cfg_arg, job_dir_arg, manifest_arg, args)
        metadata = {"job_dir": str(job_dir_arg), **extras}
        return SimpleNamespace(
            queue_root=tmp_path / engine / "queue",
            app_name=f"orca_auto_{engine}",
            task_id=job_id,
            task_kind=f"{engine}_job",
            engine=engine,
            priority=int(args.priority),
            metadata=metadata,
            context={},
        )

    def fake_enqueue(root: Path, **kwargs: Any) -> Any:
        captured["enqueue"] = (root, kwargs)
        return enqueue(root, **kwargs)

    def fake_record_queued(cfg_arg: Any, submission: Any, entry: Any) -> None:
        captured["record"] = (cfg_arg, submission, entry)

    monkeypatch.setattr(module, "load_config", fake_load_config)
    monkeypatch.setattr(module, "resolve_job_dir", fake_resolve_job_dir)
    monkeypatch.setattr(module, "load_job_manifest", fake_load_manifest)
    monkeypatch.setattr(module, "build_submission", fake_build_submission)
    monkeypatch.setattr(module, "enqueue", fake_enqueue)
    monkeypatch.setattr(module, "record_queued", fake_record_queued)

    result = module.submit_job_dir(
        job_dir=job_dir,
        priority=priority,
        config_path="/tmp/config.yaml",
    )

    build_args = captured["build"][3]
    assert captured["config_path"] == "/tmp/config.yaml"
    assert captured["resolve"] == (cfg, job_dir)
    assert captured["manifest_job_dir"] == resolved_job_dir
    assert build_args.config == "/tmp/config.yaml"
    assert build_args.path == job_dir
    assert build_args.priority == priority
    enqueue_metadata = captured["enqueue"][1]["metadata"]
    assert enqueue_metadata["job_dir"] == str(resolved_job_dir)
    assert {key: enqueue_metadata[key] for key in extras} == extras
    assert enqueue_metadata[QUEUE_RECORD_SYNC_KEY] == "preparing"
    assert enqueue_metadata["_orca_auto_queued_record_sync_owner_pid"] > 0
    assert enqueue_metadata["_orca_auto_queued_record_sync_token"]
    assert enqueue_metadata["_orca_auto_queued_record_sync_updated_at"]
    duplicate_policy = captured["enqueue"][1]["duplicate_policy"]
    assert callable(duplicate_policy)
    proposed = QueueEntry(
        queue_id="q-proposed",
        app_name=f"orca_auto_{engine}",
        task_id=job_id,
        task_kind=f"{engine}_job",
        engine=engine,
        metadata={"job_dir": str(resolved_job_dir), **extras},
    )
    for existing_metadata in ({}, {"job_dir": str(tmp_path / "other-job")}):
        existing = QueueEntry(
            queue_id="q-existing",
            app_name=f"orca_auto_{engine}",
            task_id=job_id,
            task_kind=f"{engine}_job",
            engine=engine,
            metadata=existing_metadata,
        )
        with pytest.raises(DuplicateQueueEntryError):
            duplicate_policy([existing], proposed)
    assert captured["record"][0] is cfg
    [persisted] = list_queue(tmp_path / engine / "queue")
    assert persisted.metadata[QUEUE_RECORD_SYNC_KEY] == "complete"
    assert result["status"] == "submitted"
    assert result["returncode"] == 0
    assert result["command_argv"] == [
        f"orca_auto.{engine}.submission.direct_enqueue",
        "config=/tmp/config.yaml",
        f"job_dir={job_dir}",
        f"priority={priority}",
    ]
    assert result["stdout"].startswith("status: queued\n")
    assert result["stderr"] == ""
    assert result["parsed_stdout"]["status"] == "queued"
    assert result["job_id"] == job_id
    assert result["queue_id"] == persisted.queue_id
    assert result["job_dir"] == str(resolved_job_dir)
    if module is xtb_submitter:
        assert result["job_type"] == extras["job_type"]
        assert result["reaction_key"] == extras["reaction_key"]
        assert result["parsed_stdout"]["job_type"] == extras["job_type"]
        assert result["parsed_stdout"]["reaction_key"] == extras["reaction_key"]


@pytest.mark.parametrize(
    ("module", "engine", "extras"),
    [
        (xtb_submitter, "xtb", {"job_type": "opt", "reaction_key": "rxn-1"}),
        (crest_submitter, "crest", {}),
    ],
)
def test_submit_job_dir_preserves_durable_success_when_queued_record_update_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    engine: str,
    extras: dict[str, str],
) -> None:
    job_dir = (tmp_path / engine / "job-1").resolve()
    queue_root = tmp_path / "queue"
    cfg = SimpleNamespace(name=f"{engine}-config")

    monkeypatch.setattr(module, "load_config", lambda _config_path: cfg)
    monkeypatch.setattr(module, "resolve_job_dir", lambda _cfg, _job_dir: job_dir)
    monkeypatch.setattr(module, "load_job_manifest", lambda _job_dir: {})
    monkeypatch.setattr(
        module,
        "build_submission",
        lambda _cfg, _job_dir, _manifest, args: SimpleNamespace(
            queue_root=queue_root,
            app_name=f"orca_auto_{engine}",
            task_id=f"{engine}-job-1",
            task_kind=f"{engine}_job",
            engine=engine,
            priority=int(args.priority),
            metadata={"job_dir": str(job_dir), **extras},
            context={},
        ),
    )

    def fail_record_queued(_cfg: Any, _submission: Any, _entry: Any) -> None:
        raise OSError("disk full after enqueue")

    monkeypatch.setattr(module, "record_queued", fail_record_queued)

    result = module.submit_job_dir(
        job_dir=str(job_dir),
        priority=4,
        config_path="/tmp/config.yaml",
    )

    entries = list_queue(queue_root)
    assert len(entries) == 1
    assert entries[0].status.value == "pending"
    assert entries[0].metadata[QUEUE_RECORD_SYNC_KEY] == "repair_pending"
    assert result["status"] == "submitted"
    assert result["returncode"] == 0
    assert result["queue_id"] == entries[0].queue_id
    assert result["job_id"] == f"{engine}-job-1"
    warning = "queued record publication failed; queue submission succeeded"
    assert warning in result["parsed_stdout"]["warning"]
    assert "OSError: disk full after enqueue" in result["stderr"]


def _post_commit_enqueue_submission(tmp_path: Path) -> tuple[Path, Path, SimpleNamespace]:
    queue_root = tmp_path / "queue"
    job_dir = (tmp_path / "job").resolve()
    submission = SimpleNamespace(
        queue_root=queue_root,
        app_name="orca_auto_crest",
        task_id="crest-post-commit",
        task_kind="crest_conformer_search",
        engine="crest",
        priority=6,
        metadata={"job_dir": str(job_dir)},
        context={},
    )
    return queue_root, job_dir, submission


def _submit_with_enqueue(
    *,
    job_dir: Path,
    submission: Any,
    enqueue_fn: Any,
    record_queued_fn: Any,
) -> dict[str, Any]:
    return internal_engine_submission.submit_internal_engine_job_dir(
        load_config_fn=lambda _path: object(),
        resolve_job_dir_fn=lambda _cfg, _job_dir: job_dir,
        load_manifest_fn=lambda _job_dir: {},
        build_submission_fn=lambda *_args: submission,
        record_queued_fn=record_queued_fn,
        enqueue_fn=enqueue_fn,
        api_name="crest.run_dir",
        job_dir=str(job_dir),
        priority=submission.priority,
        config_path="",
    )


def _submission_with_generation_snapshot(
    tmp_path: Path,
    *,
    task_id: str,
) -> tuple[Path, Path, internal_engine_submission.EngineRunDirSubmission, Path]:
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    job_dir = queue_root / "job"
    job_dir.mkdir(parents=True, exist_ok=True)
    selected = job_dir / "input.xyz"
    selected.write_text("1\nH\nH 0 0 0\n", encoding="utf-8")
    intent_token = f"snapshot-{task_id}"
    create_snapshot_intent(
        queue_root,
        token=intent_token,
        kind="input_snapshot_namespace",
        generation_paths=[job_dir / ".orca_auto_input_snapshots" / task_id],
    )
    descriptor = snapshot_input_file(
        job_dir,
        selected,
        role="selected",
        namespace=task_id,
    )
    submission = internal_engine_submission.EngineRunDirSubmission(
        queue_root=queue_root,
        app_name="orca_auto_crest",
        task_id=task_id,
        task_kind="crest_conformer_search",
        engine="crest",
        priority=6,
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": descriptor["snapshot_path"],
            "execution_snapshot": {
                "snapshot_namespace": task_id,
                SNAPSHOT_INTENT_TOKEN_KEY: intent_token,
                SNAPSHOT_INTENT_QUEUE_ROOT_KEY: str(queue_root.resolve()),
            },
        },
        context={"job_dir": job_dir},
    )
    return queue_root, job_dir, submission, Path(descriptor["snapshot_path"])


def test_precommit_enqueue_failure_cleans_unowned_generation_snapshot(tmp_path: Path) -> None:
    queue_root, job_dir, submission, snapshot_path = _submission_with_generation_snapshot(
        tmp_path,
        task_id="crest-precommit",
    )

    result = _submit_with_enqueue(
        job_dir=job_dir,
        submission=submission,
        enqueue_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("precommit")),
        record_queued_fn=lambda *_args: pytest.fail("precommit failure must not publish"),
    )

    assert result["status"] == "failed"
    assert not snapshot_path.exists()
    assert not (
        queue_root
        / ".orca_auto_snapshot_intents"
        / f"{submission.metadata['execution_snapshot'][SNAPSHOT_INTENT_TOKEN_KEY]}.json"
    ).exists()


def test_active_replay_cleans_only_new_unowned_generation_snapshot(tmp_path: Path) -> None:
    queue_root, job_dir, submission, snapshot_path = _submission_with_generation_snapshot(
        tmp_path,
        task_id="crest-replay-new",
    )
    existing = enqueue(
        queue_root,
        app_name=submission.app_name,
        task_id="crest-existing",
        task_kind=submission.task_kind,
        engine=submission.engine,
        priority=submission.priority,
        metadata={
            "job_dir": str(job_dir),
            **queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_COMPLETE,
                token="existing-publication",
                owner_pid=0,
            ),
        },
    )

    result = _submit_with_enqueue(
        job_dir=job_dir,
        submission=submission,
        enqueue_fn=enqueue,
        record_queued_fn=lambda *_args: True,
    )

    assert result["status"] == "submitted"
    assert result["queue_id"] == existing.queue_id
    assert not snapshot_path.exists()
    assert not (
        queue_root
        / ".orca_auto_snapshot_intents"
        / f"{submission.metadata['execution_snapshot'][SNAPSHOT_INTENT_TOKEN_KEY]}.json"
    ).exists()


def test_indeterminate_postcommit_recovery_preserves_generation_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_root, job_dir, submission, snapshot_path = _submission_with_generation_snapshot(
        tmp_path,
        task_id="crest-indeterminate",
    )
    real_mutate = core_enqueue_publication.mutate_entries
    calls = 0

    def persist_then_raise(root: Path, **kwargs: Any) -> None:
        enqueue(root, **kwargs)
        raise OSError("postcommit")

    def unavailable_recovery(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise OSError("recovery unavailable")
        return real_mutate(*args, **kwargs)

    monkeypatch.setattr(core_enqueue_publication, "mutate_entries", unavailable_recovery)

    result = _submit_with_enqueue(
        job_dir=job_dir,
        submission=submission,
        enqueue_fn=persist_then_raise,
        record_queued_fn=lambda *_args: pytest.fail("indeterminate enqueue must not publish"),
    )

    assert result["status"] == "failed"
    assert len(list_queue(queue_root)) == 1
    assert snapshot_path.is_file()
    assert (
        queue_root
        / ".orca_auto_snapshot_intents"
        / f"{submission.metadata['execution_snapshot'][SNAPSHOT_INTENT_TOKEN_KEY]}.json"
    ).is_file()


def test_enqueue_post_commit_error_recovers_and_defers_to_worker_repair(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    job_dir = queue_root / "job"
    job_dir.mkdir(parents=True)
    submission = SimpleNamespace(
        queue_root=queue_root,
        app_name="orca_auto_crest",
        task_id="crest-post-commit",
        task_kind="crest_conformer_search",
        engine="crest",
        priority=6,
        metadata={"job_dir": str(job_dir), "resource_request": {}},
        context={},
    )
    publications: list[tuple[Any, Any]] = []

    def persist_then_raise(root: Path, **kwargs: Any) -> None:
        enqueue(root, **kwargs)
        raise OSError("enqueue durability barrier failed")

    def record_queued(_cfg: Any, replay_submission: Any, entry: Any) -> bool:
        publications.append((replay_submission, entry))
        return True

    result = _submit_with_enqueue(
        job_dir=job_dir,
        submission=submission,
        enqueue_fn=persist_then_raise,
        record_queued_fn=record_queued,
    )

    persisted = list_queue(queue_root)[0]
    assert result["status"] == "submitted"
    assert result["queue_id"] == persisted.queue_id
    # A recovered enqueue whose result was lost is parked for the worker
    # repair pass instead of being published after an unknown failure.
    assert persisted.metadata[QUEUE_RECORD_SYNC_KEY] == "repair_pending"
    assert "OSError: enqueue durability barrier failed" in result["stderr"]
    assert publications == []

    assert internal_engine_submission.repair_internal_engine_queue_publication(
        cfg=object(),
        queue_root=queue_root,
        entry=persisted,
        record_queued_fn=record_queued,
        entry_matches_fn=lambda current: current.engine == "crest",
    )
    repaired = list_queue(queue_root)[0]
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == "complete"
    assert len(publications) == 1
    assert publications[0][0].context.get(
        internal_engine_submission.SUPPRESS_QUEUED_NOTIFICATION_CONTEXT_KEY,
        False,
    )


def test_enqueue_post_commit_control_flow_is_fenced_then_propagated(tmp_path: Path) -> None:
    queue_root, job_dir, submission = _post_commit_enqueue_submission(tmp_path)

    def persist_then_interrupt(root: Path, **kwargs: Any) -> None:
        enqueue(root, **kwargs)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _submit_with_enqueue(
            job_dir=job_dir,
            submission=submission,
            enqueue_fn=persist_then_interrupt,
            record_queued_fn=lambda *_args: True,
        )

    persisted = list_queue(queue_root)[0]
    assert persisted.metadata[QUEUE_RECORD_SYNC_KEY] == "repair_pending"
    assert persisted.metadata[internal_engine_submission.QUEUE_RECORD_SYNC_OWNER_PID_KEY] == 0


def test_enqueue_post_commit_recovery_barrier_failure_reports_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_root, job_dir, submission = _post_commit_enqueue_submission(tmp_path)
    real_mutate_entries = core_enqueue_publication.mutate_entries
    mutation_calls = 0

    def persist_recovery_then_raise(*args: Any, **kwargs: Any) -> Any:
        nonlocal mutation_calls
        mutation_calls += 1
        result = real_mutate_entries(*args, **kwargs)
        if mutation_calls == 1:
            raise OSError("repair_pending durability barrier failed")
        return result

    monkeypatch.setattr(
        core_enqueue_publication,
        "mutate_entries",
        persist_recovery_then_raise,
    )

    def enqueue_then_raise(root: Path, **kwargs: Any) -> None:
        enqueue(root, **kwargs)
        raise OSError("enqueue durability barrier failed")

    result = _submit_with_enqueue(
        job_dir=job_dir,
        submission=submission,
        enqueue_fn=enqueue_then_raise,
        record_queued_fn=lambda *_args: pytest.fail("an outcome-unknown enqueue must not publish"),
    )

    # The park committed before its durability barrier reported failure: the
    # row is already repair-pending for the worker pass, and the submitter
    # honestly reports the enqueue outcome as unknown instead of success.
    assert result["status"] == "failed"
    persisted = list_queue(queue_root)[0]
    assert persisted.metadata[QUEUE_RECORD_SYNC_KEY] == "repair_pending"
    assert persisted.metadata[internal_engine_submission.QUEUE_RECORD_SYNC_OWNER_PID_KEY] == 0


def test_enqueue_post_commit_recovery_propagates_new_control_flow_after_visible_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_root, job_dir, submission = _post_commit_enqueue_submission(tmp_path)
    real_mutate_entries = core_enqueue_publication.mutate_entries
    mutation_calls = 0

    def persist_recovery_then_interrupt(*args: Any, **kwargs: Any) -> Any:
        nonlocal mutation_calls
        mutation_calls += 1
        result = real_mutate_entries(*args, **kwargs)
        if mutation_calls == 1:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(
        core_enqueue_publication,
        "mutate_entries",
        persist_recovery_then_interrupt,
    )

    def enqueue_then_raise(root: Path, **kwargs: Any) -> None:
        enqueue(root, **kwargs)
        raise OSError("enqueue durability barrier failed")

    with pytest.raises(KeyboardInterrupt):
        _submit_with_enqueue(
            job_dir=job_dir,
            submission=submission,
            enqueue_fn=enqueue_then_raise,
            record_queued_fn=lambda *_args: True,
        )

    persisted = list_queue(queue_root)[0]
    assert persisted.metadata[QUEUE_RECORD_SYNC_KEY] == "repair_pending"
    assert persisted.metadata[internal_engine_submission.QUEUE_RECORD_SYNC_OWNER_PID_KEY] == 0


def test_enqueue_post_commit_recovery_never_mutates_a_different_submission(tmp_path: Path) -> None:
    queue_root, job_dir, submission = _post_commit_enqueue_submission(tmp_path)

    def persist_foreign_then_raise(root: Path, **kwargs: Any) -> None:
        enqueue(root, **{**kwargs, "task_id": "different-task"})
        raise OSError("enqueue result lost")

    result = _submit_with_enqueue(
        job_dir=job_dir,
        submission=submission,
        enqueue_fn=persist_foreign_then_raise,
        record_queued_fn=lambda *_args: True,
    )

    foreign = list_queue(queue_root)[0]
    assert result["status"] == "failed"
    assert foreign.task_id == "different-task"
    assert foreign.metadata[QUEUE_RECORD_SYNC_KEY] == "preparing"
    assert foreign.metadata[internal_engine_submission.QUEUE_RECORD_SYNC_OWNER_PID_KEY] > 0


def test_enqueue_post_commit_recovery_terminally_fences_ambiguous_exact_rows(
    tmp_path: Path,
) -> None:
    queue_root, job_dir, submission = _post_commit_enqueue_submission(tmp_path)

    def persist_two_then_raise(root: Path, **kwargs: Any) -> None:
        enqueue_kwargs = {key: value for key, value in kwargs.items() if key != "duplicate_policy"}
        for _ in range(2):
            enqueue(
                root,
                **enqueue_kwargs,
                duplicate_policy=lambda _entries, _entry: None,
            )
        raise OSError("ambiguous enqueue result")

    result = _submit_with_enqueue(
        job_dir=job_dir,
        submission=submission,
        enqueue_fn=persist_two_then_raise,
        record_queued_fn=lambda *_args: pytest.fail("ambiguous rows must not publish"),
    )

    rows = list_queue(queue_root)
    assert result["status"] == "failed"
    assert len(rows) == 2
    assert all(row.status == QueueStatus.CANCELLED for row in rows)
    assert all(row.cancel_requested for row in rows)
    assert all(row.metadata[QUEUE_RECORD_SYNC_KEY] == "aborted" for row in rows)


def test_repair_publication_system_exit_is_retryable_and_propagates(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    job_dir = tmp_path / "job"
    submission = SimpleNamespace(
        queue_root=queue_root,
        metadata={"job_dir": str(job_dir)},
    )
    entry = enqueue(
        queue_root,
        app_name="orca_auto_crest",
        task_id="crest-repair-interrupted",
        task_kind="crest_conformer_search",
        engine="crest",
        metadata={
            "job_dir": str(job_dir),
            QUEUE_RECORD_SYNC_KEY: "repair_pending",
        },
    )
    state = internal_engine_submission._InternalEngineSubmissionState(
        resolved_job_dir=job_dir,
        submission=submission,
        entry=entry,
    )

    def exit_during_repair(*_args: Any) -> None:
        raise SystemExit(17)

    with pytest.raises(SystemExit, match="17"):
        internal_engine_submission._repair_queued_record(
            cfg=object(),
            state=state,
            record_queued_fn=exit_during_repair,
        )

    persisted = list_queue(queue_root)[0]
    assert persisted.metadata[QUEUE_RECORD_SYNC_KEY] == "repair_pending"
    assert persisted.metadata[internal_engine_submission.QUEUE_RECORD_SYNC_OWNER_PID_KEY] == 0


def test_internal_publication_repair_reclaims_abandoned_live_pid_lease(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    job_dir = queue_root / "job"
    job_dir.mkdir(parents=True)
    entry = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="xtb-live-owner",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "job_type": "opt",
            "resource_request": {},
            **queue_record_sync_metadata(
                internal_engine_submission.QUEUE_RECORD_SYNC_PREPARING,
                token="live-owner-token",
                owner_pid=os.getpid(),
            ),
        },
    )
    recorded: list[str] = []

    assert internal_engine_submission.repair_internal_engine_queue_publication(
        cfg=object(),
        queue_root=queue_root,
        entry=entry,
        record_queued_fn=lambda _cfg, _submission, current: _append_and_return(
            recorded,
            current.task_id,
            True,
        ),
        entry_matches_fn=lambda current: current.engine == "xtb",
    )

    assert recorded == ["xtb-live-owner"]
    [repaired] = list_queue(queue_root)
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == "complete"


def test_xtb_active_replay_rejects_conflicting_job_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    submissions = iter(
        (
            ("xtb-old", "opt", "old-rxn", 4),
            ("xtb-new", "sp", "new-rxn", 9),
        )
    )

    monkeypatch.setattr(xtb_submitter, "load_config", lambda _path: object())
    monkeypatch.setattr(xtb_submitter, "resolve_job_dir", lambda *_args: job_dir)
    monkeypatch.setattr(xtb_submitter, "load_job_manifest", lambda _job_dir: {})

    def build_submission(_cfg: Any, _job_dir: Path, _manifest: Any, _args: Any) -> Any:
        task_id, job_type, reaction_key, priority = next(submissions)
        return SimpleNamespace(
            queue_root=queue_root,
            app_name="orca_auto_xtb",
            task_id=task_id,
            task_kind=f"xtb_{job_type}",
            engine="xtb",
            priority=priority,
            metadata={
                "job_dir": str(job_dir),
                "selected_input_xyz": str(job_dir / "input.xyz"),
                "job_type": job_type,
                "reaction_key": reaction_key,
            },
            context={},
        )

    monkeypatch.setattr(xtb_submitter, "build_submission", build_submission)
    monkeypatch.setattr(xtb_submitter, "record_queued", lambda *_args: None)

    first = xtb_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=4, config_path="/tmp/config.yaml"
    )
    replay = xtb_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=9, config_path="/tmp/config.yaml"
    )

    assert first["job_id"] == "xtb-old"
    assert replay["status"] == "failed"
    assert replay["job_id"] == ""
    assert "conflicting queue identity" in replay["stderr"]
    entry = list_queue(queue_root)[0]
    assert entry.task_id == "xtb-old"
    assert entry.task_kind == "xtb_opt"
    assert entry.metadata["job_type"] == "opt"
    assert entry.metadata["reaction_key"] == "old-rxn"


def test_active_replay_quarantines_unknown_publication_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    existing = enqueue(
        queue_root,
        app_name="orca_auto_crest",
        task_id="crest-existing",
        task_kind="crest_conformer_search",
        engine="crest",
        metadata={
            "job_dir": str(job_dir),
            "mode": "standard",
            QUEUE_RECORD_SYNC_KEY: "future_protocol_state",
        },
    )
    record_calls: list[str] = []
    monkeypatch.setattr(crest_submitter, "load_config", lambda _path: object())
    monkeypatch.setattr(crest_submitter, "resolve_job_dir", lambda *_args: job_dir)
    monkeypatch.setattr(crest_submitter, "load_job_manifest", lambda _job_dir: {})
    monkeypatch.setattr(
        crest_submitter,
        "build_submission",
        lambda _cfg, _job_dir, _manifest, args: SimpleNamespace(
            queue_root=queue_root,
            app_name="orca_auto_crest",
            task_id="crest-replay",
            task_kind="crest_conformer_search",
            engine="crest",
            priority=int(args.priority),
            metadata={"job_dir": str(job_dir), "mode": "standard"},
            context={},
        ),
    )
    monkeypatch.setattr(
        crest_submitter,
        "record_queued",
        lambda *_args: record_calls.append("published"),
    )

    replay = crest_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=8, config_path="/tmp/config.yaml"
    )

    assert replay["status"] == "failed"
    assert record_calls == []
    [persisted] = list_queue(queue_root)
    assert persisted.queue_id == existing.queue_id
    assert persisted.metadata[QUEUE_RECORD_SYNC_KEY] == "future_protocol_state"


def test_active_cancellation_returns_blocked_until_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    task_ids = iter(("crest-first", "crest-blocked", "crest-after-cancel"))
    monkeypatch.setattr(crest_submitter, "load_config", lambda _path: object())
    monkeypatch.setattr(crest_submitter, "resolve_job_dir", lambda *_args: job_dir)
    monkeypatch.setattr(crest_submitter, "load_job_manifest", lambda _job_dir: {})
    monkeypatch.setattr(
        crest_submitter,
        "build_submission",
        lambda _cfg, _job_dir, _manifest, args: SimpleNamespace(
            queue_root=queue_root,
            app_name="orca_auto_crest",
            task_id=next(task_ids),
            task_kind="crest_conformer_search",
            engine="crest",
            priority=int(args.priority),
            metadata={"job_dir": str(job_dir)},
            context={},
        ),
    )
    monkeypatch.setattr(crest_submitter, "record_queued", lambda *_args: None)

    first = crest_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=4, config_path="/tmp/config.yaml"
    )
    running = dequeue_next(queue_root)
    assert running is not None
    assert request_cancel(queue_root, running.queue_id) is not None

    blocked = crest_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=9, config_path="/tmp/config.yaml"
    )
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "cancel_requested"
    assert blocked["returncode"] == 1
    assert blocked["job_id"] == first["job_id"]
    assert blocked["queue_id"] == first["queue_id"]
    assert len(list_queue(queue_root)) == 1

    assert mark_cancelled(queue_root, running.queue_id, error="cancel_requested") is not None
    submitted = crest_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=7, config_path="/tmp/config.yaml"
    )
    assert submitted["status"] == "submitted"
    assert submitted["job_id"] == "crest-after-cancel"
    assert len(list_queue(queue_root)) == 2


def test_record_failure_replay_repairs_existing_identity_without_duplicate_notification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    task_ids = iter(("crest-original", "crest-new"))
    states: list[tuple[str, str]] = []
    indexes: list[tuple[str, str]] = []
    notifications: list[str] = []

    monkeypatch.setattr(crest_submitter, "load_config", lambda _path: object())
    monkeypatch.setattr(crest_submitter, "resolve_job_dir", lambda *_args: job_dir)
    monkeypatch.setattr(crest_submitter, "load_job_manifest", lambda _job_dir: {})
    monkeypatch.setattr(
        crest_submitter,
        "build_submission",
        lambda _cfg, _job_dir, _manifest, args: SimpleNamespace(
            queue_root=queue_root,
            app_name="orca_auto_crest",
            task_id=next(task_ids),
            task_kind="crest_conformer_search",
            engine="crest",
            priority=int(args.priority),
            metadata={"job_dir": str(job_dir), "mode": "nci"},
            context={},
        ),
    )

    def record_queued(_cfg: Any, submission: Any, _entry: Any) -> None:
        states.append((submission.task_id, submission.metadata["mode"]))
        indexes.append((submission.task_id, submission.metadata["mode"]))
        if not submission.context.get("suppress_queued_notification", False):
            notifications.append(submission.task_id)
            raise OSError("transport outcome unknown")

    monkeypatch.setattr(crest_submitter, "record_queued", record_queued)

    first = crest_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=4, config_path="/tmp/config.yaml"
    )
    replay = crest_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=9, config_path="/tmp/config.yaml"
    )

    assert "publication failed" in first["parsed_stdout"]["warning"]
    assert "state/index repaired" in replay["parsed_stdout"]["warning"]
    assert states == [("crest-original", "nci"), ("crest-original", "nci")]
    assert indexes == states
    assert notifications == ["crest-original"]
    entry = list_queue(queue_root)[0]
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == "complete"


def test_worker_cannot_claim_entry_while_fresh_queued_record_is_being_published(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    publish_started = Event()
    allow_publish = Event()
    results: list[dict[str, Any]] = []

    monkeypatch.setattr(crest_submitter, "load_config", lambda _path: object())
    monkeypatch.setattr(crest_submitter, "resolve_job_dir", lambda *_args: job_dir)
    monkeypatch.setattr(crest_submitter, "load_job_manifest", lambda _job_dir: {})
    monkeypatch.setattr(
        crest_submitter,
        "build_submission",
        lambda _cfg, _job_dir, _manifest, args: SimpleNamespace(
            queue_root=queue_root,
            app_name="orca_auto_crest",
            task_id="crest-publishing",
            task_kind="crest_conformer_search",
            engine="crest",
            priority=int(args.priority),
            metadata={"job_dir": str(job_dir)},
            context={},
        ),
    )

    def record_queued(_cfg: Any, _submission: Any, _entry: Any) -> None:
        publish_started.set()
        allow_publish.wait()

    monkeypatch.setattr(crest_submitter, "record_queued", record_queued)

    thread = Thread(
        target=lambda: results.append(
            crest_submitter.submit_job_dir(
                job_dir=str(job_dir),
                priority=4,
                config_path="/tmp/config.yaml",
            )
        )
    )
    thread.start()
    try:
        assert _wait_for_thread_event(publish_started, thread)
        publishing = list_queue(queue_root)[0]
        assert publishing.metadata[QUEUE_RECORD_SYNC_KEY] == "preparing"
        assert (
            update_metadata(
                queue_root,
                publishing.queue_id,
                {QUEUE_RECORD_SYNC_UPDATED_AT_KEY: "2000-01-01T00:00:00+00:00"},
            )
            is not None
        )
        # This also proves record_queued does not run under the queue lock: the
        # dequeue transaction completes immediately, but refuses the live
        # publisher even after its wall-clock lease appears ancient.
        assert dequeue_next(queue_root) is None
    finally:
        allow_publish.set()
        _join_thread(thread)

    assert not thread.is_alive()
    assert results[0]["status"] == "submitted"
    published = list_queue(queue_root)[0]
    assert published.metadata[QUEUE_RECORD_SYNC_KEY] == "complete"
    assert dequeue_next(queue_root) is not None


def test_cancel_during_fresh_publication_terminalizes_only_after_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    publish_started = Event()
    allow_publish = Event()
    cancel_started = Event()
    cancel_finished = Event()
    events: list[str] = []
    submissions: list[dict[str, Any]] = []
    cancellations: list[QueueEntry | None] = []

    monkeypatch.setattr(crest_submitter, "load_config", lambda _path: object())
    monkeypatch.setattr(crest_submitter, "resolve_job_dir", lambda *_args: job_dir)
    monkeypatch.setattr(crest_submitter, "load_job_manifest", lambda _job_dir: {})
    monkeypatch.setattr(
        crest_submitter,
        "build_submission",
        lambda _cfg, _job_dir, _manifest, args: SimpleNamespace(
            queue_root=queue_root,
            app_name="orca_auto_crest",
            task_id="crest-publishing-cancel",
            task_kind="crest_conformer_search",
            engine="crest",
            priority=int(args.priority),
            metadata={"job_dir": str(job_dir)},
            context={},
        ),
    )

    def record_queued(_cfg: Any, _submission: Any, _entry: Any) -> None:
        events.append("record_started")
        publish_started.set()
        allow_publish.wait()
        events.append("record_finished")

    monkeypatch.setattr(crest_submitter, "record_queued", record_queued)

    submit_thread = Thread(
        target=lambda: submissions.append(
            crest_submitter.submit_job_dir(
                job_dir=str(job_dir),
                priority=4,
                config_path="/tmp/config.yaml",
            )
        )
    )
    submit_thread.start()
    cancel_thread: Thread | None = None
    try:
        assert _wait_for_thread_event(publish_started, submit_thread)
        queue_id = list_queue(queue_root)[0].queue_id

        def cancel() -> None:
            cancel_started.set()
            cancellations.append(request_cancel(queue_root, queue_id))
            events.append("cancel_finished")
            cancel_finished.set()

        cancel_thread = Thread(target=cancel)
        cancel_thread.start()
        assert _wait_for_thread_event(cancel_started, cancel_thread)
        assert not cancel_finished.wait(timeout=0.25)
        publishing = list_queue(queue_root)[0]
        assert publishing.status.value == "pending"
        assert publishing.metadata[QUEUE_RECORD_SYNC_KEY] == "preparing"
    finally:
        allow_publish.set()
        _join_thread(submit_thread)
        if cancel_thread is not None:
            _join_thread(cancel_thread)

    assert not submit_thread.is_alive()
    assert cancel_thread is not None and not cancel_thread.is_alive()
    assert submissions[0]["status"] == "submitted"
    assert cancellations[0] is not None
    assert cancellations[0].status.value == "cancelled"
    assert events == ["record_started", "record_finished", "cancel_finished"]
    terminal = list_queue(queue_root)[0]
    assert terminal.status.value == "cancelled"
    assert terminal.metadata[QUEUE_RECORD_SYNC_KEY] == "complete"


def test_cancel_before_fresh_publication_revokes_fence_and_skips_all_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    record_calls: list[str] = []
    cancellations: list[QueueEntry] = []

    monkeypatch.setattr(crest_submitter, "load_config", lambda _path: object())
    monkeypatch.setattr(crest_submitter, "resolve_job_dir", lambda *_args: job_dir)
    monkeypatch.setattr(crest_submitter, "load_job_manifest", lambda _job_dir: {})
    monkeypatch.setattr(
        crest_submitter,
        "build_submission",
        lambda _cfg, _job_dir, _manifest, args: SimpleNamespace(
            queue_root=queue_root,
            app_name="orca_auto_crest",
            task_id="crest-cancel-before-publish",
            task_kind="crest_conformer_search",
            engine="crest",
            priority=int(args.priority),
            metadata={"job_dir": str(job_dir)},
            context={},
        ),
    )

    def enqueue_then_cancel(root: Path, **kwargs: Any) -> QueueEntry:
        entry = enqueue(root, **kwargs)
        cancelled = request_cancel(root, entry.queue_id)
        assert cancelled is not None
        cancellations.append(cancelled)
        return entry

    monkeypatch.setattr(crest_submitter, "enqueue", enqueue_then_cancel)
    monkeypatch.setattr(
        crest_submitter,
        "record_queued",
        lambda *_args: record_calls.append("recorded"),
    )

    result = crest_submitter.submit_job_dir(
        job_dir=str(job_dir),
        priority=4,
        config_path="/tmp/config.yaml",
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "cancel_requested"
    assert cancellations[0].status.value == "cancelled"
    assert record_calls == []
    terminal = list_queue(queue_root)[0]
    assert terminal.status.value == "cancelled"
    assert terminal.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED


def test_cancel_during_repair_publication_waits_for_complete_fenced_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    existing = enqueue(
        queue_root,
        app_name="orca_auto_crest",
        task_id="crest-repair-original",
        task_kind="crest_conformer_search",
        engine="crest",
        metadata={
            "job_dir": str(job_dir),
            QUEUE_RECORD_SYNC_KEY: (QUEUE_RECORD_SYNC_REPAIR_PENDING),
        },
    )
    repair_started = Event()
    allow_repair = Event()
    cancel_started = Event()
    cancel_finished = Event()
    events: list[str] = []
    replays: list[dict[str, Any]] = []
    cancellations: list[QueueEntry | None] = []

    monkeypatch.setattr(crest_submitter, "load_config", lambda _path: object())
    monkeypatch.setattr(crest_submitter, "resolve_job_dir", lambda *_args: job_dir)
    monkeypatch.setattr(crest_submitter, "load_job_manifest", lambda _job_dir: {})
    monkeypatch.setattr(
        crest_submitter,
        "build_submission",
        lambda _cfg, _job_dir, _manifest, args: SimpleNamespace(
            queue_root=queue_root,
            app_name="orca_auto_crest",
            task_id="crest-repair-replay",
            task_kind="crest_conformer_search",
            engine="crest",
            priority=int(args.priority),
            metadata={"job_dir": str(job_dir)},
            context={},
        ),
    )

    def record_queued(_cfg: Any, _submission: Any, _entry: Any) -> None:
        events.append("repair_started")
        repair_started.set()
        allow_repair.wait()
        events.append("repair_finished")

    monkeypatch.setattr(crest_submitter, "record_queued", record_queued)
    replay_thread = Thread(
        target=lambda: replays.append(
            crest_submitter.submit_job_dir(
                job_dir=str(job_dir),
                priority=7,
                config_path="/tmp/config.yaml",
            )
        )
    )
    replay_thread.start()
    cancel_thread: Thread | None = None
    try:
        assert _wait_for_thread_event(repair_started, replay_thread)
        repairing = list_queue(queue_root)[0]
        assert repairing.metadata[QUEUE_RECORD_SYNC_KEY] == "repairing"

        def cancel() -> None:
            cancel_started.set()
            cancellations.append(request_cancel(queue_root, existing.queue_id))
            events.append("cancel_finished")
            cancel_finished.set()

        cancel_thread = Thread(target=cancel)
        cancel_thread.start()
        assert _wait_for_thread_event(cancel_started, cancel_thread)
        assert not cancel_finished.wait(timeout=0.25)
        assert list_queue(queue_root)[0].status.value == "pending"
    finally:
        allow_repair.set()
        _join_thread(replay_thread)
        if cancel_thread is not None:
            _join_thread(cancel_thread)

    assert not replay_thread.is_alive()
    assert cancel_thread is not None and not cancel_thread.is_alive()
    assert replays[0]["status"] == "submitted"
    assert cancellations[0] is not None
    assert cancellations[0].status.value == "cancelled"
    assert events == ["repair_started", "repair_finished", "cancel_finished"]
    terminal = list_queue(queue_root)[0]
    assert terminal.status.value == "cancelled"
    assert terminal.metadata[QUEUE_RECORD_SYNC_KEY] == "complete"


def test_cancel_after_repair_claim_revokes_fence_before_any_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    existing = enqueue(
        queue_root,
        app_name="orca_auto_crest",
        task_id="crest-repair-cancel-first",
        task_kind="crest_conformer_search",
        engine="crest",
        metadata={
            "job_dir": str(job_dir),
            QUEUE_RECORD_SYNC_KEY: (QUEUE_RECORD_SYNC_REPAIR_PENDING),
        },
    )
    waiting_before_lock = Event()
    allow_publication_lock = Event()
    record_calls: list[str] = []
    replays: list[dict[str, Any]] = []

    monkeypatch.setattr(crest_submitter, "load_config", lambda _path: object())
    monkeypatch.setattr(crest_submitter, "resolve_job_dir", lambda *_args: job_dir)
    monkeypatch.setattr(crest_submitter, "load_job_manifest", lambda _job_dir: {})
    monkeypatch.setattr(
        crest_submitter,
        "build_submission",
        lambda _cfg, _job_dir, _manifest, args: SimpleNamespace(
            queue_root=queue_root,
            app_name="orca_auto_crest",
            task_id="crest-repair-cancel-replay",
            task_kind="crest_conformer_search",
            engine="crest",
            priority=int(args.priority),
            metadata={"job_dir": str(job_dir)},
            context={},
        ),
    )
    monkeypatch.setattr(
        crest_submitter,
        "record_queued",
        lambda *_args: record_calls.append("recorded"),
    )
    original_publication_lock = core_enqueue_publication.queue_record_publication_lock

    @contextmanager
    def delayed_publication_lock(root: Path, queue_id: str) -> Any:
        waiting_before_lock.set()
        allow_publication_lock.wait()
        with original_publication_lock(root, queue_id):
            yield

    monkeypatch.setattr(
        core_enqueue_publication,
        "queue_record_publication_lock",
        delayed_publication_lock,
    )

    replay_thread = Thread(
        target=lambda: replays.append(
            crest_submitter.submit_job_dir(
                job_dir=str(job_dir),
                priority=7,
                config_path="/tmp/config.yaml",
            )
        )
    )
    replay_thread.start()
    try:
        assert _wait_for_thread_event(waiting_before_lock, replay_thread)
        waiting = list_queue(queue_root)[0]
        assert waiting.metadata[QUEUE_RECORD_SYNC_KEY] == "repair_pending"

        cancelled = request_cancel(queue_root, existing.queue_id)
        assert cancelled is not None
        assert cancelled.status.value == "cancelled"
        assert cancelled.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED
    finally:
        allow_publication_lock.set()
        _join_thread(replay_thread)

    assert not replay_thread.is_alive()
    assert replays[0]["status"] == "blocked"
    assert replays[0]["reason"] == "cancel_requested"
    assert record_calls == []
    terminal = list_queue(queue_root)[0]
    assert terminal.status.value == "cancelled"
    assert terminal.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED


def test_repair_pending_replay_repairs_before_dequeue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    task_ids = iter(("crest-original", "crest-replay"))
    record_calls: list[str] = []

    monkeypatch.setattr(crest_submitter, "load_config", lambda _path: object())
    monkeypatch.setattr(crest_submitter, "resolve_job_dir", lambda *_args: job_dir)
    monkeypatch.setattr(crest_submitter, "load_job_manifest", lambda _job_dir: {})
    monkeypatch.setattr(
        crest_submitter,
        "build_submission",
        lambda _cfg, _job_dir, _manifest, args: SimpleNamespace(
            queue_root=queue_root,
            app_name="orca_auto_crest",
            task_id=next(task_ids),
            task_kind="crest_conformer_search",
            engine="crest",
            priority=int(args.priority),
            metadata={"job_dir": str(job_dir), "mode": "standard"},
            context={},
        ),
    )

    def record_queued(_cfg: Any, submission: Any, _entry: Any) -> None:
        record_calls.append(submission.task_id)
        if len(record_calls) == 1:
            raise OSError("first queued record write failed")

    monkeypatch.setattr(crest_submitter, "record_queued", record_queued)

    first = crest_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=4, config_path="/tmp/config.yaml"
    )
    assert dequeue_next(queue_root) is None

    replay = crest_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=5, config_path="/tmp/config.yaml"
    )
    repaired = list_queue(queue_root)[0]

    assert first["status"] == "submitted"
    assert "state/index repaired" in replay["stderr"]
    assert record_calls == ["crest-original", "crest-original"]
    assert repaired.status.value == "pending"
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == "complete"
    running = dequeue_next(queue_root)
    assert running is not None and running.queue_id == repaired.queue_id


def test_known_notification_failure_is_warned_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    monkeypatch.setattr(crest_submitter, "load_config", lambda _path: object())
    monkeypatch.setattr(crest_submitter, "resolve_job_dir", lambda *_args: job_dir)
    monkeypatch.setattr(crest_submitter, "load_job_manifest", lambda _job_dir: {})
    monkeypatch.setattr(
        crest_submitter,
        "build_submission",
        lambda _cfg, _job_dir, _manifest, args: SimpleNamespace(
            queue_root=queue_root,
            app_name="orca_auto_crest",
            task_id="crest-notification-failed",
            task_kind="crest_conformer_search",
            engine="crest",
            priority=int(args.priority),
            metadata={"job_dir": str(job_dir)},
            context={},
        ),
    )
    monkeypatch.setattr(crest_submitter, "record_queued", lambda *_args: False)

    result = crest_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=4, config_path="/tmp/config.yaml"
    )

    assert result["status"] == "submitted"
    assert "at-most-once delivery" in result["stderr"]
    entry = list_queue(queue_root)[0]
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == "complete"


def test_same_job_dir_contention_is_atomic_across_processes(tmp_path: Path) -> None:
    ctx = get_context("fork")
    queue_root = tmp_path / "queue"
    job_dir = tmp_path / "job"
    start_event = ctx.Event()
    result_queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_concurrent_internal_enqueue,
            args=(str(queue_root), str(job_dir), f"job-{index}", start_event, result_queue),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    outcomes = [result_queue.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert sorted(status for status, _detail in outcomes) == ["_ActiveJobDirReplay", "enqueued"]
    assert len(list_queue(queue_root)) == 1


@pytest.mark.parametrize("module", [xtb_submitter, crest_submitter])
def test_submit_job_dir_reports_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
) -> None:
    def fake_load_config(_config_path: str) -> Any:
        raise RuntimeError("submission failed")

    monkeypatch.setattr(module, "load_config", fake_load_config)

    result = module.submit_job_dir(
        job_dir="/jobs/job-1",
        priority=4,
        config_path="/tmp/config.yaml",
    )

    assert result["status"] == "failed"
    assert result["returncode"] == 1
    assert result["stdout"] == ""
    assert result["stderr"] == "RuntimeError: submission failed\n"
    assert result["parsed_stdout"] == {}
    assert result["job_id"] == ""
    assert result["queue_id"] == ""


def test_snapshot_marker_is_quiet_when_worker_already_retired_the_intent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue_root, job_dir, submission, _snapshot_path = _submission_with_generation_snapshot(
        tmp_path,
        task_id="crest-retired-intent",
    )
    token = submission.metadata["execution_snapshot"][SNAPSHOT_INTENT_TOKEN_KEY]
    transition_snapshot_intent(
        queue_root,
        token,
        target_state=SNAPSHOT_INTENT_STATE_ENQUEUEING,
        expected_states={SNAPSHOT_INTENT_STATE_CREATING},
    )
    # The worker retires an intent as soon as a committed queue row references it.
    discard_snapshot_intent(queue_root, token)
    state = internal_engine_submission._InternalEngineSubmissionState(resolved_job_dir=job_dir)

    marker_logger = "orca_auto.core.queue.engine.snapshot_intent"
    with caplog.at_level(logging.INFO, logger=marker_logger):
        internal_engine_submission._mark_submission_snapshot_owned(submission, state)

    assert state.warning == ""
    records = [record for record in caplog.records if record.name == marker_logger]
    assert not [record for record in records if record.levelno >= logging.WARNING]
    assert any("already retired" in record.getMessage() for record in records)
