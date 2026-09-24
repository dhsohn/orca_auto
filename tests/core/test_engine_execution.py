from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue import cancellable, metadata, resource_requests


def test_object_attribute_fields_extracts_named_context_values() -> None:
    context = SimpleNamespace(
        job_type="ranking",
        reaction_key="rxn-1",
        input_summary={"candidate_count": 3},
    )

    assert metadata.object_attribute_fields(
        context,
        "job_type",
        "reaction_key",
        "input_summary",
    ) == {
        "job_type": "ranking",
        "reaction_key": "rxn-1",
        "input_summary": {"candidate_count": 3},
    }


def test_default_entry_resource_request_uses_common_resource_caps() -> None:
    cfg = SimpleNamespace(
        resources=SimpleNamespace(max_cores_per_task=8, max_memory_gb_per_task=32),
    )
    entry = SimpleNamespace(metadata={"resource_request": {"max_cores": "4"}})

    from orca_auto.orca.job_locations import resource_dict

    def caps(config: Any) -> dict[str, int]:
        return resource_requests.engine_resource_caps(config, resource_dict_fn=resource_dict)

    assert caps(cfg) == {
        "max_cores": 8,
        "max_memory_gb": 32,
    }
    assert resource_requests.entry_resource_request(cfg, entry, resource_caps_fn=caps) == {
        "max_cores": 4
    }


def test_run_cancellable_process_execution_waits_and_clears_running_job() -> None:
    running = SimpleNamespace(process=SimpleNamespace())
    registered: list[Any | None] = []
    wait_calls: list[tuple[Any, dict[str, Any]]] = []

    def wait_for_process(actual_running: Any, **kwargs: Any) -> str:
        wait_calls.append((actual_running, kwargs))
        return "completed"

    outcome = cancellable.run_cancellable_process_execution(
        cancellable.CancellableProcessExecution(
            start_job=lambda: running,
            finalize_job=lambda *_args, **_kwargs: "finalized",
            terminate_process=lambda _proc: True,
            build_failure_result=lambda exc: f"failed:{exc}",
            wait_for_cancellable_process=wait_for_process,
            should_cancel=lambda: False,
            sleep=lambda _seconds: None,
            poll_interval_seconds=0.25,
            check_cancel_before_poll=True,
            register_running_job=registered.append,
        )
    )

    assert outcome == "completed"
    assert registered == [running, None]
    assert wait_calls[0][0] is running
    assert wait_calls[0][1]["check_cancel_before_poll"] is True
    assert wait_calls[0][1]["poll_interval_seconds"] == 0.25


def test_run_cancellable_process_execution_builds_failure_result() -> None:
    outcome = cancellable.run_cancellable_process_execution(
        cancellable.CancellableProcessExecution(
            start_job=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            finalize_job=lambda *_args, **_kwargs: "finalized",
            terminate_process=lambda _proc: True,
            build_failure_result=lambda exc: f"failed:{exc}",
        )
    )

    assert outcome == "failed:boom"


def test_finalizer_failure_after_group_exit_does_not_terminate_reaped_process() -> None:
    process = SimpleNamespace(poll=lambda: 0)
    running = SimpleNamespace(process=process)
    terminated: list[Any] = []

    def wait_for_process(actual_running: Any, *, finalize_fn: Any, **_kwargs: Any) -> Any:
        return finalize_fn(actual_running)

    def terminate_process(proc: Any) -> bool:
        terminated.append(proc)
        return True

    outcome = cancellable.run_cancellable_process_execution(
        cancellable.CancellableProcessExecution(
            start_job=lambda: running,
            finalize_job=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("finalize failed")
            ),
            terminate_process=terminate_process,
            build_failure_result=lambda exc: f"failed:{exc}",
            wait_for_cancellable_process=wait_for_process,
        )
    )

    assert outcome == "failed:finalize failed"
    assert terminated == []


@pytest.mark.parametrize("failure_source", ["cancel", "poll"])
def test_run_cancellable_process_execution_terminates_after_wait_error(
    failure_source: str,
) -> None:
    class Process:
        exited = False

        def poll(self) -> int | None:
            if failure_source == "poll" and not self.exited:
                raise RuntimeError("poll failed")
            return 0 if self.exited else None

    process = Process()
    running = SimpleNamespace(process=process)
    terminated: list[Any] = []
    registered: list[Any | None] = []

    def should_cancel() -> bool:
        if failure_source == "cancel":
            raise RuntimeError("cancel check failed")
        return False

    def terminate(actual_process: Any) -> bool:
        terminated.append(actual_process)
        actual_process.exited = True
        return True

    outcome = cancellable.run_cancellable_process_execution(
        cancellable.CancellableProcessExecution(
            start_job=lambda: running,
            finalize_job=lambda *_args, **_kwargs: "finalized",
            terminate_process=terminate,
            build_failure_result=lambda exc: f"failed:{exc}",
            should_cancel=should_cancel,
            check_cancel_before_poll=True,
            register_running_job=registered.append,
        )
    )

    expected_error = "cancel check failed" if failure_source == "cancel" else "poll failed"
    assert outcome == f"failed:{expected_error}"
    assert terminated == [process]
    assert registered == [running, None]


