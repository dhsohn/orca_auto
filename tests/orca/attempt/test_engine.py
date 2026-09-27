from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.core.engine_scratch import (
    ScratchPublication,
    attach_scratch_provenance_to_exception,
)
from orca_auto.orca.attempt import engine as attempt_engine
from orca_auto.orca.attempt.engine import (
    RunStartedNotification,
    run_attempts,
)
from orca_auto.orca.orca_runner import WorkerShutdownInterrupt
from orca_auto.orca.state import new_state
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.types import RunState

_OPT_INPUT = "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
_NORMAL_TERMINATION = (
    "****ORCA TERMINATED NORMALLY****\nTOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds 0 msec\n"
)


class _InterruptRunner:
    def run(self, _inp_path: Path):
        raise KeyboardInterrupt


class _InPlaceFailureRunner:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def run(self, inp_path: Path):
        inp_path.with_suffix(".out").write_text("partial output\n", encoding="utf-8")
        raise self.exc


class _WorkerShutdownRunner:
    def run(self, _inp_path: Path):
        raise WorkerShutdownInterrupt


class _WorkerShutdownWithScratchRunner:
    def run(self, inp_path: Path):
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


class _PublishedSuccessRunner:
    def run(self, inp_path: Path):
        out_path = inp_path.with_suffix(".out")
        out_path.write_text("published output\n", encoding="utf-8")
        return SimpleNamespace(
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


class _AlwaysScfFailRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path):
        self.seen.append(inp_path)
        inp_path.with_suffix(".xyz").write_text(
            "2\ncheckpoint geometry\nH 0 0 0\nH 0 0 0.75\n",
            encoding="utf-8",
        )
        out_path = inp_path.with_suffix(".out")
        out_path.write_text("SCF NOT CONVERGED AFTER 300 CYCLES\n", encoding="utf-8")
        return SimpleNamespace(out_path=str(out_path), return_code=1)


class _NoArtifactScfFailRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path):
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        out_path.write_text("SCF NOT CONVERGED AFTER 300 CYCLES\n", encoding="utf-8")
        return SimpleNamespace(out_path=str(out_path), return_code=1)


class _NoImaginaryModeRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path):
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        out_path.write_text(
            "VIBRATIONAL FREQUENCIES\n  1    120.00 cm**-1\n  2    240.00 cm**-1\n"
            "****ORCA TERMINATED NORMALLY****\n",
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _UnusedRunner:
    def run(self, _inp_path: Path):
        raise AssertionError("runner.run() should not be called for terminal resumed attempts")


class _CaptureSuccessRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path):
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        out_path.write_text(_NORMAL_TERMINATION, encoding="utf-8")
        return SimpleNamespace(
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


def _fresh_run(
    reaction_dir: Path, *, name: str = "rxn.inp", text: str = _OPT_INPUT
) -> tuple[Path, RunState]:
    """Write the selected input and return it with the in-memory state the engine mutates."""

    selected_inp = reaction_dir / name
    selected_inp.write_text(text, encoding="utf-8")
    return selected_inp, new_state(reaction_dir, selected_inp)


def _saved_state(reaction_dir: Path) -> RunState:
    saved = load_state(reaction_dir)
    assert saved is not None
    return saved


def test_keyboard_interrupt_emits_single_run_interrupted_event(tmp_path: Path) -> None:
    selected_inp, state = _fresh_run(tmp_path)
    emitted_payloads: list[object] = []

    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=False,
        runner=_InterruptRunner(),
        emit=emitted_payloads.append,
    )

    saved = _saved_state(tmp_path)
    final_result = saved["final_result"]
    assert final_result is not None
    assert rc == 130
    assert final_result["reason"] == "interrupted_by_user"
    assert saved["status"] == "failed"
    assert len(emitted_payloads) == 1


@pytest.mark.parametrize(
    ("exc", "expected_rc", "expected_reason"),
    [
        (KeyboardInterrupt(), 130, "interrupted_by_user"),
        (RuntimeError("injected runner failure"), 1, "runner_exception"),
    ],
    ids=["interrupted_by_user", "runner_exception"],
)
def test_in_place_failure_preserves_existing_output_path(
    tmp_path: Path, exc: BaseException, expected_rc: int, expected_reason: str
) -> None:
    selected_inp, state = _fresh_run(tmp_path, text="! SP\n")

    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=False,
        runner=_InPlaceFailureRunner(exc),
        emit=lambda _payload: None,
    )

    saved = _saved_state(tmp_path)
    final_result = saved["final_result"]
    assert final_result is not None
    assert rc == expected_rc
    assert final_result["reason"] == expected_reason
    assert saved["attempts"] == []
    assert saved.get("scratch_publications", []) == []
    assert final_result["last_out_path"] == str(selected_inp.with_suffix(".out"))


