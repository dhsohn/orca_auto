from __future__ import annotations

import sys
from contextlib import contextmanager
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto import cli_worker_specs
from orca_auto.core.queue import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
    DuplicateQueueEntryError,
    QueueEntry,
    dequeue_next,
    enqueue,
    list_queue,
    mark_cancelled,
    request_cancel,
    requeue_running_entry,
    update_metadata,
)
from orca_auto.flow import engine_runtime
from orca_auto.flow.submitters import (
    crest as crest_submitter,
)
from orca_auto.flow.submitters import (
    internal_engine_submission,
)
from orca_auto.flow.submitters import (
    xtb as xtb_submitter,
)


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


def test_queue_submission_status_treats_admission_wait_as_blocked() -> None:
    status, reason = internal_engine_submission.queue_submission_status(
        returncode=1,
        parsed_stdout={"status": "waiting_for_slot"},
        stdout="status: waiting_for_slot\n",
        stderr="Admission limit reached",
    )

    assert status == "blocked"
    assert reason == "waiting_for_slot"


def test_worker_module_command_without_repo_root_uses_module_execution() -> None:
    argv, cwd, env = cli_worker_specs.worker_module_command(
        config_path="/tmp/config.yaml",
        repo_root=None,
        module_name="orca_auto.orca.commands.queue",
        tail_argv=["--engine", "orca"],
    )

    assert argv == [
        sys.executable,
        "-m",
        "orca_auto.orca.commands.queue",
        "--config",
        "/tmp/config.yaml",
        "--engine",
        "orca",
    ]
    assert cwd is None
    assert env is None


def test_worker_module_command_with_repo_root_uses_module_execution_and_prepends_pythonpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setenv("PYTHONPATH", "/existing/site-packages")

    argv, cwd, env = cli_worker_specs.worker_module_command(
        config_path="/tmp/config.yaml",
        repo_root=str(repo_root),
        module_name="orca_auto.cli",
        tail_argv=["queue", "cancel", "job-1"],
    )

    assert argv == [
        sys.executable,
        "-m",
        "orca_auto.cli",
        "--config",
        "/tmp/config.yaml",
        "queue",
        "cancel",
        "job-1",
    ]
    assert cwd == str(repo_root.resolve())
    assert env is not None
    assert env["PYTHONPATH"] == f"{repo_root.resolve()}:/existing/site-packages"


def test_engine_runtime_paths_reads_top_level_runs_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    assert engine_runtime.engine_runtime_paths(str(config_path)) == {
        "workflow_root": runs_root.resolve(),
        "allowed_root": runs_root.resolve(),
        "admission_root": runs_root.resolve() / ".admission",
    }