@pytest.mark.parametrize("value", [None, "", "relative/job", {"path": "/tmp/job"}])
def test_entry_metadata_resolved_path_rejects_unsafe_values(value: Any) -> None:
    entry = SimpleNamespace(metadata={"job_dir": value})

    with pytest.raises(ValueError, match="Queue metadata 'job_dir'"):
        metadata.entry_metadata_resolved_path(entry, "job_dir")


def test_require_path_within_roots_rejects_symlink_escape(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    outside_root = tmp_path / "outside"
    allowed_root.mkdir()
    outside_root.mkdir()
    escape = allowed_root / "escape"
    escape.symlink_to(outside_root, target_is_directory=True)

    with pytest.raises(ValueError, match="must be under an allowed root"):
        metadata.require_path_within_roots(
            escape / "job",
            (allowed_root,),
            label="Queue metadata 'job_dir'",
        )


def test_run_cancellable_process_execution_can_reraise_policy_exceptions() -> None:
    class ShutdownRequested(RuntimeError):
        pass

    def wait_for_process(_running: Any, **_kwargs: Any) -> str:
        raise ShutdownRequested("shutdown")

    process = SimpleNamespace(exited=False)
    process.poll = lambda: 0 if process.exited else None

    def terminate(_process: Any) -> bool:
        process.exited = True
        return True

    try:
        cancellable.run_cancellable_process_execution(
            cancellable.CancellableProcessExecution(
                start_job=lambda: SimpleNamespace(process=process),
                finalize_job=lambda *_args, **_kwargs: "finalized",
                terminate_process=terminate,
                build_failure_result=lambda exc: f"failed:{exc}",
                wait_for_cancellable_process=wait_for_process,
                should_reraise_exception=lambda exc: isinstance(exc, ShutdownRequested),
            )
        )
    except ShutdownRequested as exc:
        assert str(exc) == "shutdown"
    else:
        raise AssertionError("expected shutdown")


@pytest.mark.parametrize(
    ("termination_mode", "error_match"),
    [
        ("false", "did not confirm"),
        ("raises", "raised before termination"),
        ("still_running", "reported success while the process is still running"),
    ],
)
def test_cleanup_failure_retries_and_never_builds_terminal_result(
    termination_mode: str,
    error_match: str,
) -> None:
    class Process:
        exited = False

        def poll(self) -> int | None:
            return 0 if self.exited else None

    process = Process()
    terminate_calls: list[Process] = []
    finalize_calls: list[Any] = []
    failure_calls: list[Exception] = []
    sleeps: list[float] = []
    registered: list[Any | None] = []

    def terminate(actual_process: Process) -> bool:
        terminate_calls.append(actual_process)
        if len(terminate_calls) == 3:
            actual_process.exited = True
        if termination_mode == "raises":
            raise RuntimeError("termination callback failed")
        return termination_mode == "still_running"

    def sleep(seconds: float) -> None:
        assert registered == [running]
        sleeps.append(seconds)

    running = SimpleNamespace(process=process)

    with pytest.raises(cancellable.ProcessCleanupError, match=error_match):
        cancellable.run_cancellable_process_execution(
            cancellable.CancellableProcessExecution(
                start_job=lambda: running,
                finalize_job=lambda *_args, **_kwargs: finalize_calls.append(True),
                terminate_process=terminate,
                build_failure_result=lambda exc: failure_calls.append(exc),
                should_cancel=lambda: True,
                sleep=sleep,
                check_cancel_before_poll=True,
                register_running_job=registered.append,
                cleanup_retry_attempts=2,
                cleanup_poll_interval_seconds=0.25,
            )
        )

    assert terminate_calls == [process, process, process]
    assert sleeps == [0.25, 0.25]
    assert process.exited is True
    assert finalize_calls == []
    assert failure_calls == []
    assert registered == [running, None]


def test_run_cancellable_engine_process_builds_common_execution_actions() -> None:
    running = SimpleNamespace(process=SimpleNamespace())
    registered: list[Any | None] = []
    wait_kwargs: list[dict[str, Any]] = []

    def wait_for_process(actual_running: Any, **kwargs: Any) -> str:
        assert actual_running is running
        wait_kwargs.append(kwargs)
        return "done"

    outcome = cancellable.run_cancellable_engine_process(
        start_job=lambda: running,
        finalize_job=lambda *_args, **_kwargs: "finalized",
        terminate_process=lambda _proc: True,
        build_failure_result=lambda exc: f"failed:{exc}",
        wait_for_cancellable_process=wait_for_process,
        should_cancel=lambda: False,
        sleep=lambda _seconds: None,
        poll_interval_seconds=0.5,
        check_cancel_before_poll=True,
        register_running_job=registered.append,
    )

    assert outcome == "done"
    assert registered == [running, None]
    assert wait_kwargs[0]["poll_interval_seconds"] == 0.5
    assert wait_kwargs[0]["check_cancel_before_poll"] is True
