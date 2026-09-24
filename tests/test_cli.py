import json
import logging
import logging.handlers
import os
from argparse import Namespace
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from orca_auto import cli as unified_cli
from orca_auto import cli_handlers as cli_run_dir
from orca_auto import cli_queue
from orca_auto.core.admission import reserve_slot
from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR as CONFIG_ENV_VAR
from orca_auto.core.config.discovery import default_shared_config_path as default_config_path
from orca_auto.orca.cli_logging import (
    configure_logging as _configure_logging,
)
from orca_auto.orca.cli_logging import (
    remove_managed_handlers as _remove_managed_handlers,
)
from orca_auto.orca.commands import init as init_command
from orca_auto.orca.commands import run_inp as run_inp_command
from orca_auto.orca.config import load_config
from orca_auto.orca.execution import (
    _emit,
    execute_orca_run,
    existing_completed_out,
    select_latest_inp,
)
from orca_auto.orca.orca_runner import OrcaRunner, RunResult, WorkerShutdownInterrupt
from orca_auto.orca.run_context import RunExecutionContext, configured_admission_root
from orca_auto.orca.run_lock import acquire_run_lock
from orca_auto.orca.state_reading import load_state, state_path
from orca_auto.orca.types import AttemptRecord, RunFinalResult, RunState
from tests.conftest import write_run_state

build_parser = unified_cli.build_parser
main = unified_cli.main

_OPT_INPUT = "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
_NORMAL_TERMINATION = "****ORCA TERMINATED NORMALLY****\n"
_FakeRun = Callable[[OrcaRunner, Path], RunResult]


def _loaded_state(reaction_dir: Path) -> RunState:
    state = load_state(reaction_dir)
    assert state is not None
    return state


def _final_result(state: RunState) -> RunFinalResult:
    final_result = state["final_result"]
    assert final_result is not None
    return final_result


def _reaction(root: Path, name: str, *, inp_text: str = _OPT_INPUT) -> tuple[Path, Path]:
    reaction = root / name
    reaction.mkdir(parents=True)
    inp = reaction / "rxn.inp"
    inp.write_text(inp_text, encoding="utf-8")
    return reaction, inp


def _attempt(inp: Path, out: Path, **fields: object) -> AttemptRecord:
    record: AttemptRecord = {
        "index": 1,
        "inp_path": str(inp),
        "out_path": str(out),
        "return_code": 1,
        "analyzer_status": "incomplete",
        "markers": {},
        "patch_actions": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:00:01+00:00",
    }
    record.update(fields)  # type: ignore[typeddict-item]
    return record


def _run_internal_execute(config: Path, reaction_dir: Path, *, force: bool = False) -> int:
    # Shared admission slots live in the hidden .admission directory
    # under the runs root (= allowed_root).
    token = reserve_slot(
        reaction_dir.parent / ".admission",
        1,
        work_dir=str(reaction_dir),
        source="queue_worker",
        state="reserved",
    )
    assert token is not None
    cfg = load_config(str(config))
    return execute_orca_run(
        RunExecutionContext(
            cfg=cfg,
            reaction_dir=reaction_dir.resolve(),
            selected_inp=select_latest_inp(reaction_dir),
            admission_root=configured_admission_root(cfg),
            reservation_token=token,
            force=force,
        ),
    )


def _runner_must_not_run(_self: OrcaRunner, inp_path: Path) -> RunResult:
    raise AssertionError(f"OrcaRunner.run must not be called (got {inp_path})")


@pytest.fixture
def config(config_path: Callable[..., Path], queue_root: Path) -> Path:
    """An ``orca_auto.yaml`` whose ``runs_root`` is ``queue_root``."""

    return config_path(runs_root=queue_root)


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> Callable[[_FakeRun], None]:
    """Install a stand-in for ``OrcaRunner.run`` (the subprocess boundary)."""

    def install(run: _FakeRun) -> None:
        monkeypatch.setattr(OrcaRunner, "run", run)

    return install


