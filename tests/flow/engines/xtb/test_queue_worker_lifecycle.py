from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.flow.engines.xtb import queue_runtime as queue_cmd
from orca_auto.flow.engines.xtb import state as state_mod
from tests.engine_artifact_helpers import artifact_payload
from tests.engine_process_helpers import process_one_xtb_for_test
from tests.flow.engines.xtb.factories import (
    make_cfg as _make_cfg,
)
from tests.flow.engines.xtb.factories import (
    make_entry as _make_entry,
)


def _dequeued_running_entry(entry: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(**{**vars(entry), "status": SimpleNamespace(value="running")})


def test_queue_worker_parser_has_no_organize_flags() -> None:
    args = queue_cmd.build_parser().parse_args(["--config", "/tmp/orca_auto.yaml"])

    assert args.config == "/tmp/orca_auto.yaml"
    assert not hasattr(args, "auto_organize")
    assert not hasattr(args, "no_auto_organize")

    with pytest.raises(SystemExit):
        queue_cmd.build_parser().parse_args(["--config", "/tmp/orca_auto.yaml", "--auto-organize"])


def test_process_one_returns_blocked_when_no_admission_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)

    monkeypatch.setattr(queue_cmd, "_try_reserve_admission_slot", lambda _cfg: None)

    assert process_one_xtb_for_test(queue_cmd, cfg) == "blocked"


def test_process_one_returns_idle_and_releases_reserved_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    released: list[tuple[object, object]] = []

    monkeypatch.setattr(queue_cmd, "_try_reserve_admission_slot", lambda _cfg: "slot-1")
    monkeypatch.setattr(queue_cmd, "dequeue_next", lambda _root, **_kw: None)
    monkeypatch.setattr(
        queue_cmd, "release_slot", lambda root, token: released.append((str(root), token))
    )

    assert process_one_xtb_for_test(queue_cmd, cfg) == "idle"
    assert released == [(cfg.runtime.admission_root, "slot-1")]


def test_queue_worker_starts_up_to_max_concurrent_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    entries = []
    for index in range(2):
        job_dir = queue_root / f"job-{index}"
        job_dir.mkdir()
        selected_xyz = job_dir / f"input-{index}.xyz"
        selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
        entries.append(
            _make_entry(
                job_dir,
                selected_xyz,
                queue_id=f"queue-{index}",
                job_id=f"job-{index}",
                status="pending",
            )
        )

    slots = iter(["slot-1", "slot-2"])
    dequeued = iter(entries)
    started: list[tuple[str, str, str]] = []

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    monkeypatch.setattr(queue_cmd, "_try_reserve_admission_slot", lambda _cfg: next(slots))
    monkeypatch.setattr(queue_cmd, "list_queue", lambda _root: entries)
    monkeypatch.setattr(queue_cmd, "dequeue_next", lambda _root, **_kw: next(dequeued))
    monkeypatch.setattr(queue_cmd, "activate_reserved_slot", lambda *args, **kwargs: object())
    real_deps = queue_cmd._queue_worker_deps()
    monkeypatch.setattr(
        queue_cmd,
        "_queue_worker_deps",
        lambda: replace(
            real_deps,
            dequeue_next_entry=lambda _cfg: (queue_root, _dequeued_running_entry(next(dequeued))),
        ),
    )

    def fake_start_background_job_process(
        *,
        config_path: str,
        queue_root: Path,
        entry: object,
        admission_root: str,
        admission_token: str,
    ) -> _Process:
        started.append((config_path, str(queue_root), admission_token))
        return _Process(len(started) + 100)

    monkeypatch.setattr(
        queue_cmd,
        "_start_background_job_process",
        fake_start_background_job_process,
    )

    worker = queue_cmd.QueueWorker(
        cfg,
        config_path="/tmp/orca_auto.yaml",
        max_concurrent=2,
    )

    assert worker._fill_slots() == "processed"
    assert sorted(worker._running) == ["queue-0", "queue-1"]
    assert started == [
        ("/tmp/orca_auto.yaml", str(queue_root), "slot-1"),
        ("/tmp/orca_auto.yaml", str(queue_root), "slot-2"),
    ]


def test_queue_worker_check_cancel_requests_is_child_side_noop(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz, status="pending")

    signals: list[int] = []

    class _Process:
        pid = 1234

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def send_signal(self, signum: int) -> None:
            signals.append(signum)

    worker = queue_cmd.QueueWorker(cfg, config_path="/tmp/cfg.yaml")
    worker._running[entry.queue_id] = queue_cmd._RunningJob(
        queue_root=queue_root,
        entry=entry,
        process=_Process(),
        admission_token="slot-1",
    )

    worker._check_cancel_requests()
    worker._check_cancel_requests()

    assert signals == []
    assert worker._running[entry.queue_id].cancel_requested is False