def test_in_place_failure_does_not_report_symlink_output(tmp_path: Path) -> None:
    selected_inp, state = _fresh_run(tmp_path, text="! SP\n")
    outside_out = tmp_path / "outside.out"
    outside_out.write_text("outside\n", encoding="utf-8")
    selected_inp.with_suffix(".out").symlink_to(outside_out)

    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=False,
        runner=_InterruptRunner(),
        emit=lambda _payload: None,
    )

    final_result = _saved_state(tmp_path)["final_result"]
    assert final_result is not None
    assert rc == 130
    assert final_result["last_out_path"] is None


def test_worker_shutdown_propagates_without_failed_final_result(tmp_path: Path) -> None:
    selected_inp, state = _fresh_run(tmp_path)
    emitted_payloads: list[object] = []

    with pytest.raises(WorkerShutdownInterrupt):
        run_attempts(
            tmp_path,
            selected_inp,
            state,
            resumed=False,
            runner=_WorkerShutdownRunner(),
            emit=emitted_payloads.append,
        )

    saved = _saved_state(tmp_path)
    assert saved["final_result"] is None
    assert saved["status"] == "running"
    assert emitted_payloads == []


def test_worker_shutdown_persists_committed_scratch_publication(tmp_path: Path) -> None:
    selected_inp, state = _fresh_run(tmp_path, text="! SP\n")

    with pytest.raises(WorkerShutdownInterrupt):
        run_attempts(
            tmp_path,
            selected_inp,
            state,
            resumed=False,
            runner=_WorkerShutdownWithScratchRunner(),
            emit=lambda _payload: None,
        )

    saved = _saved_state(tmp_path)
    assert saved["attempts"] == []
    assert saved["final_result"] is None
    publication = saved["scratch_publications"][0]
    assert publication["attempt_index"] == 1
    assert publication["outcome"] == "worker_shutdown"
    assert publication["publication"]["published_files"] == ["rxn.gbw", "rxn.out"]


def test_analyzer_exception_persists_committed_scratch_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_inp, state = _fresh_run(tmp_path, text="! SP\n")

    def failing_analyzer(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected analyzer failure")

    monkeypatch.setattr(attempt_engine, "analyze_output", failing_analyzer)
    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=False,
        runner=_PublishedSuccessRunner(),
        emit=lambda _payload: None,
    )

    saved = _saved_state(tmp_path)
    assert rc == 1
    assert saved["attempts"] == []
    assert saved["scratch_publications"][0]["outcome"] == "exception"
    assert saved["final_result"] is not None
    assert saved["final_result"]["last_out_path"] == str(selected_inp.with_suffix(".out"))


@pytest.mark.parametrize(
    "runner_factory",
    [_AlwaysScfFailRunner, _NoArtifactScfFailRunner],
    ids=["with_restart_artifacts", "without_restart_artifacts"],
)
def test_scf_failure_is_terminal(
    tmp_path: Path, runner_factory: type[_AlwaysScfFailRunner | _NoArtifactScfFailRunner]
) -> None:
    selected_inp, state = _fresh_run(tmp_path)
    runner = runner_factory()

    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=False,
        runner=runner,
        emit=lambda _payload: None,
    )

    saved = _saved_state(tmp_path)
    assert rc == 1
    assert runner.seen == [selected_inp]
    assert not (tmp_path / "rxn.retry01.inp").exists()
    assert "max_retries" not in saved
    final_result = saved.get("final_result")
    assert final_result is not None
    assert final_result.get("reason") == "scf_not_converged"


def test_neb_ts_failure_is_terminal(tmp_path: Path) -> None:
    selected_inp, state = _fresh_run(
        tmp_path,
        name="neb.inp",
        text="! NEB-TS B3LYP def2-SVP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
    )
    runner = _NoImaginaryModeRunner()

    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=False,
        runner=runner,
        emit=lambda _payload: None,
    )

    saved = _saved_state(tmp_path)
    assert rc == 1
    assert runner.seen == [selected_inp]
    assert not (tmp_path / "neb.retry01.inp").exists()
    assert "max_retries" not in saved
    final_result = saved.get("final_result")
    assert final_result is not None
    assert final_result.get("reason") == "ts_criteria_failed"