@pytest.fixture
def restored_root_logger() -> Iterator[logging.Logger]:
    """The root logger with its managed handlers cleared, restored afterwards."""

    root_logger = logging.getLogger()
    original_level = root_logger.level
    original_handlers = list(root_logger.handlers)
    _remove_managed_handlers(root_logger)
    try:
        yield root_logger
    finally:
        _remove_managed_handlers(root_logger)
        root_logger.setLevel(original_level)
        for handler in list(root_logger.handlers):
            if handler not in original_handlers:
                root_logger.removeHandler(handler)
        for handler in original_handlers:
            if handler not in root_logger.handlers:
                root_logger.addHandler(handler)


def _managed_handlers(root_logger: logging.Logger) -> list[logging.Handler]:
    return [
        handler
        for handler in root_logger.handlers
        if getattr(handler, "_orca_auto_managed_handler", False)
    ]


# -- argument parsing and dispatch ------------------------------------------


def test_rejects_outside_allowed_root(tmp_path: Path, config: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.inp").write_text(_OPT_INPUT, encoding="utf-8")

    rc = main(["run-dir", "--config", str(config), str(outside)])
    assert rc == 1


def test_error_goes_to_stderr(
    tmp_path: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.inp").write_text("! Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")

    rc = main(["run-dir", "--config", str(config), str(outside)])

    assert rc == 1
    # Error should go to stderr (via logger.error), not stdout
    assert "allowed root" not in capsys.readouterr().out


def test_select_latest_inp_prefers_base_input(tmp_path: Path) -> None:
    base = tmp_path / "rxn.inp"
    retry = tmp_path / "rxn.scfgrad.inp"
    base.write_text("! Opt\n", encoding="utf-8")
    retry.write_text("! Opt\n", encoding="utf-8")
    os.utime(base, ns=(1_000_000_000, 1_000_000_000))
    os.utime(retry, ns=(2_000_000_000, 2_000_000_000))
    assert select_latest_inp(tmp_path).name == "rxn.inp"


def test_select_latest_inp_warns_when_multiple_base_inputs_exist(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    older = tmp_path / "b.inp"
    newer = tmp_path / "a.inp"
    older.write_text("! Opt\n", encoding="utf-8")
    newer.write_text("! Opt\n", encoding="utf-8")
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

    with caplog.at_level(logging.WARNING, logger="orca_auto.orca.execution"):
        selected = select_latest_inp(tmp_path)

    # `a.inp` is the newer file here: stamped against the name order so the
    # assertion cannot pass on the alphabetical tie-break alone.
    assert selected.name == "a.inp"
    assert "Multiple ORCA .inp candidates" in caplog.text


def test_existing_completed_out_ignores_stale_output_older_than_selected_input(
    tmp_path: Path,
) -> None:
    inp = tmp_path / "rxn.inp"
    out = tmp_path / "rxn.out"
    inp.write_text("! Opt\n", encoding="utf-8")
    out.write_text(_NORMAL_TERMINATION, encoding="utf-8")
    os.utime(out, ns=(1_000_000_000, 1_000_000_000))
    os.utime(inp, ns=(2_000_000_000, 2_000_000_000))

    assert existing_completed_out(inp) is None


def test_default_config_path_prefers_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, "/tmp/custom_orca_auto.yaml")
    assert default_config_path() == "/tmp/custom_orca_auto.yaml"


def test_run_dir_accepts_queue_submission_flags() -> None:
    args = build_parser().parse_args(
        ["run-dir", "/tmp/rxn", "--priority", "3", "--force", "--json"]
    )

    assert args.command == "run-dir"
    assert args.path == "/tmp/rxn"
    assert args.priority == 3
    assert args.force
    assert args.json


def test_run_dir_rejects_foreground_flag() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["run-dir", "/tmp/rxn", "--foreground"])
    assert exc.value.code == 2


def test_queue_add_is_not_a_valid_subcommand() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["queue", "add"])
    assert exc.value.code == 2


def test_main_dispatches_run_dir_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Namespace] = []

    def cmd_run_dir(args: Namespace) -> int:
        calls.append(args)
        return 8

    monkeypatch.setattr(cli_run_dir, "cmd_run_dir", cmd_run_dir)
    assert main(["run-dir", "/tmp/rxn"]) == 8
    assert len(calls) == 1