def test_queue_worker_shutdown_requeues_running_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz)

    graceful_terminated: list[int] = []
    hard_terminated: list[int] = []
    requeued: list[tuple[Path, str]] = []
    released: list[tuple[str, str]] = []

    class _Process:
        pid = 9001

        def __init__(self) -> None:
            self._terminated = False

        def poll(self) -> int | None:
            return 0 if self._terminated else None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            graceful_terminated.append(self.pid)
            self._terminated = True

        def kill(self) -> None:
            return None

    monkeypatch.setattr(
        queue_cmd,
        "_terminate_process",
        lambda proc: hard_terminated.append(proc.pid),
    )
    monkeypatch.setattr(
        queue_cmd, "requeue_running_entry", lambda root, queue_id: requeued.append((root, queue_id))
    )
    monkeypatch.setattr(queue_cmd, "_queue_entry_by_id", lambda _root, _queue_id: entry)
    monkeypatch.setattr(
        queue_cmd, "release_slot", lambda root, token: released.append((str(root), token))
    )

    worker = queue_cmd.QueueWorker(cfg, config_path="/tmp/cfg.yaml")
    worker._shutdown_requested = True
    worker._running[entry.queue_id] = queue_cmd._RunningJob(
        queue_root=queue_root,
        entry=entry,
        process=_Process(),
        admission_token="slot-1",
    )

    worker._shutdown_all()

    assert graceful_terminated == [9001]
    assert hard_terminated == []
    assert requeued == [(queue_root, "queue-1")]
    assert released == [(cfg.runtime.admission_root, "slot-1")]
    assert worker._running == {}
    state = state_mod.load_state(job_dir)
    assert state is not None
    assert state["status"]["state"] == "queued"
    assert state["status"]["reason"] == "worker_shutdown"
    assert state["recovery"]["pending"] is True


def test_queue_worker_run_once_waits_for_child_completion_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz)
    sleep_calls: list[float] = []
    released: list[tuple[str, str]] = []

    class _Process:
        def __init__(self) -> None:
            self.pid = 4444
            self._poll_values = iter([None, 0])

        def poll(self) -> int | None:
            return next(self._poll_values)

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    monkeypatch.setattr(queue_cmd, "reconcile_stale_slots", lambda _root: 0)
    monkeypatch.setattr(queue_cmd, "list_queue", lambda _root: [entry])
    monkeypatch.setattr(queue_cmd, "_try_reserve_admission_slot", lambda _cfg: "slot-1")
    monkeypatch.setattr(queue_cmd, "dequeue_next", lambda _root, **_kw: entry)
    monkeypatch.setattr(queue_cmd, "activate_reserved_slot", lambda *args, **kwargs: object())
    real_deps = queue_cmd._queue_worker_deps()
    monkeypatch.setattr(
        queue_cmd,
        "_queue_worker_deps",
        lambda: replace(
            real_deps,
            dequeue_next_entry=lambda _cfg: (queue_root, _dequeued_running_entry(entry)),
        ),
    )
    monkeypatch.setattr(
        queue_cmd,
        "_start_background_job_process",
        lambda **kwargs: _Process(),
    )
    monkeypatch.setattr(
        queue_cmd,
        "_load_terminal_summary",
        lambda queue_root, entry, rc=None: queue_cmd._TerminalSummary(
            queue_id=entry.queue_id,
            job_id=entry.task_id,
            status="completed",
            reason="xtb_ok",
        ),
    )
    monkeypatch.setattr(
        queue_cmd, "_ensure_terminal_queue_status", lambda queue_root, entry, summary: None
    )
    monkeypatch.setattr(
        queue_cmd, "release_slot", lambda root, token: released.append((str(root), token))
    )
    monkeypatch.setattr(queue_cmd.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    worker = queue_cmd.QueueWorker(cfg, config_path="/tmp/cfg.yaml")
    exit_code = worker.run_once()

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "status: completed" in output
    assert "reason: xtb_ok" in output
    assert sleep_calls == [queue_cmd.POLL_INTERVAL_SECONDS]
    assert released == [(cfg.runtime.admission_root, "slot-1")]


def test_queue_worker_reconcile_worker_state_requeues_stale_running_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz, status="running")
    state_mod.write_state(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id="job-1",
            job_dir=str(job_dir),
            status="running",
            primary_path=str(selected_xyz),
            selected_xyz_path=str(selected_xyz),
            engine_payload={
                "job_type": "path_search",
                "reaction_key": "rxn-1",
            },
        )
        | {"process": {"worker_pid": 999_999}},
    )
    requeued: list[tuple[Path, str]] = []

    monkeypatch.setattr(queue_cmd, "reconcile_stale_slots", lambda _root: 0)
    monkeypatch.setattr(queue_cmd, "list_queue", lambda _root: [entry])
    monkeypatch.setattr(queue_cmd, "_pid_is_alive", lambda _pid: False)
    monkeypatch.setattr(
        queue_cmd, "requeue_running_entry", lambda root, queue_id: requeued.append((root, queue_id))
    )

    worker = queue_cmd.QueueWorker(cfg, config_path="/tmp/cfg.yaml")
    worker._reconcile_worker_state()

    assert requeued == [(queue_root, "queue-1")]
    state = state_mod.load_state(job_dir)
    assert state is not None
    assert state["status"]["state"] == "queued"
    assert state["status"]["reason"] == "crashed_recovery"
    assert state["recovery"]["pending"] is True


def test_cmd_queue_worker_constructs_xtb_worker_without_organize_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    seen: list[tuple[str, str]] = []

    class _FakeWorker:
        def __init__(
            self,
            cfg_obj: object,
            *,
            config_path: str,
            max_concurrent: int | None = None,
        ) -> None:
            seen.append(("init", config_path))

        def run_once(self) -> int:
            seen.append(("run_once", ""))
            return 17

        def run(self) -> int:
            seen.append(("run", ""))
            return 23

    monkeypatch.setattr(queue_cmd, "load_config", lambda _path=None: cfg)
    monkeypatch.setattr(queue_cmd, "QueueWorker", _FakeWorker)
    monkeypatch.setattr(queue_cmd, "default_config_path", lambda: "/tmp/default-orca_auto.yaml")

    exit_code = queue_cmd.cmd_queue_worker(
        SimpleNamespace(
            config=None,
        )
    )

    assert seen[0] == ("init", "/tmp/default-orca_auto.yaml")
    assert exit_code == 23
    assert seen[-1] == ("run", "")
