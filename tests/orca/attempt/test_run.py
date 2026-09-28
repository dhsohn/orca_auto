from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from orca_auto.core.engine_scratch import (
    ScratchPublication,
    attach_scratch_provenance_to_exception,
)
from orca_auto.orca.attempt import run as attempt_run
from orca_auto.orca.orca_runner import RunResult, WorkerShutdownInterrupt
from orca_auto.orca.state import new_state
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.types import RunStartedNotification, RunState
from tests.conftest import RecordingChannel

_OPT_INPUT = "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
_NORMAL_TERMINATION = (
    "FINAL SINGLE POINT ENERGY -1.1\nTHE OPTIMIZATION HAS CONVERGED\n"
    "****ORCA TERMINATED NORMALLY****\nTOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds 0 msec\n"
)

RunFn = Callable[[Path], RunResult]
Attempt = Callable[..., int]


def _write_input(reaction_dir: Path, *, name: str = "rxn.inp", text: str = _OPT_INPUT) -> Path:
    selected_inp = reaction_dir / name
    selected_inp.write_text(text, encoding="utf-8")
    return selected_inp


def _saved_state(reaction_dir: Path) -> RunState:
    saved = load_state(reaction_dir)
    assert saved is not None
    return saved


def _output_run(text: str, *, return_code: int, seen: list[Path] | None = None) -> RunFn:
    def run(inp_path: Path) -> RunResult:
        if seen is not None:
            seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        out_path.write_text(text, encoding="utf-8")
        return RunResult(out_path=str(out_path), return_code=return_code)

    return run


def _capture_success_run(seen: list[Path]) -> RunFn:
    def run(inp_path: Path) -> RunResult:
        seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        out_path.write_text(_NORMAL_TERMINATION, encoding="utf-8")
        return RunResult(
            out_path=str(out_path),
            return_code=0,
            command=("/opt/orca/orca", inp_path.name),
            input_identity={
                "path": str(inp_path),
                "sha256": "a" * 64,
                "size_bytes": inp_path.stat().st_size,
            },
            executable_identity={
                "path": "/opt/orca/orca",
                "sha256": "b" * 64,
                "size_bytes": 1024,
            },
        )

    return run


def _raising_run(exc: BaseException) -> RunFn:
    def run(inp_path: Path) -> RunResult:
        inp_path.with_suffix(".out").write_text("partial output\n", encoding="utf-8")
        raise exc

    return run


def test_runner_exception_preserves_existing_output_path(
    tmp_path: Path, attempt: Attempt, capsys: pytest.CaptureFixture[str]
) -> None:
    selected_inp = _write_input(tmp_path, text="! SP\n")

    rc = attempt(selected_inp, _raising_run(RuntimeError("injected runner failure")))

    saved = _saved_state(tmp_path)
    final_result = saved["final_result"]
    assert final_result is not None
    assert rc == 1
    assert final_result["reason"] == "runner_exception"
    assert final_result["runner_error"] == "injected runner failure"
    assert saved["attempts"] == []
    assert saved.get("scratch_publications", []) == []
    assert final_result["last_out_path"] == str(selected_inp.with_suffix(".out"))
    assert capsys.readouterr().out.count("status: failed\n") == 1


def test_runner_exception_does_not_report_symlink_output(tmp_path: Path, attempt: Attempt) -> None:
    selected_inp = _write_input(tmp_path, text="! SP\n")
    outside_out = tmp_path / "outside.out"
    outside_out.write_text("outside\n", encoding="utf-8")
    selected_inp.with_suffix(".out").symlink_to(outside_out)

    def run(_inp_path: Path) -> RunResult:
        raise RuntimeError("injected runner failure")

    rc = attempt(selected_inp, run)

    final_result = _saved_state(tmp_path)["final_result"]
    assert final_result is not None
    assert rc == 1
    assert final_result["last_out_path"] is None


def test_worker_shutdown_propagates_without_failed_final_result(
    tmp_path: Path, attempt: Attempt, capsys: pytest.CaptureFixture[str]
) -> None:
    selected_inp = _write_input(tmp_path)

    def run(_inp_path: Path) -> RunResult:
        raise WorkerShutdownInterrupt

    with pytest.raises(WorkerShutdownInterrupt):
        attempt(selected_inp, run)

    saved = _saved_state(tmp_path)
    assert saved["final_result"] is None
    assert saved["status"] == "running"
    assert capsys.readouterr().out == ""