def test_main_dispatches_list_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Namespace] = []

    def cmd_queue_list(args: Namespace) -> int:
        calls.append(args)
        return 9

    monkeypatch.setattr(cli_queue, "cmd_queue_list", cmd_queue_list)
    assert main(["queue", "list", "--status", "running"]) == 9
    assert len(calls) == 1


def test_cmd_run_inp_dispatches_to_orca_command_module(
    monkeypatch: pytest.MonkeyPatch, restored_root_logger: logging.Logger
) -> None:
    seen: list[Namespace] = []
    resolved_config = object()

    def _fake_run_inp(args: Namespace, **_seams: Any) -> int:
        seen.append(args)
        return 41

    monkeypatch.setattr(run_inp_command, "cmd_run_inp", _fake_run_inp)
    monkeypatch.setattr(cli_run_dir, "engine_config_for_args", lambda _args: resolved_config)
    args = Namespace(
        config="/tmp/orca_auto.yaml",
        verbose=True,
        log_file="/tmp/orca.log",
        path="/tmp/rxn",
        priority=3,
        force=True,
        max_cores=12,
        max_memory_gb=48,
    )
    rc = cli_run_dir.cmd_orca_run_dir(args)

    assert rc == 41
    assert seen == [args]
    # The wrapper must hand the command the engine-resolved config, not the
    # raw parser value the identity check alone would accept.
    assert seen[0].config is resolved_config


def test_other_public_wrappers_dispatch_to_orca_command_modules(
    monkeypatch: pytest.MonkeyPatch, restored_root_logger: logging.Logger
) -> None:
    seen: list[tuple[str, Namespace]] = []

    def _record(name: str, return_code: int) -> Callable[..., int]:
        def _inner(args: Namespace, **_seams: Any) -> int:
            seen.append((name, args))
            return return_code

        return _inner

    resolved_config = object()
    monkeypatch.setattr(init_command, "cmd_init", _record("init", 42))
    monkeypatch.setattr(cli_run_dir, "engine_config_for_args", lambda _args: resolved_config)
    init_args = Namespace(config="/tmp/orca_auto.yaml", verbose=False, log_file=None, force=True)
    init_rc = cli_run_dir.cmd_init(init_args)

    assert init_rc == 42
    assert seen == [("init", init_args)]
    # The wrapper must hand the command the engine-resolved config, not the
    # raw parser value the identity check alone would accept.
    assert seen[0][1].config is resolved_config


def test_emit_plain_text_filters_known_keys(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "status": "completed",
        "reaction_dir": "/tmp/rxn",
        "selected_inp": "/tmp/rxn/rxn.inp",
        "attempt_count": 1,
        "reason": "normal_termination",
        "run_state": "/tmp/rxn/job_state.json",
        "extra_unknown_key": "ignored",
    }
    _emit(payload)
    output = capsys.readouterr().out
    assert "status: completed" in output
    assert "attempt_count: 1" in output
    assert "extra_unknown_key" not in output


# -- logging ----------------------------------------------------------------


def test_configure_logging_replaces_previous_orca_auto_handler(
    restored_root_logger: logging.Logger,
) -> None:
    _configure_logging(Namespace(verbose=False, log_file=None))
    _configure_logging(Namespace(verbose=True, log_file=None))

    assert len(_managed_handlers(restored_root_logger)) == 1
    assert restored_root_logger.level == logging.DEBUG


def test_configure_logging_uses_rotating_file_handler_when_log_file_is_set(
    tmp_path: Path, restored_root_logger: logging.Logger
) -> None:
    log_file = tmp_path / "orca_auto.log"

    _configure_logging(Namespace(verbose=False, log_file=str(log_file)))

    [handler] = _managed_handlers(restored_root_logger)
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.baseFilename == str(log_file)
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 5
    assert handler.encoding == "utf-8"
    assert handler.formatter is not None
    assert restored_root_logger.level == logging.INFO


