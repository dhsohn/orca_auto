from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.orca import submission as submission_mod
from orca_auto.orca.commands.run_inp import cmd_run_inp
from orca_auto.orca.queue.adapter import enqueue, list_queue, queue_entry_metadata
from orca_auto.orca.run_lock import acquire_run_lock
from orca_auto.orca.submission import submit_reaction_dir_to_queue
from tests.conftest import make_queue_entry

DEFAULT_INP = "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"


def _write_inp(reaction_dir: Path, content: str = DEFAULT_INP) -> Path:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    inp = reaction_dir / "rxn.inp"
    inp.write_text(content, encoding="utf-8")
    return inp


def _make_args(config: Path | str, reaction_dir: Path, **overrides: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "config": str(config),
        "reaction_dir": str(reaction_dir),
        "force": False,
        "priority": 10,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _submitted(reaction_dir: Path, entry: Any, **worker: Any) -> SimpleNamespace:
    worker_info = {"status": None, "pid": None, "log_file": None, "detail": None, **worker}
    return SimpleNamespace(
        status="submitted",
        reason="",
        stderr="",
        context=SimpleNamespace(reaction_dir=reaction_dir),
        queued_result=SimpleNamespace(entry=entry, worker_info=SimpleNamespace(**worker_info)),
    )


@pytest.fixture
def reaction_dir(tmp_path: Path) -> Path:
    reaction = tmp_path / "rxn"
    _write_inp(reaction)
    return reaction


@pytest.fixture
def config(config_path: Callable[..., Path]) -> Path:
    return config_path(max_concurrent=1)


@pytest.fixture
def submit_to_queue(monkeypatch: pytest.MonkeyPatch) -> Callable[[SimpleNamespace], list[Any]]:
    """Replace the submission pipeline behind ``cmd_run_inp``; returns the recorded args."""

    def install(result: SimpleNamespace) -> list[Any]:
        calls: list[Any] = []

        def fake_submit(args: Any) -> SimpleNamespace:
            calls.append(args)
            return result

        monkeypatch.setattr(submission_mod, "submit_reaction_dir_to_queue", fake_submit)
        return calls

    return install


@dataclass
class _WorkerSeams:
    """The queue worker pid file and the messenger, as ``submission`` sees them."""

    worker_pid: int | None = None
    pid_reads: list[Path] = field(default_factory=list)
    notifications: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)


@pytest.fixture
def worker_seams(monkeypatch: pytest.MonkeyPatch) -> _WorkerSeams:
    seams = _WorkerSeams()

    def read_worker_pid(root: Path) -> int | None:
        seams.pid_reads.append(root)
        return seams.worker_pid

    def notify(*args: Any, **kwargs: Any) -> bool:
        seams.notifications.append((args, kwargs))
        return True

    monkeypatch.setattr(submission_mod, "read_worker_pid", read_worker_pid)
    monkeypatch.setattr(submission_mod, "notify_queue_enqueued_event", notify)
    return seams


def test_submit_always_enqueues_without_attempting_direct_execution(
    tmp_path: Path,
    reaction_dir: Path,
    submit_to_queue: Callable[[SimpleNamespace], list[Any]],
) -> None:
    entry = make_queue_entry(reaction_dir=reaction_dir)
    calls = submit_to_queue(_submitted(reaction_dir, entry))

    rc = cmd_run_inp(_make_args(tmp_path / "orca_auto.yaml", reaction_dir))

    assert rc == 0
    assert len(calls) == 1