def test_worker_shutdown_persists_committed_scratch_publication(
    tmp_path: Path, attempt: Attempt
) -> None:
    selected_inp = _write_input(tmp_path, text="! SP\n")

    def run(inp_path: Path) -> RunResult:
        exc = WorkerShutdownInterrupt()
        attach_scratch_provenance_to_exception(
            exc,
            ScratchPublication(
                paths=(inp_path.with_suffix(".gbw"), inp_path.with_suffix(".out")),
                omitted_transient_files=(f"{inp_path.stem}.EIJ.tmp",),
                omitted_transient_bytes=128,
            ),
        )
        raise exc

    with pytest.raises(WorkerShutdownInterrupt):
        attempt(selected_inp, run)

    saved = _saved_state(tmp_path)
    assert saved["attempts"] == []
    assert saved["final_result"] is None
    publication = saved["scratch_publications"][0]
    assert publication["attempt_index"] == 1
    assert publication["outcome"] == "worker_shutdown"
    assert publication["publication"]["published_files"] == ["rxn.gbw", "rxn.out"]


def test_analyzer_exception_persists_committed_scratch_publication(
    tmp_path: Path, attempt: Attempt, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_inp = _write_input(tmp_path, text="! SP\n")

    def failing_analyzer(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected analyzer failure")

    def run(inp_path: Path) -> RunResult:
        out_path = inp_path.with_suffix(".out")
        out_path.write_text("published output\n", encoding="utf-8")
        return RunResult(
            out_path=str(out_path),
            return_code=0,
            scratch_provenance={
                "used": True,
                "filesystem": "tmpfs",
                "publication_status": "committed",
                "published_files": [inp_path.with_suffix(".gbw").name, out_path.name],
                "omitted_transient_files": [],
                "omitted_transient_bytes": 0,
            },
        )

    monkeypatch.setattr(attempt_run, "analyze_output", failing_analyzer)
    rc = attempt(selected_inp, run)

    saved = _saved_state(tmp_path)
    assert rc == 1
    assert saved["attempts"] == []
    assert saved["scratch_publications"][0]["outcome"] == "exception"
    assert saved["final_result"] is not None
    assert saved["final_result"]["last_out_path"] == str(selected_inp.with_suffix(".out"))


@pytest.mark.parametrize("with_restart_artifacts", [True, False])
def test_scf_failure_is_terminal(
    tmp_path: Path, attempt: Attempt, with_restart_artifacts: bool
) -> None:
    selected_inp = _write_input(tmp_path)
    seen: list[Path] = []

    def run(inp_path: Path) -> RunResult:
        seen.append(inp_path)
        if with_restart_artifacts:
            inp_path.with_suffix(".xyz").write_text(
                "2\ncheckpoint geometry\nH 0 0 0\nH 0 0 0.75\n",
                encoding="utf-8",
            )
        out_path = inp_path.with_suffix(".out")
        out_path.write_text("SCF NOT CONVERGED AFTER 300 CYCLES\n", encoding="utf-8")
        return RunResult(out_path=str(out_path), return_code=1)

    rc = attempt(selected_inp, run)

    saved = _saved_state(tmp_path)
    assert rc == 1
    assert seen == [selected_inp]
    assert not (tmp_path / "rxn.retry01.inp").exists()
    assert "max_retries" not in saved
    final_result = saved.get("final_result")
    assert final_result is not None
    assert final_result.get("reason") == "scf_not_converged"


def test_neb_ts_failure_is_terminal(tmp_path: Path, attempt: Attempt) -> None:
    selected_inp = _write_input(
        tmp_path,
        name="neb.inp",
        text="! NEB-TS B3LYP def2-SVP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
    )
    seen: list[Path] = []
    run = _output_run(
        "FINAL SINGLE POINT ENERGY -1.1\nTHE OPTIMIZATION HAS CONVERGED\n"
        "VIBRATIONAL FREQUENCIES\n  1    120.00 cm**-1\n  2    240.00 cm**-1\n"
        "****ORCA TERMINATED NORMALLY****\n",
        return_code=0,
        seen=seen,
    )

    rc = attempt(selected_inp, run)

    saved = _saved_state(tmp_path)
    assert rc == 1
    assert seen == [selected_inp]
    assert not (tmp_path / "neb.retry01.inp").exists()
    assert "max_retries" not in saved
    final_result = saved.get("final_result")
    assert final_result is not None
    assert final_result.get("reason") == "ts_criteria_failed"


def test_start_notification_and_persisted_terminal_result_describe_one_attempt(
    tmp_path: Path, attempt: Attempt, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_inp = _write_input(tmp_path)
    delivered = threading.Event()
    started_events: list[RunStartedNotification] = []

    def notify(_channel: object, event: RunStartedNotification) -> bool:
        started_events.append(event)
        delivered.set()
        return True

    monkeypatch.setattr(attempt_run, "notification_channel", lambda _cfg: RecordingChannel())
    monkeypatch.setattr(attempt_run, "notify_run_started_event", notify)

    rc = attempt(selected_inp, _capture_success_run([]))

    assert rc == 0
    assert delivered.wait(5)
    assert len(started_events) == 1
    started = started_events[0]
    assert started["attempt_index"] == 1
    assert started["status"] == "running"
    assert started["current_inp"] == str(selected_inp)
    assert started["resumed"] is False

    saved = _saved_state(tmp_path)
    assert saved["attempts"][0]["command"] == ["/opt/orca/orca", "rxn.inp"]
    assert saved["attempts"][0]["executable_identity"]["sha256"] == "b" * 64
    finished = saved["final_result"]
    assert finished is not None
    assert finished["status"] == "completed"
    assert finished["analyzer_status"] == "completed"
    assert finished["reason"] == "normal_termination"
    assert len(saved["attempts"]) == 1
    last_out_path = finished["last_out_path"]
    assert last_out_path is not None
    assert last_out_path.endswith("rxn.out")


def test_disabled_channel_sends_no_start_notification(
    tmp_path: Path, attempt: Attempt, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_inp = _write_input(tmp_path)
    channel = RecordingChannel(enabled=False)
    monkeypatch.setattr(attempt_run, "notification_channel", lambda _cfg: channel)

    assert attempt(selected_inp, _capture_success_run([])) == 0
    assert channel.sends == []


def test_resumed_terminal_attempt_finishes_without_running_again(
    tmp_path: Path, attempt: Attempt, capsys: pytest.CaptureFixture[str]
) -> None:
    selected_inp = _write_input(tmp_path)
    state = new_state(tmp_path, selected_inp)
    out_path = tmp_path / "rxn.out"
    out_path.write_text(_NORMAL_TERMINATION, encoding="utf-8")
    state["attempts"].append(
        {
            "index": 1,
            "inp_path": str(selected_inp),
            "out_path": str(out_path),
            "return_code": 0,
            "analyzer_status": "completed",
            "analyzer_reason": "normal_termination",
            "markers": {},
            "patch_actions": [],
            "started_at": "2026-03-22T00:00:00+00:00",
            "ended_at": "2026-03-22T00:00:01+00:00",
        }
    )

    def run(_inp_path: Path) -> RunResult:
        raise AssertionError("a recorded attempt must not run again")

    rc = attempt(selected_inp, run, resumed=True, state=state)

    assert rc == 0
    assert capsys.readouterr().out.count("status: completed\n") == 1
    saved = _saved_state(tmp_path)
    final = saved["final_result"]
    assert final is not None
    assert final["status"] == "completed"
    assert final["resumed"]
    assert final["last_out_path"] == str(out_path)


def test_resumed_run_without_a_recorded_attempt_runs_the_selected_input_unchanged(
    tmp_path: Path, attempt: Attempt
) -> None:
    selected_inp = _write_input(tmp_path)
    selected_inp.with_suffix(".gbw").write_bytes(b"checkpoint")
    selected_inp.with_suffix(".xyz").write_text(
        "2\nresume geometry\nH 0 0 0\nH 0 0 0.75\n",
        encoding="utf-8",
    )
    seen: list[Path] = []

    rc = attempt(selected_inp, _capture_success_run(seen), resumed=True)

    saved = _saved_state(tmp_path)
    assert rc == 0
    assert seen == [selected_inp]
    assert selected_inp.read_text(encoding="utf-8") == _OPT_INPUT
    assert sorted(path.name for path in tmp_path.glob("*.inp")) == ["rxn.inp"]
    attempt_record = saved["attempts"][0]
    assert attempt_record["inp_path"] == str(selected_inp)
    assert attempt_record["patch_actions"] == []
    final = saved["final_result"]
    assert final is not None and final["resumed"]


def test_missing_selected_input_fails_before_the_attempt_starts(
    tmp_path: Path, attempt: Attempt
) -> None:
    selected_inp = tmp_path / "rxn.inp"

    def run(_inp_path: Path) -> RunResult:
        raise AssertionError("a missing input must not run")

    assert attempt(selected_inp, run) == 1

    saved = _saved_state(tmp_path)
    assert saved["attempts"] == []
    final = saved["final_result"]
    assert final is not None
    assert final["reason"] == "selected_input_missing"
    assert final["last_out_path"] is None


def test_stalled_started_notification_does_not_delay_calculation(
    tmp_path: Path, attempt: Attempt, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_inp = _write_input(tmp_path)
    entered, release = threading.Event(), threading.Event()
    received: list[object] = []

    def stall(message: object) -> None:
        entered.set()
        assert release.wait(10)
        received.append(message)
        raise RuntimeError("synthetic delivery failure")

    channel = RecordingChannel(on_send=stall)
    monkeypatch.setattr(attempt_run, "notification_channel", lambda _cfg: channel)
    capture = _capture_success_run([])

    def run(inp_path: Path) -> RunResult:
        assert entered.wait(5)
        assert not release.is_set()
        return capture(inp_path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            future = pool.submit(attempt, selected_inp, run)
            assert entered.wait(5)
            assert future.result(timeout=5) == 0
            saved = _saved_state(tmp_path)
            assert saved["status"] == "completed"
            before = (tmp_path / "job_state.json").read_bytes()
        finally:
            release.set()
            for thread in threading.enumerate():
                if thread.name == "orca-started-notification":
                    thread.join(timeout=5)
    assert len(received) == 1
    assert (tmp_path / "job_state.json").read_bytes() == before