def test_remove_managed_handlers_ignores_close_errors() -> None:
    class _ExplodingManagedHandler(logging.Handler):
        _orca_auto_managed_handler = True

        def close(self) -> None:
            raise RuntimeError("boom")

    root_logger = logging.Logger("test_cli_remove_managed")
    unmanaged = logging.StreamHandler()
    managed = _ExplodingManagedHandler()
    root_logger.addHandler(unmanaged)
    root_logger.addHandler(managed)

    _remove_managed_handlers(root_logger)

    assert unmanaged in root_logger.handlers
    assert managed not in root_logger.handlers


def test_main_removes_the_managed_log_handler_after_a_command(
    monkeypatch: pytest.MonkeyPatch, restored_root_logger: logging.Logger
) -> None:
    root = restored_root_logger
    before = list(root.handlers)

    def command(args: Namespace) -> int:
        _configure_logging(args)
        assert len(root.handlers) == len(before) + 1
        return 0

    parser = unified_cli.build_parser()
    monkeypatch.setattr(
        parser, "parse_args", lambda argv: Namespace(func=command, verbose=False, log_file=None)
    )
    monkeypatch.setattr(unified_cli, "build_parser", lambda: parser)
    assert unified_cli.main(["queue", "list"]) == 0
    assert root.handlers == before


# -- run-dir against a completed output --------------------------------------


def test_run_dir_queues_existing_completed_out_for_worker_reconciliation(
    queue_root: Path, config: Path
) -> None:
    reaction, _inp = _reaction(queue_root, "rxn1")
    (reaction / "rxn.out").write_text(_NORMAL_TERMINATION, encoding="utf-8")

    rc = main(["run-dir", "--config", str(config), str(reaction)])

    assert rc == 0
    assert not state_path(reaction).exists()


def test_skip_existing_completed_out_still_respects_run_lock(
    queue_root: Path, config: Path
) -> None:
    reaction, _inp = _reaction(queue_root, "rxn1_locked")
    (reaction / "rxn.out").write_text(_NORMAL_TERMINATION, encoding="utf-8")

    with acquire_run_lock(reaction):
        rc = main(["run-dir", "--config", str(config), str(reaction)])

    assert rc == 1
    assert not state_path(reaction).exists()


def test_run_dir_preserves_existing_state_until_worker_reconciles_completed_output(
    queue_root: Path, config: Path, fake_run: Callable[[_FakeRun], None]
) -> None:
    reaction, inp = _reaction(queue_root, "rxn1_resume_skip")
    out = reaction / "rxn.out"
    out.write_text(_NORMAL_TERMINATION, encoding="utf-8")
    write_run_state(
        reaction,
        status="failed",
        run_id="run_resume_skip_existing_out",
        selected_inp=inp,
        attempts=[_attempt(inp, out, analyzer_reason="run_incomplete")],
        final_result={
            "status": "failed",
            "analyzer_status": "incomplete",
            "reason": "worker_shutdown",
            "completed_at": "2026-01-01T00:00:02+00:00",
            "last_out_path": str(out),
        },
    )
    fake_run(_runner_must_not_run)

    rc = main(["run-dir", "--config", str(config), str(reaction)])

    saved = _loaded_state(reaction)
    assert rc == 0
    assert saved["run_id"] == "run_resume_skip_existing_out"
    assert saved["status"] == "failed"
    assert _final_result(saved)["reason"] == "worker_shutdown"


# -- internal execution: runner outcomes -------------------------------------


def test_worker_shutdown_propagates_without_finalizing_failed_state(
    queue_root: Path, config: Path, fake_run: Callable[[_FakeRun], None]
) -> None:
    reaction, _inp = _reaction(queue_root, "rxn5_worker_shutdown")

    def _fake_run(_self: OrcaRunner, inp_path: Path) -> RunResult:
        raise WorkerShutdownInterrupt

    fake_run(_fake_run)
    with pytest.raises(WorkerShutdownInterrupt):
        _run_internal_execute(config, reaction)

    saved = _loaded_state(reaction)
    assert saved["status"] == "running"
    assert saved["final_result"] is None
    assert len(saved["attempts"]) == 0