def test_engine_runtime_paths_requires_runs_root(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("scheduler:\n  max_active_simulations: 4\n", encoding="utf-8")

    for engine in (None, "orca", "xtb", "crest"):
        with pytest.raises(ValueError, match="Missing runs_root"):
            engine_runtime.engine_runtime_paths(str(config_path), engine=engine)


def test_engine_runtime_paths_rejects_invalid_runs_root_before_resolving(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"

    config_path.write_text("runs_root: './runs'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="absolute Linux path"):
        engine_runtime.engine_runtime_paths(str(config_path), engine="orca")

    config_path.write_text("runs_root: '/mnt/c/runs'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Linux path"):
        engine_runtime.engine_runtime_paths(str(config_path), engine="xtb")


def test_engine_runtime_paths_ignores_legacy_root_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "workflow:",
                "  root: /tmp/wf",
                "orca:",
                "  runtime:",
                "    allowed_root: /tmp/runs",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Missing runs_root"):
        engine_runtime.engine_runtime_paths(str(config_path), engine="orca")


def test_engine_runtime_paths_all_engines_share_the_runs_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    expected = {
        "workflow_root": runs_root.resolve(),
        "allowed_root": runs_root.resolve(),
        "admission_root": runs_root.resolve() / ".admission",
    }
    for engine in ("orca", "xtb", "crest"):
        assert engine_runtime.engine_runtime_paths(str(config_path), engine=engine) == expected


def test_engine_runtime_paths_uses_scheduler_admission_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    admission_root = tmp_path / "admission"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {runs_root}",
                "scheduler:",
                "  max_active_simulations: 4",
                f"  admission_root: {admission_root}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    for engine in (None, "orca", "xtb", "crest"):
        paths = engine_runtime.engine_runtime_paths(str(config_path), engine=engine)
        assert paths["allowed_root"] == runs_root.resolve()
        assert paths["admission_root"] == admission_root.resolve()


def test_engine_runtime_paths_rejects_engine_scoped_scheduler_override(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    shared_admission = tmp_path / "shared-admission"
    orca_admission = tmp_path / "orca-admission"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {runs_root}",
                "scheduler:",
                f"  admission_root: {shared_admission}",
                "orca:",
                "  scheduler:",
                f"    admission_root: {orca_admission}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cannot override the shared top-level scheduler"):
        engine_runtime.engine_runtime_paths(str(config_path), engine="orca")

    # Internal engines still resolve the single shared scheduler section.
    xtb_paths = engine_runtime.engine_runtime_paths(str(config_path), engine="xtb")
    assert xtb_paths["admission_root"] == shared_admission.resolve()


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
        return SimpleNamespace(
            queue_id=queue_id,
            task_id=kwargs["task_id"],
            priority=kwargs["priority"],
        )

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
    assert enqueue_metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "preparing"
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
    assert result["queue_id"] == queue_id
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
    assert (
        entries[0].metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "repair_pending"
    )
    assert result["status"] == "submitted"
    assert result["returncode"] == 0
    assert result["queue_id"] == entries[0].queue_id
    assert result["job_id"] == f"{engine}-job-1"
    warning = "queued job record update failed; queue submission succeeded"
    assert warning in result["parsed_stdout"]["warning"]
    assert "OSError: disk full after enqueue" in result["stderr"]


def test_xtb_active_replay_uses_existing_entry_identity_and_metadata(
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
    assert replay["job_id"] == "xtb-old"
    assert replay["parsed_stdout"]["priority"] == "4"
    assert replay["job_type"] == "opt"
    assert replay["reaction_key"] == "old-rxn"
    entry = list_queue(queue_root)[0]
    assert entry.task_id == "xtb-old"
    assert entry.task_kind == "xtb_opt"
    assert entry.metadata["job_type"] == "opt"
    assert entry.metadata["reaction_key"] == "old-rxn"


def test_legacy_pending_entry_without_sync_marker_is_repaired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    legacy = enqueue(
        queue_root,
        app_name="orca_auto_crest",
        task_id="crest-legacy",
        task_kind="crest_conformer_search",
        engine="crest",
        metadata={"job_dir": str(job_dir), "mode": "nci"},
    )
    record_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(crest_submitter, "load_config", lambda _path: object())
    monkeypatch.setattr(crest_submitter, "resolve_job_dir", lambda *_args: job_dir)
    monkeypatch.setattr(crest_submitter, "load_job_manifest", lambda _job_dir: {})
    monkeypatch.setattr(
        crest_submitter,
        "build_submission",
        lambda _cfg, _job_dir, _manifest, args: SimpleNamespace(
            queue_root=queue_root,
            app_name="orca_auto_crest",
            task_id="crest-new",
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
        lambda _cfg, submission, _entry: record_calls.append(
            (
                submission.task_id,
                bool(submission.context.get("suppress_queued_notification", False)),
            )
        ),
    )

    replay = crest_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=8, config_path="/tmp/config.yaml"
    )

    assert replay["job_id"] == legacy.task_id
    assert record_calls == [("crest-legacy", True)]
    repaired = list_queue(queue_root)[0]
    assert repaired.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "complete"


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

    assert "record update failed" in first["parsed_stdout"]["warning"]
    assert "state/index repaired" in replay["parsed_stdout"]["warning"]
    assert states == [("crest-original", "nci"), ("crest-original", "nci")]
    assert indexes == states
    assert notifications == ["crest-original"]
    entry = list_queue(queue_root)[0]
    assert entry.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "complete"


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
        assert allow_publish.wait(timeout=5)

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
        assert publish_started.wait(timeout=5)
        publishing = list_queue(queue_root)[0]
        assert (
            publishing.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "preparing"
        )
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
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert results[0]["status"] == "submitted"
    published = list_queue(queue_root)[0]
    assert published.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "complete"
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
        assert allow_publish.wait(timeout=5)
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
        assert publish_started.wait(timeout=5)
        queue_id = list_queue(queue_root)[0].queue_id

        def cancel() -> None:
            cancel_started.set()
            cancellations.append(request_cancel(queue_root, queue_id))
            events.append("cancel_finished")
            cancel_finished.set()

        cancel_thread = Thread(target=cancel)
        cancel_thread.start()
        assert cancel_started.wait(timeout=5)
        assert not cancel_finished.wait(timeout=0.25)
        publishing = list_queue(queue_root)[0]
        assert publishing.status.value == "pending"
        assert (
            publishing.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "preparing"
        )
    finally:
        allow_publish.set()
        submit_thread.join(timeout=5)
        if cancel_thread is not None:
            cancel_thread.join(timeout=5)

    assert not submit_thread.is_alive()
    assert cancel_thread is not None and not cancel_thread.is_alive()
    assert submissions[0]["status"] == "submitted"
    assert cancellations[0] is not None
    assert cancellations[0].status.value == "cancelled"
    assert events == ["record_started", "record_finished", "cancel_finished"]
    terminal = list_queue(queue_root)[0]
    assert terminal.status.value == "cancelled"
    assert terminal.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "complete"


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
    assert (
        terminal.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY]
        == QUEUE_RECORD_SYNC_ABORTED
    )


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
            internal_engine_submission._QUEUED_RECORD_SYNC_KEY: (
                internal_engine_submission._QUEUED_RECORD_SYNC_PENDING
            ),
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
        assert allow_repair.wait(timeout=5)
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
        assert repair_started.wait(timeout=5)
        repairing = list_queue(queue_root)[0]
        assert repairing.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "repairing"

        def cancel() -> None:
            cancel_started.set()
            cancellations.append(request_cancel(queue_root, existing.queue_id))
            events.append("cancel_finished")
            cancel_finished.set()

        cancel_thread = Thread(target=cancel)
        cancel_thread.start()
        assert cancel_started.wait(timeout=5)
        assert not cancel_finished.wait(timeout=0.25)
        assert list_queue(queue_root)[0].status.value == "pending"
    finally:
        allow_repair.set()
        replay_thread.join(timeout=5)
        if cancel_thread is not None:
            cancel_thread.join(timeout=5)

    assert not replay_thread.is_alive()
    assert cancel_thread is not None and not cancel_thread.is_alive()
    assert replays[0]["status"] == "submitted"
    assert cancellations[0] is not None
    assert cancellations[0].status.value == "cancelled"
    assert events == ["repair_started", "repair_finished", "cancel_finished"]
    terminal = list_queue(queue_root)[0]
    assert terminal.status.value == "cancelled"
    assert terminal.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "complete"


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
            internal_engine_submission._QUEUED_RECORD_SYNC_KEY: (
                internal_engine_submission._QUEUED_RECORD_SYNC_PENDING
            ),
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
    original_publication_lock = internal_engine_submission.queue_record_publication_lock

    @contextmanager
    def delayed_publication_lock(root: Path, queue_id: str) -> Any:
        waiting_before_lock.set()
        assert allow_publication_lock.wait(timeout=5)
        with original_publication_lock(root, queue_id):
            yield

    monkeypatch.setattr(
        internal_engine_submission,
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
        assert waiting_before_lock.wait(timeout=5)
        repairing = list_queue(queue_root)[0]
        assert repairing.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "repairing"

        cancelled = request_cancel(queue_root, existing.queue_id)
        assert cancelled is not None
        assert cancelled.status.value == "cancelled"
        assert (
            cancelled.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY]
            == QUEUE_RECORD_SYNC_ABORTED
        )
    finally:
        allow_publication_lock.set()
        replay_thread.join(timeout=5)

    assert not replay_thread.is_alive()
    assert replays[0]["status"] == "blocked"
    assert replays[0]["reason"] == "cancel_requested"
    assert record_calls == []
    terminal = list_queue(queue_root)[0]
    assert terminal.status.value == "cancelled"
    assert (
        terminal.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY]
        == QUEUE_RECORD_SYNC_ABORTED
    )


def test_running_replay_keeps_repair_pending_until_requeue_can_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_dir = (tmp_path / "job").resolve()
    queue_root = tmp_path / "queue"
    task_ids = iter(("crest-original", "crest-running-replay", "crest-pending-replay"))
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
    running = dequeue_next(queue_root)
    assert running is not None

    replay_while_running = crest_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=5, config_path="/tmp/config.yaml"
    )
    running_entry = list_queue(queue_root)[0]

    assert "repair skipped because the worker is running" in replay_while_running["stderr"]
    assert (
        running_entry.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY]
        == "repair_pending"
    )
    assert requeue_running_entry(queue_root, running.queue_id) is not None

    replay_after_requeue = crest_submitter.submit_job_dir(
        job_dir=str(job_dir), priority=6, config_path="/tmp/config.yaml"
    )
    repaired = list_queue(queue_root)[0]

    assert first["status"] == "submitted"
    assert "state/index repaired" in replay_after_requeue["stderr"]
    assert record_calls == ["crest-original", "crest-original"]
    assert repaired.status.value == "pending"
    assert repaired.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "complete"


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
    assert entry.metadata[internal_engine_submission._QUEUED_RECORD_SYNC_KEY] == "complete"


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