def test_start_notification_and_persisted_terminal_result_describe_one_attempt(
    tmp_path: Path,
) -> None:
    selected_inp, state = _fresh_run(tmp_path)
    started_notifications: list[RunStartedNotification] = []
    delivered = threading.Event()

    def notify(event: RunStartedNotification) -> None:
        started_notifications.append(event)
        delivered.set()

    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=False,
        runner=_CaptureSuccessRunner(),
        emit=lambda _payload: None,
        notify_started=notify,
    )

    assert rc == 0
    assert delivered.wait(5)
    assert len(started_notifications) == 1

    started = started_notifications[0]
    assert started["attempt_index"] == 1
    assert started["status"] == "running"
    assert started["current_inp"].endswith("rxn.inp")

    saved = _saved_state(tmp_path)
    finished = saved["final_result"]
    assert finished is not None
    assert finished["status"] == "completed"
    assert finished["analyzer_status"] == "completed"
    assert finished["reason"] == "normal_termination"
    assert len(saved["attempts"]) == 1
    last_out_path = finished["last_out_path"]
    assert last_out_path is not None
    assert last_out_path.endswith("rxn.out")


def test_resumed_terminal_attempt_finishes_without_running_again(tmp_path: Path) -> None:
    selected_inp, state = _fresh_run(tmp_path)
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
    emitted_payloads: list[object] = []

    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=True,
        runner=_UnusedRunner(),
        emit=emitted_payloads.append,
    )

    assert rc == 0
    assert len(emitted_payloads) == 1
    saved = _saved_state(tmp_path)
    final = saved["final_result"]
    assert final is not None
    assert final["status"] == "completed"
    assert final["resumed"]
    assert final["last_out_path"] == str(out_path)


def test_resumed_run_uses_gbw_checkpoint_restart_input(tmp_path: Path) -> None:
    selected_inp, state = _fresh_run(tmp_path)
    selected_inp.with_suffix(".gbw").write_bytes(b"checkpoint")
    selected_inp.with_suffix(".xyz").write_text(
        "2\nresume geometry\nH 0 0 0\nH 0 0 0.75\n",
        encoding="utf-8",
    )
    runner = _CaptureSuccessRunner()

    rc = run_attempts(
        tmp_path,
        selected_inp,
        state,
        resumed=True,
        runner=runner,
        emit=lambda _payload: None,
    )

    saved = _saved_state(tmp_path)
    resume_inp = tmp_path / "rxn.resume.inp"
    resume_text = resume_inp.read_text(encoding="utf-8")
    assert rc == 0
    assert runner.seen == [resume_inp]
    assert '%moinp "rxn.gbw"' in resume_text
    assert "MORead" in resume_text
    attempt = saved["attempts"][0]
    assert attempt["inp_path"] == str(resume_inp)
    assert attempt["command"] == ["/opt/orca/orca", resume_inp.name]
    assert attempt["input_identity"]["path"] == str(resume_inp)
    assert attempt["executable_identity"]["sha256"] == "b" * 64
    assert "resume_checkpoint_restart_from_rxn.gbw" in attempt["patch_actions"]
    assert "resume_geometry_restart_from_rxn.xyz" in attempt["patch_actions"]


def test_stalled_started_callback_does_not_delay_calculation(tmp_path: Path) -> None:
    selected_inp, state = _fresh_run(tmp_path)
    entered, release = threading.Event(), threading.Event()
    received: list[RunStartedNotification] = []

    def notify(event: RunStartedNotification) -> None:
        entered.set()
        assert release.wait(10)
        received.append(event)
        raise RuntimeError("synthetic delivery failure")

    class Runner(_CaptureSuccessRunner):
        def run(self, inp_path: Path):
            assert entered.wait(5)
            assert not release.is_set()
            return super().run(inp_path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            future = pool.submit(
                run_attempts,
                tmp_path,
                selected_inp,
                state,
                resumed=False,
                runner=Runner(),
                emit=lambda _payload: None,
                notify_started=notify,
            )
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
    assert received[0]["status"] == "running"
    assert (tmp_path / "job_state.json").read_bytes() == before
