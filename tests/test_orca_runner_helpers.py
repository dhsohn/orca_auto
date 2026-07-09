from __future__ import annotations

import signal
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from orca_auto.orca.orca_runner import OrcaRunner
from tests.process_helpers import patch_missing_process_group


def test_ensure_trailing_newline_only_appends_when_needed(tmp_path: Path) -> None:
    runner = OrcaRunner("/opt/orca/orca")

    empty_inp = tmp_path / "empty.inp"
    empty_inp.write_bytes(b"")
    runner._ensure_trailing_newline(empty_inp)
    assert empty_inp.read_bytes() == b""

    newline_inp = tmp_path / "newline.inp"
    newline_inp.write_bytes(b"! Opt\n")
    runner._ensure_trailing_newline(newline_inp)
    assert newline_inp.read_bytes() == b"! Opt\n"

    missing_newline_inp = tmp_path / "missing_newline.inp"
    missing_newline_inp.write_bytes(b"! Opt")
    runner._ensure_trailing_newline(missing_newline_inp)
    assert missing_newline_inp.read_bytes() == b"! Opt\n"


def test_terminate_subprocess_tree_falls_back_to_terminate_when_sigterm_group_kill_fails() -> None:
    runner = OrcaRunner("/opt/orca/orca")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 4242
    proc.wait.return_value = 0

    with (
        patch_missing_process_group("orca_auto.orca.orca_runner.os.killpg"),
        patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=False),
    ):
        assert runner._terminate_subprocess_tree(proc)

    proc.terminate.assert_called_once()


def test_terminate_subprocess_tree_falls_back_to_proc_kill_when_sigkill_group_kill_fails() -> None:
    runner = OrcaRunner("/opt/orca/orca")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 4343
    proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="orca", timeout=3),
        0,
    ]

    with (
        patch(
            "orca_auto.orca.orca_runner.os.killpg",
            side_effect=[None, ProcessLookupError("no pg kill")],
        ),
        patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=False),
    ):
        assert runner._terminate_subprocess_tree(proc)

    proc.kill.assert_called_once()


def test_terminate_subprocess_tree_waits_after_sigkill() -> None:
    runner = OrcaRunner("/opt/orca/orca")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 4646
    proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="orca", timeout=3),
        0,
    ]

    with (
        patch("orca_auto.orca.orca_runner.os.killpg") as killpg,
        patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=False),
    ):
        assert runner._terminate_subprocess_tree(proc)

    assert killpg.mock_calls == [
        call(4646, signal.SIGTERM),
        call(4646, signal.SIGKILL),
    ]
    assert [item.kwargs["timeout"] for item in proc.wait.mock_calls] == pytest.approx(
        [3, 5], rel=1e-4
    )


def test_terminate_subprocess_tree_ignores_terminate_failure_when_sigterm_group_kill_fails() -> (
    None
):
    runner = OrcaRunner("/opt/orca/orca")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 4444
    proc.terminate.side_effect = Exception("terminate failed")
    proc.wait.return_value = 0

    with (
        patch_missing_process_group("orca_auto.orca.orca_runner.os.killpg"),
        patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=False),
    ):
        assert runner._terminate_subprocess_tree(proc)

    proc.terminate.assert_called_once()


def test_terminate_subprocess_tree_ignores_proc_kill_failure_when_sigkill_group_kill_fails() -> (
    None
):
    runner = OrcaRunner("/opt/orca/orca")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 4545
    proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="orca", timeout=3),
        subprocess.TimeoutExpired(cmd="orca", timeout=5),
    ]
    proc.kill.side_effect = Exception("kill failed")

    with (
        patch(
            "orca_auto.orca.orca_runner.os.killpg",
            side_effect=[None, ProcessLookupError("no pg kill")],
        ),
        patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=True),
    ):
        assert not runner._terminate_subprocess_tree(proc)

    proc.kill.assert_called_once()


@patch("orca_auto.orca.orca_runner.subprocess.Popen")
@patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
@patch("orca_auto.orca.orca_runner.signal.signal", side_effect=ValueError("not main thread"))
def test_run_handles_signal_install_value_error(
    _mock_signal: MagicMock,
    _mock_getsignal: MagicMock,
    mock_popen: MagicMock,
) -> None:
    proc = MagicMock()
    proc.wait.return_value = 0
    mock_popen.return_value = proc

    runner = OrcaRunner("/opt/orca/orca")
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "test.inp"
        inp.write_text("! Opt\n", encoding="utf-8")
        result = runner.run(inp)

    assert result.return_code == 0
    assert result.out_path.endswith("test.out")


@patch("orca_auto.orca.orca_runner.subprocess.Popen")
@patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
def test_run_ignores_restore_signal_value_error(
    _mock_getsignal: MagicMock,
    mock_popen: MagicMock,
) -> None:
    proc = MagicMock()
    proc.wait.return_value = 0
    mock_popen.return_value = proc

    restore_calls = []

    def _signal(_sig: int, handler):
        restore_calls.append(handler)
        if len(restore_calls) == 2:
            raise ValueError("restore failed")
        return None

    runner = OrcaRunner("/opt/orca/orca")
    with patch("orca_auto.orca.orca_runner.signal.signal", side_effect=_signal):
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "test.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            result = runner.run(inp)

    assert result.return_code == 0
    assert len(restore_calls) == 2