def test_standalone_optts_failure_does_not_retry(
    queue_root: Path, config: Path, fake_run: Callable[[_FakeRun], None]
) -> None:
    reaction, _inp = _reaction(
        queue_root, "rxn2", inp_text="! OptTS Freq IRC\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    )
    calls: list[Path] = []

    def _fake_run(_self: OrcaRunner, inp_path: Path) -> RunResult:
        calls.append(inp_path)
        out = inp_path.with_suffix(".out")
        out.write_text("ORCA finished by error termination in SCF gradient\n", encoding="utf-8")
        return RunResult(out_path=str(out), return_code=55)

    fake_run(_fake_run)
    rc = _run_internal_execute(config, reaction)

    state = _loaded_state(reaction)
    assert rc == 1
    assert len(calls) == 1
    assert not (reaction / "rxn.retry01.inp").exists()
    assert state["status"] == "failed"
    assert "max_retries" not in state
    assert len(state["attempts"]) == 1


def test_disk_io_error_without_restart_artifacts_fails_closed(
    queue_root: Path, config: Path, fake_run: Callable[[_FakeRun], None]
) -> None:
    reaction, _inp = _reaction(queue_root, "rxn_disk")
    calls: list[Path] = []

    def _fake_run(_self: OrcaRunner, inp_path: Path) -> RunResult:
        calls.append(inp_path)
        out = inp_path.with_suffix(".out")
        out.write_text("COULD NOT WRITE TO DISK\n", encoding="utf-8")
        return RunResult(out_path=str(out), return_code=99)

    fake_run(_fake_run)
    rc = _run_internal_execute(config, reaction)

    state = _loaded_state(reaction)
    assert rc == 1
    assert len(calls) == 1
    assert not (reaction / "rxn.retry01.inp").exists()
    assert state["status"] == "failed"
    assert "max_retries" not in state
    assert len(state["attempts"]) == 1
    final_result = _final_result(state)
    assert final_result["reason"] == "disk_write_failed"
    assert final_result["analyzer_status"] == "error_disk_io"


def test_resume_preserves_recorded_analyzer_reason(
    queue_root: Path, config: Path, fake_run: Callable[[_FakeRun], None]
) -> None:
    reaction, inp = _reaction(queue_root, "rxn4")
    retry_inp = reaction / "rxn.retry01.inp"
    retry_out = reaction / "rxn.retry01.out"
    write_run_state(
        reaction,
        status="running",
        run_id="run_test_resume",
        selected_inp=inp,
        attempts=[
            _attempt(inp, reaction / "rxn.out"),
            _attempt(
                retry_inp,
                retry_out,
                index=2,
                analyzer_reason="run_incomplete",
                started_at="2026-01-01T00:00:02+00:00",
                ended_at="2026-01-01T00:00:03+00:00",
            ),
        ],
    )
    fake_run(_runner_must_not_run)

    rc = _run_internal_execute(config, reaction)

    saved = _loaded_state(reaction)
    assert rc == 1
    assert saved["status"] == "failed"
    final_result = _final_result(saved)
    assert final_result["reason"] == "run_incomplete"
    assert final_result["last_out_path"] == str(retry_out)


def test_resume_interrupted_failure_keeps_run_id_and_continues(
    queue_root: Path, config: Path, fake_run: Callable[[_FakeRun], None]
) -> None:
    reaction, inp = _reaction(queue_root, "rxn_resume_interrupt")
    write_run_state(
        reaction,
        status="failed",
        run_id="run_resume_interrupted",
        selected_inp=inp,
        final_result={
            "status": "failed",
            "analyzer_status": "incomplete",
            "reason": "interrupted_by_user",
            "completed_at": "2026-01-01T00:00:02+00:00",
            "last_out_path": str(reaction / "rxn.out"),
        },
    )
    seen: list[str] = []

    def _fake_run(_self: OrcaRunner, inp_path: Path) -> RunResult:
        seen.append(inp_path.name)
        out = inp_path.with_suffix(".out")
        out.write_text(_NORMAL_TERMINATION, encoding="utf-8")
        return RunResult(out_path=str(out), return_code=0)

    fake_run(_fake_run)
    rc = _run_internal_execute(config, reaction)

    saved = _loaded_state(reaction)
    assert rc == 0
    assert saved["run_id"] == "run_resume_interrupted"
    assert seen == ["rxn.inp"]
    assert not (reaction / "rxn.retry01.inp").exists()
    assert saved["status"] == "completed"
    assert len(saved["attempts"]) == 1
    assert _final_result(saved)["resumed"]


def test_resume_completed_attempt_finalizes_without_extra_run(
    queue_root: Path, config: Path, fake_run: Callable[[_FakeRun], None]
) -> None:
    reaction, inp = _reaction(queue_root, "rxn_resume_done")
    out = reaction / "rxn.out"
    out.write_text("SCF NOT CONVERGED\n", encoding="utf-8")
    write_run_state(
        reaction,
        status="running",
        run_id="run_resume_completed",
        selected_inp=inp,
        attempts=[
            _attempt(
                inp,
                out,
                return_code=0,
                analyzer_status="completed",
                analyzer_reason="normal_termination",
            )
        ],
    )
    fake_run(_runner_must_not_run)

    rc = _run_internal_execute(config, reaction)

    saved = _loaded_state(reaction)
    assert rc == 0
    assert saved["status"] == "completed"
    assert len(saved["attempts"]) == 1
    final_result = _final_result(saved)
    assert final_result["reason"] == "normal_termination"
    assert final_result["resumed"]


def test_keyboard_interrupt_stops_run_and_finalizes_state(
    queue_root: Path, config: Path, fake_run: Callable[[_FakeRun], None]
) -> None:
    reaction, _inp = _reaction(queue_root, "rxn5")

    def _fake_run(_self: OrcaRunner, inp_path: Path) -> RunResult:
        raise KeyboardInterrupt

    fake_run(_fake_run)
    rc = _run_internal_execute(config, reaction)

    saved = _loaded_state(reaction)
    assert rc == 130
    assert saved["status"] == "failed"
    final_result = _final_result(saved)
    assert final_result["reason"] == "interrupted_by_user"
    assert final_result["analyzer_status"] == "incomplete"
    assert len(saved["attempts"]) == 0


def test_runner_exception_finalizes_state_with_failure(
    queue_root: Path, config: Path, fake_run: Callable[[_FakeRun], None]
) -> None:
    reaction, _inp = _reaction(queue_root, "rxn6")

    def _fake_run(_self: OrcaRunner, inp_path: Path) -> RunResult:
        raise RuntimeError("runner exploded")

    fake_run(_fake_run)
    rc = _run_internal_execute(config, reaction)

    saved = _loaded_state(reaction)
    assert rc == 1
    assert saved["status"] == "failed"
    final_result = _final_result(saved)
    assert final_result["reason"] == "runner_exception"
    assert final_result["analyzer_status"] == "incomplete"
    assert final_result["runner_error"] == "runner exploded"
    assert len(saved["attempts"]) == 0


def _json_documents(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    documents: list[dict[str, Any]] = []
    position = 0
    while position < len(text):
        document, end = decoder.raw_decode(text, position)
        documents.append(document)
        position = end
        while position < len(text) and text[position].isspace():
            position += 1
    return documents


def test_emit_json_document_shape(capsys: pytest.CaptureFixture[str]) -> None:
    from orca_auto import terminal

    terminal.emit_json({"count": 1})
    terminal.emit_json({"count": 0}, ok=False, error="nothing matched")
    terminal.emit_json(ok=False)

    success, failure, bare = _json_documents(capsys.readouterr().out)
    assert list(success) == ["ok", "count"]
    assert success == {"ok": True, "count": 1}
    assert failure == {"ok": False, "count": 0, "error": "nothing matched"}
    assert bare == {"ok": False, "error": "command failed"}


def test_emit_error_under_json_prints_the_error_document_and_the_stderr_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from orca_auto import terminal

    terminal.emit_error("queue file is corrupt", hint="repair it", json_output=True)
    terminal.emit_error("plain failure")

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": False, "error": "queue file is corrupt"}
    assert captured.err == "error: queue file is corrupt\nhint: repair it\nerror: plain failure\n"