def test_json_submission_emits_one_parseable_document(
    submit_to_queue: Callable[[SimpleNamespace], list[Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    reaction_dir = Path("/tmp/orca-json-job")
    entry = make_queue_entry(
        queue_id="q-json", task_id="orca-json", priority=7, reaction_dir=reaction_dir
    )
    submit_to_queue(_submitted(reaction_dir, entry, status="inactive", log_file="/tmp/q-json.log"))

    rc = cmd_run_inp(SimpleNamespace(config="/tmp/orca.yaml", priority=7, json=True))

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "queued",
        "job_dir": str(reaction_dir),
        "queue_id": "q-json",
        "job_id": "orca-json",
        "priority": 7,
        "worker": "inactive",
        "worker_log": "/tmp/q-json.log",
    }


@pytest.fixture
def refuse_queued_submission(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Fail the test if the submission pipeline reaches queue-row creation."""

    calls: list[Any] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        pytest.fail("create_queued_submission must not run for a rejected submission")

    monkeypatch.setattr(submission_mod, "create_queued_submission", refuse)
    return calls


def test_submit_rejects_when_active_queue_entry_exists_for_same_reaction_dir(
    tmp_path: Path,
    reaction_dir: Path,
    config: Path,
    refuse_queued_submission: list[Any],
) -> None:
    enqueue(tmp_path, str(reaction_dir))

    rc = cmd_run_inp(_make_args(config, reaction_dir))

    assert rc == 1
    assert refuse_queued_submission == []


def test_submit_rejects_when_same_reaction_dir_is_already_running_directly(
    reaction_dir: Path,
    config: Path,
    refuse_queued_submission: list[Any],
) -> None:
    with acquire_run_lock(reaction_dir):
        rc = cmd_run_inp(_make_args(config, reaction_dir))

    assert rc == 1
    assert refuse_queued_submission == []


def test_submit_queues_completed_output_for_worker_reconciliation(
    tmp_path: Path,
    reaction_dir: Path,
    submit_to_queue: Callable[[SimpleNamespace], list[Any]],
) -> None:
    (reaction_dir / "rxn.out").write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
    entry = make_queue_entry(reaction_dir=reaction_dir)
    calls = submit_to_queue(_submitted(reaction_dir, entry))

    rc = cmd_run_inp(_make_args(tmp_path / "orca_auto.yaml", reaction_dir))

    assert rc == 0
    assert len(calls) == 1


def test_submit_reaction_dir_to_queue_reports_inactive_worker_without_autostart(
    tmp_path: Path,
    reaction_dir: Path,
    config: Path,
    worker_seams: _WorkerSeams,
) -> None:
    root = tmp_path

    submission = submit_reaction_dir_to_queue(_make_args(config, reaction_dir, priority=3))

    entries = list_queue(root)

    assert submission.status == "submitted"
    result = submission.queued_result
    assert result is not None
    assert len(entries) == 1
    entry = entries[0]
    metadata = queue_entry_metadata(entry)
    assert entry.priority == 3
    assert entry.app_name == "orca_auto_orca"
    assert entry.task_id.startswith("orca_")
    assert metadata["source_selected_inp"] == str(reaction_dir / "rxn.inp")
    execution_dir = Path(metadata["execution_snapshot"]["execution_dir"])
    assert execution_dir.parent == reaction_dir.resolve()
    assert Path(metadata["selected_inp"]) == execution_dir / "rxn.inp"
    assert not (reaction_dir / ".orca_auto_orca_executions").exists()
    assert not (reaction_dir / ".orca_auto_input_snapshots").exists()
    assert metadata["execution_snapshot"]["selected_inp"] == metadata["selected_inp"]
    assert metadata["selected_input_path"] == str(reaction_dir / "rxn.inp")
    assert metadata["selected_input_xyz"] == ""
    assert "max_retries" not in metadata
    assert metadata["submitted_via"] == "run_inp"
    assert metadata["job_type"] == "opt"
    assert metadata["worker_log"] == str((root / "logs" / f"{entry.queue_id}.log").resolve())
    assert str(metadata["molecule_key"]).strip()
    assert metadata["resource_request"]["max_cores"] == 8
    assert metadata["resource_request"]["max_memory_gb"] == 32
    assert metadata["resource_actual"]["max_cores"] == 8
    assert metadata["resource_actual"]["max_memory_gb"] == 32
    source_text = (reaction_dir / "rxn.inp").read_text(encoding="utf-8")
    assert "%pal" not in source_text
    assert "%maxcore" not in source_text
    private_text = Path(metadata["selected_inp"]).read_text(encoding="utf-8")
    assert "%pal" in private_text
    assert "nprocs 8" in private_text
    assert "%maxcore 4096" in private_text
    tracking_records = json.loads((root / "job_locations.json").read_text(encoding="utf-8"))
    assert len(tracking_records) == 1
    assert tracking_records[0]["job_id"] == entry.task_id
    assert tracking_records[0]["status"] == "queued"
    assert tracking_records[0]["original_run_dir"] == str(reaction_dir.resolve())
    assert tracking_records[0]["selected_input_xyz"] == str((reaction_dir / "rxn.inp").resolve())
    assert result.worker_info.status == "inactive"
    assert result.worker_info.pid is None
    assert result.worker_info.log_file == metadata["worker_log"]
    assert len(worker_seams.pid_reads) == 1
    assert len(worker_seams.notifications) == 1


def test_submit_reaction_dir_to_queue_reports_running_worker_pid(
    tmp_path: Path,
    reaction_dir: Path,
    config: Path,
    worker_seams: _WorkerSeams,
) -> None:
    worker_seams.worker_pid = 4321

    submission = submit_reaction_dir_to_queue(_make_args(config, reaction_dir))

    assert submission.status == "submitted"
    result = submission.queued_result
    assert result is not None
    [entry] = list_queue(tmp_path)
    metadata = queue_entry_metadata(entry)
    assert result.worker_info.status == "running"
    assert result.worker_info.pid == 4321
    assert result.worker_info.log_file == metadata["worker_log"]
    assert len(worker_seams.pid_reads) == 1
    assert len(worker_seams.notifications) == 1


def test_submit_reaction_dir_to_queue_separates_inp_and_xyzfile_artifacts(
    tmp_path: Path,
    reaction_dir: Path,
    config: Path,
    worker_seams: _WorkerSeams,
) -> None:
    _write_inp(reaction_dir, "! Opt\n* xyzfile 0 1 geom.xyz\n")
    (reaction_dir / "geom.xyz").write_text("2\ncomment\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")

    submission = submit_reaction_dir_to_queue(_make_args(config, reaction_dir))

    assert submission.status == "submitted"
    entry = list_queue(tmp_path)[0]
    metadata = queue_entry_metadata(entry)
    xyz_path = str((reaction_dir / "geom.xyz").resolve())
    assert metadata["source_selected_inp"] == str(reaction_dir / "rxn.inp")
    execution_dir = Path(metadata["execution_snapshot"]["execution_dir"])
    assert execution_dir.parent == reaction_dir.resolve()
    assert Path(metadata["selected_inp"]) == execution_dir / "rxn.inp"
    assert (execution_dir / "geom.xyz").read_bytes() == (reaction_dir / "geom.xyz").read_bytes()
    assert metadata["selected_input_xyz"] == xyz_path
    assert metadata["selected_input_path"] == xyz_path
    assert metadata["job_type"] == "opt"

    tracking_records = json.loads((tmp_path / "job_locations.json").read_text(encoding="utf-8"))
    assert tracking_records[0]["selected_input_xyz"] == xyz_path


def test_submit_reaction_dir_to_queue_succeeds_when_tracking_side_effect_fails(
    tmp_path: Path,
    reaction_dir: Path,
    config: Path,
    worker_seams: _WorkerSeams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upserts: list[Any] = []

    def failing_upsert(*args: Any, **kwargs: Any) -> None:
        upserts.append((args, kwargs))
        raise RuntimeError("index write failed")

    monkeypatch.setattr(submission_mod, "upsert_queued_job_record", failing_upsert)

    submission = submit_reaction_dir_to_queue(_make_args(config, reaction_dir, priority=3))

    entries = list_queue(tmp_path)

    assert submission.status == "submitted"
    assert len(entries) == 1
    assert not (tmp_path / "job_locations.json").exists()
    result = submission.queued_result
    assert result is not None
    assert "queue submission succeeded" in (result.worker_info.detail or "")
    assert len(upserts) == 1
    assert len(worker_seams.pid_reads) == 1
    assert len(worker_seams.notifications) == 1


def test_submit_reaction_dir_to_queue_reads_metadata_from_input_even_when_flags_are_present(
    tmp_path: Path,
    reaction_dir: Path,
    config: Path,
    worker_seams: _WorkerSeams,
) -> None:
    _write_inp(
        reaction_dir,
        "! Opt\n%pal\n  nprocs 12\nend\n%maxcore 2048\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
    )

    submission = submit_reaction_dir_to_queue(
        _make_args(config, reaction_dir, max_cores=20, max_memory_gb=80)
    )

    entries = list_queue(tmp_path)

    assert submission.status == "submitted"
    assert len(entries) == 1
    metadata = queue_entry_metadata(entries[0])
    assert metadata["resource_request"]["max_cores"] == 12
    assert metadata["resource_request"]["max_memory_gb"] == 24
    assert metadata["resource_actual"]["max_cores"] == 12
    assert metadata["resource_actual"]["max_memory_gb"] == 24
    inp_text = (reaction_dir / "rxn.inp").read_text(encoding="utf-8")
    assert "nprocs 12" in inp_text
    assert "%maxcore 2048" in inp_text
    assert len(worker_seams.pid_reads) == 1
    assert len(worker_seams.notifications) == 1
