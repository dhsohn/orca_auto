from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import Mock, patch

import orca_auto.core.utils.process_tracking as process_tracking


def test_current_process_lock_payload_omits_ticks_when_unavailable() -> None:
    with (
        patch("orca_auto.core.utils.process_tracking.os.getpid", return_value=4321),
        patch(
            "orca_auto.core.utils.process_tracking.now_utc_iso",
            return_value="2026-03-22T00:00:00+00:00",
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.current_process_start_ticks",
            return_value=None,
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.current_boot_id",
            return_value=None,
        ),
    ):
        payload = process_tracking.current_process_lock_payload()

    assert payload == {
        "pid": 4321,
        "started_at": "2026-03-22T00:00:00+00:00",
    }


def test_current_process_lock_payload_includes_boot_scope() -> None:
    with (
        patch("orca_auto.core.utils.process_tracking.os.getpid", return_value=4321),
        patch(
            "orca_auto.core.utils.process_tracking.now_utc_iso",
            return_value="2026-03-22T00:00:00+00:00",
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.current_process_start_ticks",
            return_value=111,
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.current_boot_id",
            return_value="boot-a",
        ),
    ):
        payload = process_tracking.current_process_lock_payload()

    assert payload == {
        "pid": 4321,
        "started_at": "2026-03-22T00:00:00+00:00",
        "process_start_ticks": 111,
        "boot_id": "boot-a",
    }


def test_active_run_lock_rejects_cross_boot_pid_before_numeric_probe(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    with (
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.parse_lock_info",
            return_value={
                "pid": 123,
                "process_start_ticks": 111,
                "boot_id": "boot-a",
            },
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.current_boot_id",
            return_value="boot-b",
        ),
        patch("orca_auto.core.utils.process_tracking.process_lock.is_process_alive") as alive,
    ):
        assert process_tracking.active_run_lock_pid(reaction_dir) is None
    alive.assert_not_called()


def test_active_run_lock_pid_covers_invalid_dead_reuse_and_logger_paths(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()

    with patch(
        "orca_auto.core.utils.process_tracking.process_lock.parse_lock_info",
        return_value={"pid": "bad"},
    ):
        assert process_tracking.active_run_lock_pid(reaction_dir) is None

    with (
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.parse_lock_info",
            return_value={"pid": 123},
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.is_process_alive",
            return_value=False,
        ),
    ):
        assert process_tracking.active_run_lock_pid(reaction_dir) is None

    on_pid_reuse = Mock()
    with (
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.parse_lock_info",
            return_value={"pid": 456, "process_start_ticks": 111},
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.is_process_alive", return_value=True
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.process_start_ticks",
            return_value=None,
        ),
    ):
        assert (
            process_tracking.active_run_lock_pid(
                reaction_dir,
                on_pid_reuse=on_pid_reuse,
            )
            is None
        )
    on_pid_reuse.assert_called_once_with(456, 111, None)

    logger = Mock(spec=logging.Logger)
    with (
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.parse_lock_info",
            return_value={"pid": 789, "process_start_ticks": 111},
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.is_process_alive", return_value=True
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.process_start_ticks",
            return_value=222,
        ),
    ):
        assert process_tracking.active_run_lock_pid(reaction_dir, logger=logger) is None
    logger.info.assert_called_once()

    with (
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.parse_lock_info",
            return_value={"pid": 654},
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.is_process_alive",
            return_value=True,
        ),
    ):
        assert process_tracking.active_run_lock_pid(reaction_dir) == 654


def test_read_pid_file_covers_missing_invalid_dead_unlink_failure_and_live_pid(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.pid"
    assert process_tracking.read_pid_file(missing) is None

    invalid = tmp_path / "invalid.pid"
    invalid.write_text("not-a-pid", encoding="utf-8")
    assert process_tracking.read_pid_file(invalid) is None

    unreadable = tmp_path / "unreadable.pid"
    unreadable.write_text("123", encoding="utf-8")
    with patch("pathlib.Path.read_text", autospec=True, side_effect=OSError("boom")):
        assert process_tracking.read_pid_file(unreadable) is None

    stale = tmp_path / "stale.pid"
    stale.write_text("999", encoding="utf-8")
    with (
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.is_process_alive",
            return_value=False,
        ),
        patch(
            "pathlib.Path.unlink",
            autospec=True,
            side_effect=OSError("boom"),
        ),
    ):
        assert process_tracking.read_pid_file(stale) is None

    live = tmp_path / "live.pid"
    live.write_text("321", encoding="utf-8")
    with patch(
        "orca_auto.core.utils.process_tracking.process_lock.is_process_alive", return_value=True
    ):
        assert process_tracking.read_pid_file(live) == 321

    json_live = tmp_path / "json-live.pid"
    json_live.write_text(json.dumps({"pid": 654, "process_start_ticks": 111}), encoding="utf-8")
    with (
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.is_process_alive", return_value=True
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.process_start_ticks",
            return_value=111,
        ),
    ):
        assert process_tracking.read_pid_file(json_live) == 654

    reused = tmp_path / "reused.pid"
    reused.write_text(json.dumps({"pid": 654, "process_start_ticks": 111}), encoding="utf-8")
    with (
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.is_process_alive", return_value=True
        ),
        patch(
            "orca_auto.core.utils.process_tracking.process_lock.process_start_ticks",
            return_value=222,
        ),
    ):
        assert process_tracking.read_pid_file(reused) is None
    assert not reused.exists()
