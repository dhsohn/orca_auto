from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest.mock import Mock, patch

from orca_auto.core.utils.lock import file_lock
from orca_auto.core.utils.process_tracking import (
    current_process_lock_payload,
    run_lock_is_held,
    run_lock_status,
)


def test_current_process_lock_payload_keeps_observability_metadata_only() -> None:
    with (
        patch("orca_auto.core.utils.process_tracking.os.getpid", return_value=4321),
        patch(
            "orca_auto.core.utils.process_tracking.now_utc_iso",
            return_value="2026-03-22T00:00:00+00:00",
        ),
    ):
        payload = current_process_lock_payload()

    assert payload == {
        "pid": 4321,
        "started_at": "2026-03-22T00:00:00+00:00",
    }


def test_unlocked_stale_run_lock_is_not_active(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    (reaction_dir / "run.lock").write_text(
        json.dumps({"pid": os.getpid(), "started_at": "old"}),
        encoding="utf-8",
    )

    assert not run_lock_is_held(reaction_dir)


def test_held_run_lock_reports_owner_metadata(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    payload = json.dumps({"pid": 4321, "started_at": "2026-03-22T00:00:00+00:00"})

    with file_lock(reaction_dir / "run.lock", payload=payload):
        status = run_lock_status(reaction_dir)
        assert status.held
        assert status.pid == 4321
        assert status.started_at == "2026-03-22T00:00:00+00:00"


def test_held_run_lock_with_invalid_payload_still_blocks(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()

    with file_lock(reaction_dir / "run.lock", payload="not-json"):
        status = run_lock_status(reaction_dir)
        assert status.held
        assert status.pid is None


def test_run_lock_inspection_error_fails_closed(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    logger = Mock(spec=logging.Logger)

    with patch(
        "orca_auto.core.utils.process_tracking.held_file_lock_payload",
        side_effect=OSError("busy"),
    ):
        status = run_lock_status(reaction_dir, logger=logger)

    assert status.held
    assert status.pid is None
    logger.warning.assert_called_once()
