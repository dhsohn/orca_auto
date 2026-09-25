import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

from orca_auto.core.queue.engine.input_snapshot import bind_direct_generation_owner
from orca_auto.orca import run_lock
from orca_auto.orca import state as state_module
from orca_auto.orca import state_reading as state_reading_module
from orca_auto.orca.engine_runner import executable_identity
from orca_auto.orca.report import publication as publication_module
from orca_auto.orca.report.publication import write_report_files, write_report_json
from orca_auto.orca.run_lock import acquire_run_lock
from orca_auto.orca.state import (
    atomic_write_text,
    new_state,
    save_state,
    write_state,
)
from orca_auto.orca.state_reading import load_report_json, load_state
from orca_auto.orca.statuses import TERMINAL_RUN_STATUSES, RunStatus
from orca_auto.orca.types import RunFinalResult, RunState
from tests.machine_contract_helpers import validate_common_machine


def _bind_generation(reaction: Path, *, token: str) -> tuple[Path, dict]:
    """Create a verified execution generation and the matching state fields."""
    generation = reaction / "20260714-224054-959479f2"
    generation.mkdir()
    inp = generation / "nebts.inp"
    inp.write_text("! NEB-TS\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    generation_status = generation.stat()
    reaction_status = reaction.stat()
    bind_direct_generation_owner(
        reaction,
        namespace=generation.name,
        expected_job_identity=(reaction_status.st_dev, reaction_status.st_ino),
        expected_generation_identity=(generation_status.st_dev, generation_status.st_ino),
        owner_token=token,
    )
    provenance = {
        "execution_dir": str(generation),
        "execution_dir_identity": {
            "device": generation_status.st_dev,
            "inode": generation_status.st_ino,
        },
        "generation_owner_token": token,
        "bound_selected_identity": executable_identity(inp),
    }
    return generation, provenance


def _bound_state(reaction: Path, *, token: str) -> tuple[Path, RunState]:
    """A fresh state whose ``execution_provenance`` points at a verified generation."""
    generation, provenance = _bind_generation(reaction, token=token)
    state = new_state(reaction, generation / "nebts.inp")
    state["execution_provenance"] = provenance
    return generation, state


_COMPLETED_RESULT: RunFinalResult = {
    "status": "completed",
    "analyzer_status": "completed",
    "reason": "normal_termination",
    "completed_at": "2026-01-01T00:00:00+00:00",
}


def test_generation_state_read_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = tmp_path / "20260714-224054-959479f2"
    generation.mkdir()
    (generation / "job_state.json").write_text("{}" * 8, encoding="utf-8")

    monkeypatch.setattr(state_reading_module, "MAX_RUN_ARTIFACT_JSON_BYTES", 8)
    assert state_reading_module.load_generation_state(generation) is None


def test_generation_report_read_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation, state = _bound_state(tmp_path, token="bounded-report-token-0001")
    report_path = write_report_json(tmp_path, dict(state))
    assert report_path is not None

    monkeypatch.setattr(
        state_reading_module, "MAX_RUN_ARTIFACT_JSON_BYTES", report_path.stat().st_size - 1
    )
    assert load_report_json(generation) is None


def test_generation_report_rejects_swap_after_confined_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation, state = _bound_state(tmp_path, token="swapped-report-token-0001")
    report_path = write_report_json(tmp_path, dict(state))
    assert report_path is not None
    original_target = state_reading_module.verified_generation_artifact_target

    def replace_after_read(
        reaction_dir: Path,
        payload: dict,
    ) -> tuple[Path, tuple[int, int]] | None:
        target = original_target(reaction_dir, payload)
        report_path.write_text(
            report_path.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        return target

    monkeypatch.setattr(
        state_reading_module, "verified_generation_artifact_target", replace_after_read
    )
    assert load_report_json(generation) is None


def test_recover_stale_lock_with_dead_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    lock_path.write_text(
        json.dumps({"pid": 2147483647, "started_at": "2026-01-01T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    with acquire_run_lock(tmp_path):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload.get("pid") == os.getpid()
        assert isinstance(payload.get("started_at"), str)
    assert lock_path.exists()


def test_active_lock_blocks_second_runner(tmp_path: Path) -> None:
    with acquire_run_lock(tmp_path), pytest.raises(RuntimeError), acquire_run_lock(tmp_path):
        pass


def test_unlocked_stale_metadata_is_reused_without_pid_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "run.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 12345,
                "started_at": "2026-01-01T00:00:00+00:00",
                "process_start_ticks": 111,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        run_lock,
        "current_process_lock_payload",
        lambda: {
            "pid": os.getpid(),
            "started_at": "2026-03-22T00:00:00+00:00",
            "process_start_ticks": 333,
        },
    )

    with acquire_run_lock(tmp_path):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload.get("pid") == os.getpid()
        assert payload.get("process_start_ticks") == 333


def test_state_and_reports_are_written_without_tmp_leaks(tmp_path: Path) -> None:
    generation, state = _bound_state(tmp_path, token="state-leak-token-0001")
    assert re.match(r"^run_\d{8}_\d{6}_[0-9a-f]{32}$", str(state["run_id"]))

    save_state(tmp_path, state)
    loaded = load_state(tmp_path)
    assert isinstance(loaded, dict)

    write_report_files(tmp_path, state)
    assert state_reading_module.report_json_path(generation).exists()
    assert not (tmp_path / "job_report.json").exists()

    assert list(tmp_path.glob("*.tmp.*")) == []
    assert list(generation.glob("*.tmp.*")) == []


def test_queue_identity_survives_state_round_trip(tmp_path: Path) -> None:
    inp = tmp_path / "rxn.inp"
    inp.write_text("! Opt\n", encoding="utf-8")
    state = new_state(tmp_path, inp)
    state["queue_id"] = "q-orca-round-trip"
    state["queue_generation"] = "a" * 64

    save_state(tmp_path, state)
    loaded = load_state(tmp_path)
    assert loaded is not None
    assert loaded["queue_id"] == "q-orca-round-trip"
    assert loaded["queue_generation"] == "a" * 64

    save_state(tmp_path, loaded)
    raw = json.loads((tmp_path / "job_state.json").read_text(encoding="utf-8"))
    assert raw["job"]["queue_id"] == "q-orca-round-trip"
    assert raw["job"]["generation"] == "a" * 64


def test_direct_state_keeps_queue_identity_empty(tmp_path: Path) -> None:
    inp = tmp_path / "rxn.inp"
    inp.write_text("! Opt\n", encoding="utf-8")
    save_state(tmp_path, new_state(tmp_path, inp))

    raw = json.loads((tmp_path / "job_state.json").read_text(encoding="utf-8"))
    assert raw["job"]["queue_id"] == ""
    assert raw["job"]["generation"] == ""


def test_write_report_files_skips_publication_without_verified_generation(
    tmp_path: Path,
) -> None:
    inp = tmp_path / "rxn.inp"
    inp.write_text("! Opt\n", encoding="utf-8")
    state = new_state(tmp_path, inp)

    reports = write_report_files(tmp_path, state)

    assert reports == {}
    assert not (tmp_path / "job_report.json").exists()


def test_write_state_fails_closed_when_pinned_reaction_directory_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reaction = tmp_path / "reaction"
    displaced = tmp_path / "reaction-displaced"
    reaction.mkdir()
    inp = reaction / "rxn.inp"
    inp.write_text("! Opt\n", encoding="utf-8")
    state = new_state(reaction, inp)
    original_identity = (reaction.stat().st_dev, reaction.stat().st_ino)

    @contextmanager
    def _replace_after_pin(
        directory_fd: int,
        lock_name: str,
        *,
        display_path: Path | None = None,
        timeout_seconds: float = 10.0,
    ) -> Iterator[None]:
        del lock_name, display_path, timeout_seconds
        pinned = os.fstat(directory_fd)
        assert (pinned.st_dev, pinned.st_ino) == original_identity
        reaction.rename(displaced)
        reaction.mkdir()
        yield

    monkeypatch.setattr(state_module, "file_lock_at", _replace_after_pin)
    with pytest.raises(ValueError, match="parent directory identity changed"):
        write_state(reaction, state)

    assert not (reaction / "job_state.json").exists()
    assert not (displaced / "job_state.json").exists()
    assert list(reaction.glob(".job_state.json.*.tmp")) == []


def test_execution_state_is_recorded_in_root_and_visible_generation(tmp_path: Path) -> None:
    generation, state = _bound_state(tmp_path, token="state-mirror-owner-token-0001")

    save_state(tmp_path, state)
    write_report_files(tmp_path, state)

    # Execution facts match before root-only notification bookkeeping;
    # reports live only inside the generation.
    assert load_state(generation) == load_state(tmp_path)
    assert load_report_json(generation) is not None
    assert load_report_json(tmp_path) is None
    assert not (tmp_path / ".orca_auto_orca_executions").exists()
    assert not (tmp_path / ".orca_auto_input_snapshots").exists()


def test_generation_report_leaves_root_copies_untouched(tmp_path: Path) -> None:
    generation, state = _bound_state(tmp_path, token="state-mirror-owner-token-0002")
    state_reading_module.report_json_path(tmp_path).write_text("{}", encoding="utf-8")

    write_report_files(tmp_path, state)

    # Unbound root files are left untouched; the writer publishes only
    # into the verified generation.
    assert state_reading_module.report_json_path(tmp_path).exists()
    assert state_reading_module.report_json_path(generation).is_file()


def test_replaced_visible_generation_never_receives_state_or_report(tmp_path: Path) -> None:
    generation = tmp_path / "20260714-224054-959479f2"
    generation.mkdir()
    inp = generation / "nebts.inp"
    inp.write_text("! NEB-TS\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    generation_status = generation.stat()
    state = new_state(tmp_path, inp)
    state["execution_provenance"] = {
        "execution_dir": str(generation),
        "execution_dir_identity": {
            "device": generation_status.st_dev,
            "inode": generation_status.st_ino,
        },
    }
    moved = tmp_path / "moved-original-generation"
    generation.rename(moved)
    generation.mkdir()
    (generation / "sentinel").write_text("replacement", encoding="utf-8")

    save_state(tmp_path, state)
    write_report_files(tmp_path, state)

    assert (tmp_path / "job_state.json").is_file()
    assert not (tmp_path / "job_report.json").exists()
    assert {path.name for path in generation.iterdir()} == {"sentinel"}
    assert not (moved / "job_state.json").exists()
    assert not (moved / "job_report.json").exists()


def test_atomic_write_text_remains_available(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"

    atomic_write_text(target, "hello")

    assert target.read_text(encoding="utf-8") == "hello"
    assert list(tmp_path.glob("*.tmp.*")) == []


def test_state_module_keeps_write_helpers_available(tmp_path: Path) -> None:
    generation, state = _bound_state(tmp_path, token="state-helper-token-0001")
    inp = generation / "nebts.inp"

    saved_path = write_state(tmp_path, state)
    assert saved_path == state_reading_module.state_path(tmp_path)
    assert state_reading_module.load_state(tmp_path) is not None

    report_payload = {
        "run_id": state["run_id"],
        "reaction_dir": str(tmp_path),
        "selected_inp": str(inp),
        "status": "created",
        "started_at": state["started_at"],
        "updated_at": state["updated_at"],
        "attempt_count": 0,
        "attempts": [],
        "execution_provenance": state["execution_provenance"],
        "final_result": None,
    }
    assert write_report_json(tmp_path, report_payload) == state_reading_module.report_json_path(
        generation
    )
    written_report = load_report_json(generation)
    assert written_report is not None
    assert written_report["engine"] == "orca"
    assert written_report["engine_payload"]["run_id"] == state["run_id"]
    assert written_report["status"]["state"] == "created"


def test_state_module_does_not_forward_read_owner() -> None:
    for name in state_reading_module.__all__:
        assert not hasattr(state_module, name), name


def test_write_report_files_json_fields(tmp_path: Path) -> None:
    generation, state = _bound_state(tmp_path, token="state-fields-token-0001")
    inp = generation / "nebts.inp"
    state["status"] = "completed"
    state["attempts"] = [
        {
            "index": 1,
            "inp_path": str(inp),
            "out_path": str(tmp_path / "rxn.out"),
            "return_code": 0,
            "analyzer_status": "completed",
            "command": ["/opt/orca/orca", "rxn.inp"],
            "input_identity": {
                "path": str(inp),
                "sha256": "a" * 64,
                "size_bytes": 6,
            },
            "executable_identity": {
                "path": "/opt/orca/orca",
                "sha256": "b" * 64,
                "size_bytes": 1024,
            },
        }
    ]
    state["execution_provenance"] = {
        **state["execution_provenance"],
        "materialized_inputs": {},
        "executable_identity": state["attempts"][0]["executable_identity"],
    }
    state["final_result"] = {**_COMPLETED_RESULT, "last_out_path": str(tmp_path / "rxn.out")}
    write_state(tmp_path, state)
    result = write_report_files(tmp_path, state)
    report_json_path = Path(result["report_json"])

    observation = json.loads(report_json_path.read_text(encoding="utf-8"))
    validate_common_machine(report_json_path)
    assert observation["contract"] == {"name": "factory/machine-observation", "version": 1}
    assert observation["operation"]["kind"] == "chemistry/orca-run"
    assert observation["lifecycle"]["outcome"] == "succeeded"
    assert observation["handoff"]["status"] == "blocked"
    assert observation["delivery"]["status"] == "incomplete"
    report = load_report_json(generation)
    assert report is not None
    assert load_report_json(generation, require_consumable_success=True) is None
    assert report["status"]["state"] == "completed"
    assert "max_retries" not in report["engine_payload"]
    assert len(report["engine_payload"]["attempts"]) == 1
    assert report["engine_payload"]["attempts"] == state["attempts"]
    assert report["engine_payload"]["execution_provenance"] == state["execution_provenance"]
    assert report["engine_payload"]["final_result"] is not None


def test_write_report_files_reentry_preserves_published_terminal_generation(
    tmp_path: Path,
) -> None:
    _generation, state = _bound_state(tmp_path, token="reentry-terminal-tok-01")
    state["status"] = "completed"
    state["final_result"] = {**_COMPLETED_RESULT, "last_out_path": str(tmp_path / "rxn.out")}
    write_state(tmp_path, state)
    first = write_report_files(tmp_path, state)
    report_json_path = Path(first["report_json"])
    published = {
        path.name: path.read_bytes() for path in report_json_path.parent.iterdir() if path.is_file()
    }

    changed = dict(state)
    changed["updated_at"] = "2026-02-02T00:00:00+00:00"
    second = write_report_files(tmp_path, changed)

    assert second["report_json"] == first["report_json"]
    for name, data in published.items():
        assert (report_json_path.parent / name).read_bytes() == data, (
            f"re-entry modified published artifact {name}"
        )


@pytest.mark.parametrize(
    ("link_kind", "target_exists"),
    [("live-symlink", True), ("dangling-symlink", False), ("hardlink", True)],
    ids=["live-symlink", "dangling-symlink", "hardlink"],
)
def test_write_report_files_rejects_unsafe_machine_links_before_artifact_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link_kind: str, target_exists: bool
) -> None:
    generation, state = _bound_state(tmp_path, token=f"unsafe-machine-link-token-{link_kind}")
    state["status"] = "completed"

    foreign_machine = tmp_path / "foreign-machine.json"
    if target_exists:
        foreign_machine.write_text("{}", encoding="utf-8")
    machine_path = state_reading_module.report_json_path(generation)
    if link_kind == "hardlink":
        os.link(foreign_machine, machine_path)
    else:
        machine_path.symlink_to(foreign_machine)
    html_path = generation / "job_report.html"
    si_path = generation / "si_block.md"
    html_bytes = b"published html\n"
    si_bytes = b"published si\n"
    html_path.write_bytes(html_bytes)
    si_path.write_bytes(si_bytes)

    def html_writer_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("HTML writer must not run")

    def si_writer_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("SI writer must not run")

    monkeypatch.setattr(publication_module, "write_job_html_report", html_writer_must_not_run)
    monkeypatch.setattr(publication_module, "write_si_block", si_writer_must_not_run)
    with pytest.raises(RuntimeError, match="existing machine observation is invalid"):
        write_report_files(tmp_path, state)

    assert html_path.read_bytes() == html_bytes
    assert si_path.read_bytes() == si_bytes
    if link_kind == "hardlink":
        assert not machine_path.is_symlink()
        assert machine_path.stat().st_nlink == 2
    else:
        assert machine_path.is_symlink()
        assert os.readlink(machine_path) == str(foreign_machine)
    assert foreign_machine.exists() == target_exists
    if target_exists:
        assert foreign_machine.read_bytes() == b"{}"


def test_terminal_machine_observation_is_immutable(tmp_path: Path) -> None:
    _generation, state = _bound_state(tmp_path, token="immutable-machine-token-0001")
    state["status"] = "failed"
    final_result: RunFinalResult = {
        "status": "failed",
        "analyzer_status": "incomplete",
        "reason": "runner_exception",
    }
    state["final_result"] = final_result
    write_state(tmp_path, state)

    path = write_report_json(tmp_path, dict(state))
    assert path is not None
    original_identity = (path.stat().st_dev, path.stat().st_ino)

    assert write_report_json(tmp_path, dict(state)) == path
    assert (path.stat().st_dev, path.stat().st_ino) == original_identity

    changed = dict(state)
    changed["final_result"] = {**final_result, "reason": "cancel_requested"}
    with pytest.raises(RuntimeError, match="terminal machine observation is immutable"):
        write_report_json(tmp_path, changed)


def test_load_report_json_returns_none_for_missing_invalid_and_non_dict(tmp_path: Path) -> None:
    assert load_report_json(tmp_path) is None

    report_path = state_reading_module.report_json_path(tmp_path)
    report_path.write_text("not valid json!!!", encoding="utf-8")
    assert load_report_json(tmp_path) is None

    report_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert load_report_json(tmp_path) is None


def test_load_state_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert load_state(tmp_path) is None


def test_load_state_returns_none_for_invalid_json(tmp_path: Path) -> None:
    state_reading_module.state_path(tmp_path).write_text("not valid json!!!", encoding="utf-8")
    assert load_state(tmp_path) is None


def test_lock_released_after_context_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / run_lock.RUN_LOCK_FILE_NAME
    with acquire_run_lock(tmp_path):
        assert lock_path.exists()
    with acquire_run_lock(tmp_path):
        assert lock_path.exists()


def test_unlocked_invalid_lock_payload_does_not_block(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    lock_path.write_text(json.dumps({"pid": "invalid", "started_at": "x"}) + "\n", encoding="utf-8")
    with acquire_run_lock(tmp_path):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()


# -- finalize_state contract ----------------------------------------------


def _contract_final_result(status: str) -> RunFinalResult:
    return {
        "status": status,
        "analyzer_status": "incomplete",
        "reason": "test",
        "last_out_path": None,
    }


@pytest.mark.parametrize(
    "status",
    [RunStatus.RUNNING, RunStatus.CREATED, "running", "bogus", ""],
    ids=["RUNNING", "CREATED", "running", "bogus", "empty"],
)
def test_finalize_state_rejects_non_terminal_status(
    tmp_path: Path, status: RunStatus | str
) -> None:
    state = new_state(tmp_path, tmp_path / "job.inp")
    with pytest.raises(ValueError):
        state_module.finalize_state(
            tmp_path,
            state,
            status=status,
            final_result=_contract_final_result("failed"),
        )
    assert state["status"] == RunStatus.CREATED.value
    assert state.get("final_result") is None
    assert not state_reading_module.state_path(tmp_path).exists()


def test_terminal_run_statuses_are_completed_failed_and_cancelled() -> None:
    assert TERMINAL_RUN_STATUSES == {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED])
@pytest.mark.parametrize("as_value", [False, True], ids=["member", "value"])
def test_finalize_state_accepts_each_terminal_status_member_or_value(
    tmp_path: Path, status: RunStatus, as_value: bool
) -> None:
    state = new_state(tmp_path, tmp_path / "job.inp")
    state_module.finalize_state(
        tmp_path,
        state,
        status=status.value if as_value else status,
        final_result=_contract_final_result(status.value),
    )
    assert state["status"] == status.value
    assert isinstance(state["status"], str)
    assert not isinstance(state["status"], RunStatus)


@pytest.mark.parametrize(
    "marker", ["finished_notification_claimed_at", "finished_notification_sent_at"]
)
def test_generation_state_records_execution_without_notification_bookkeeping(
    tmp_path: Path, marker: str
) -> None:
    generation, state = _bound_state(tmp_path, token="execution-state-owner-0001")
    state["status"] = "completed"
    final = {**_COMPLETED_RESULT, marker: "2026-09-25T10:00:00Z"}
    state["final_result"] = cast(RunFinalResult, final)
    save_state(tmp_path, state)

    current = load_state(tmp_path)
    execution = load_state(generation)
    assert current is not None and current["final_result"] is not None
    assert execution is not None and execution["final_result"] is not None
    assert current["final_result"].get(marker) == "2026-09-25T10:00:00Z"
    assert marker not in execution["final_result"]
    assert execution["final_result"] == _COMPLETED_RESULT
    assert final[marker] == "2026-09-25T10:00:00Z"


def test_root_notification_and_replayed_finalization_leave_execution_state_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation, state = _bound_state(tmp_path, token="execution-state-owner-0002")
    monkeypatch.setattr(state_module, "now_utc_iso", lambda: "2026-09-25T10:00:00Z")
    state_module.finalize_state(
        tmp_path, state, status="completed", final_result=_COMPLETED_RESULT.copy()
    )
    generation_path = generation / "job_state.json"
    before = generation_path.read_bytes()
    before_stat = generation_path.stat()
    monkeypatch.setattr(state_module, "now_utc_iso", lambda: "2026-09-25T11:00:00Z")
    assert state["final_result"] is not None
    state["final_result"]["finished_notification_claimed_at"] = "2026-09-25T11:00:00Z"
    save_state(tmp_path, state)
    state_module.finalize_state(
        tmp_path, state, status="completed", final_result=state["final_result"]
    )

    assert generation_path.read_bytes() == before
    assert generation_path.stat().st_mtime_ns == before_stat.st_mtime_ns
    current = load_state(tmp_path)
    execution = load_state(generation)
    assert current is not None and execution is not None
    assert current["updated_at"] == "2026-09-25T11:00:00Z"
    assert execution["updated_at"] == "2026-09-25T10:00:00Z"
    assert current["final_result"] is not None
    assert current["final_result"]["finished_notification_claimed_at"]


def test_execution_changes_still_update_generation_before_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation, state = _bound_state(tmp_path, token="execution-state-owner-0003")
    save_state(tmp_path, state)
    writes: list[Path] = []
    original = state_module.atomic_write_confined_bytes

    def observe(root: Path, path: Path, *args, **kwargs):
        original(root, path, *args, **kwargs)
        writes.append(path)

    monkeypatch.setattr(state_module, "atomic_write_confined_bytes", observe)
    state["status"] = "running"
    save_state(tmp_path, state)
    running = load_state(generation)
    assert running is not None and running["status"] == "running"
    state["attempts"].append(
        {"index": 1, "inp_path": state["selected_inp"], "analyzer_status": "completed"}
    )
    save_state(tmp_path, state)
    analyzed = load_state(generation)
    assert analyzed is not None and analyzed["status"] == "running"
    assert len(analyzed["attempts"]) == 1
    assert analyzed["attempts"][0]["analyzer_status"] == "completed"
    state_module.finalize_state(
        tmp_path, state, status="completed", final_result=_COMPLETED_RESULT.copy()
    )
    completed = load_state(generation)
    assert completed is not None and completed["status"] == "completed"
    assert completed["final_result"] == _COMPLETED_RESULT
    assert writes == [generation / "job_state.json", tmp_path / "job_state.json"] * 3


@pytest.mark.parametrize("fail_at", ["generation", "root"])
def test_state_write_failure_preserves_execution_evidence_and_retry_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    generation, state = _bound_state(tmp_path, token="execution-state-owner-0004")
    save_state(tmp_path, state)
    root_path = tmp_path / "job_state.json"
    generation_path = generation / "job_state.json"
    root_before = root_path.read_bytes()
    generation_before = generation_path.read_bytes()
    original = state_module.atomic_write_confined_bytes
    failed = False

    def fail_once(root: Path, path: Path, *args, **kwargs):
        nonlocal failed
        target = generation_path if fail_at == "generation" else root_path
        if path == target and not failed:
            failed = True
            raise OSError("injected state write failure")
        return original(root, path, *args, **kwargs)

    monkeypatch.setattr(state_module, "atomic_write_confined_bytes", fail_once)
    state["status"] = "running"
    with pytest.raises(OSError, match="injected state write failure"):
        save_state(tmp_path, state)
    assert root_path.read_bytes() == root_before
    if fail_at == "generation":
        assert generation_path.read_bytes() == generation_before
    else:
        execution = load_state(generation)
        assert execution is not None and execution["status"] == "running"
    after_failure = generation_path.read_bytes()
    after_failure_stat = generation_path.stat()
    save_state(tmp_path, state)
    current = load_state(tmp_path)
    execution = load_state(generation)
    assert current is not None and current["status"] == "running"
    assert execution is not None and execution["status"] == "running"
    if fail_at == "root":
        assert generation_path.read_bytes() == after_failure
        assert generation_path.stat().st_mtime_ns == after_failure_stat.st_mtime_ns


def test_unreadable_generation_refuses_both_state_writes(tmp_path: Path) -> None:
    generation, state = _bound_state(tmp_path, token="execution-state-owner-0005")
    save_state(tmp_path, state)
    root_path = tmp_path / "job_state.json"
    before = root_path.read_bytes()
    generation_path = generation / "job_state.json"
    generation_path.write_text("broken generation state")
    state["status"] = "running"
    with pytest.raises(ValueError, match="unreadable ORCA generation state"):
        save_state(tmp_path, state)
    assert root_path.read_bytes() == before
    assert generation_path.read_text() == "broken generation state"


def test_historical_generation_notification_fields_are_not_rewritten(tmp_path: Path) -> None:
    generation, state = _bound_state(tmp_path, token="execution-state-owner-0006")
    state_module.finalize_state(
        tmp_path, state, status="completed", final_result=_COMPLETED_RESULT.copy()
    )
    generation_path = generation / "job_state.json"
    payload = json.loads(generation_path.read_text())
    payload["engine_payload"]["final_result"]["finished_notification_sent_at"] = "historical"
    generation_path.write_text(json.dumps(payload))
    before = generation_path.read_bytes()
    assert state["final_result"] is not None
    state["final_result"]["finished_notification_claimed_at"] = "2026-09-25T10:00:00Z"
    save_state(tmp_path, state)
    assert generation_path.read_bytes() == before
    execution = load_state(generation)
    assert execution is not None and execution["final_result"] is not None
    assert execution["final_result"]["finished_notification_sent_at"] == "historical"


def test_common_machine_validation_rejects_changed_input_receipt(tmp_path: Path) -> None:
    generation, state = _bound_state(tmp_path, token="receipt-validation-token-01")
    write_state(tmp_path, state)
    reports = write_report_files(tmp_path, state)
    machine = Path(reports["report_json"])
    validate_common_machine(machine)

    inp = generation / "nebts.inp"
    original = inp.read_bytes()
    replacement = original.replace(b"NEB-TS", b"OptTS ", 1)
    assert replacement != original and len(replacement) == len(original)
    inp.write_bytes(replacement)

    with pytest.raises(pytest.fail.Exception, match="artifact sha256 mismatch"):
        validate_common_machine(machine)