@pytest.mark.parametrize(
    ("module", "engine", "target", "displayed_status", "expected_status", "queue_id", "job_id"),
    [
        (
            xtb_submitter,
            "xtb",
            "xtb-job-1",
            "cancel_requested",
            "cancel_requested",
            "q-1",
            "xtb-job-1",
        ),
        (xtb_submitter, "xtb", "xtb-job-2", "cancelled", "cancelled", "q-2", "xtb-job-2"),
        (
            crest_submitter,
            "crest",
            "crest-job-1",
            "cancel_requested",
            "cancel_requested",
            "c-1",
            "crest-job-1",
        ),
        (crest_submitter, "crest", "crest-job-2", "cancelled", "cancelled", "c-2", "crest-job-2"),
    ],
)
def test_cancel_target_uses_structured_queue_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    engine: str,
    target: str,
    displayed_status: str,
    expected_status: str,
    queue_id: str,
    job_id: str,
) -> None:
    captured: dict[str, Any] = {}
    queue_root = tmp_path / "queue"
    cfg = SimpleNamespace(name=f"{engine}-queue-config")
    original_entry = SimpleNamespace(queue_id=queue_id, task_id=job_id)

    def fake_load_queue_config(config_path: str) -> Any:
        captured["config_path"] = config_path
        return cfg

    def fake_queue_entries_with_roots(cfg_arg: Any) -> list[tuple[Path, Any]]:
        captured["listed_cfg"] = cfg_arg
        return [(queue_root, original_entry)]

    def fake_request_cancel(root: Path, requested_queue_id: str) -> Any:
        captured["request_cancel"] = (root, requested_queue_id)
        return SimpleNamespace(
            queue_id=requested_queue_id,
            task_id=job_id,
            cancel_requested=displayed_status == "cancel_requested",
            status=SimpleNamespace(
                value="running" if displayed_status == "cancel_requested" else "cancelled"
            ),
        )

    def fake_display_status(entry: Any) -> str:
        captured["display_entry"] = entry
        return displayed_status

    monkeypatch.setattr(module, "load_queue_config", fake_load_queue_config)
    monkeypatch.setattr(module, "queue_entries_with_roots", fake_queue_entries_with_roots)
    monkeypatch.setattr(module, "request_cancel", fake_request_cancel)
    monkeypatch.setattr(module, "display_status", fake_display_status)

    result = module.cancel_target(
        target=target,
        config_path="/tmp/config.yaml",
    )

    assert captured["config_path"] == "/tmp/config.yaml"
    assert captured["listed_cfg"] is cfg
    assert captured["request_cancel"] == (queue_root, queue_id)
    assert result["status"] == expected_status
    assert result["returncode"] == 0
    assert result["command_argv"] == [
        f"orca_auto.{engine}.queue_runtime.direct_cancel",
        "config=/tmp/config.yaml",
        f"target={target}",
    ]
    assert result["stdout"] == f"status: {expected_status}\nqueue_id: {queue_id}\njob_id: {job_id}"
    assert result["stderr"] == ""
    assert result["parsed_stdout"]["status"] == expected_status
    assert result["queue_id"] == queue_id
    assert result["job_id"] == job_id


@pytest.mark.parametrize("module", [xtb_submitter, crest_submitter])
def test_cancel_target_reports_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
) -> None:
    def fake_load_queue_config(_config_path: str) -> Any:
        return object()

    def fake_queue_entries_with_roots(_cfg: Any) -> list[tuple[Path, Any]]:
        return [(tmp_path / "queue", SimpleNamespace(queue_id="q-1", task_id="job-1"))]

    def fake_request_cancel(_root: Path, _queue_id: str) -> Any:
        raise RuntimeError("cancel failed")

    monkeypatch.setattr(module, "load_queue_config", fake_load_queue_config)
    monkeypatch.setattr(module, "queue_entries_with_roots", fake_queue_entries_with_roots)
    monkeypatch.setattr(module, "request_cancel", fake_request_cancel)

    result = module.cancel_target(
        target="job-1",
        config_path="/tmp/config.yaml",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "cancel_command_failed"
    assert result["returncode"] == 1
    assert result["stdout"] == ""
    assert result["stderr"] == "RuntimeError: cancel failed\n"
