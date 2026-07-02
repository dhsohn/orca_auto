from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from orca_auto.orca import organize_index
from orca_auto.orca.state import save_state, state_path


def _write_state(reaction_dir: Path, state: dict[str, object] | str) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(state, str):
        state_path(reaction_dir).write_text(state, encoding="utf-8")
        return
    save_state(reaction_dir, state)


def test_load_index_returns_empty_on_read_error(tmp_path: Path) -> None:
    records_file = organize_index.records_path(tmp_path)
    records_file.parent.mkdir(parents=True, exist_ok=True)
    records_file.write_text('{"run_id":"run_1"}\n', encoding="utf-8")

    with patch.object(Path, "read_text", side_effect=OSError("boom")):
        assert organize_index.load_index(tmp_path) == {}


def test_load_index_skips_blank_non_mapping_and_invalid_run_ids(tmp_path: Path) -> None:
    records_file = organize_index.records_path(tmp_path)
    records_file.parent.mkdir(parents=True, exist_ok=True)
    records_file.write_text(
        "\n".join(
            [
                "",
                "[]",
                '{"run_id": ""}',
                '{"run_id": 12}',
                '{"run_id": "run_ok", "job_type": "opt"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert organize_index.load_index(tmp_path) == {
        "run_ok": {"run_id": "run_ok", "job_type": "opt"}
    }


def test_to_reaction_relative_path_handles_invalid_and_relative_values(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()

    assert organize_index.to_reaction_relative_path(None, reaction_dir) == ""
    assert organize_index.to_reaction_relative_path("   ", reaction_dir) == ""
    assert organize_index.to_reaction_relative_path("./calc.out", reaction_dir) == "calc.out"
    assert (
        organize_index.to_reaction_relative_path("nested/calc.out", reaction_dir)
        == "nested/calc.out"
    )


def test_to_reaction_relative_path_handles_absolute_paths(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()

    inside = reaction_dir / "calc.out"
    outside = tmp_path / "outside" / "calc.out"

    assert organize_index.to_reaction_relative_path(str(inside), reaction_dir) == "calc.out"
    assert organize_index.to_reaction_relative_path(str(outside), reaction_dir) == ""


def test_rebuild_index_creates_empty_records_for_missing_root(tmp_path: Path) -> None:
    organized_root = tmp_path / "missing_outputs"

    assert organize_index.rebuild_index(organized_root) == 0
    assert organize_index.records_path(organized_root).read_text(encoding="utf-8") == ""


def test_rebuild_index_skips_invalid_and_non_completed_states(tmp_path: Path) -> None:
    organized_root = tmp_path / "outputs"
    organized_root.mkdir()

    _write_state(organized_root / "bad_json", "{not json")
    _write_state(
        organized_root / "running_case",
        {
            "run_id": "run_running",
            "status": "running",
            "final_result": {"reason": "not_done"},
        },
    )
    _write_state(
        organized_root / "missing_final_result",
        {
            "run_id": "run_no_final",
            "status": "completed",
            "final_result": "bad",
        },
    )
    _write_state(
        organized_root / "missing_run_id",
        {
            "status": "completed",
            "final_result": {"reason": "done"},
        },
    )
    _write_state(
        organize_index.index_dir(organized_root) / "skip_me",
        {
            "run_id": "run_in_index",
            "status": "completed",
            "final_result": {"reason": "done"},
        },
    )

    assert organize_index.rebuild_index(organized_root) == 0
    assert organize_index.load_index(organized_root) == {}


def test_rebuild_index_falls_back_to_first_inp_when_metadata_missing(tmp_path: Path) -> None:
    organized_root = tmp_path / "outputs"
    reaction_dir = organized_root / "misc" / "case_01"
    reaction_dir.mkdir(parents=True)
    first_inp = reaction_dir / "a_first.inp"
    second_inp = reaction_dir / "b_second.inp"
    first_inp.write_text("! SP\n", encoding="utf-8")
    second_inp.write_text("! Opt\n", encoding="utf-8")

    _write_state(
        reaction_dir,
        {
            "run_id": "run_fallback",
            "status": "completed",
            "selected_inp": "",
            "attempts": "not-a-list",
            "final_result": {
                "analyzer_status": "completed",
                "reason": "normal_termination",
                "completed_at": "2026-01-01T00:00:00+00:00",
                "last_out_path": "",
            },
        },
    )

    with patch(
        "orca_auto.orca.result_organizer.planning.resolve_organize_metadata",
        return_value=(None, "other", "unknown"),
    ):
        assert organize_index.rebuild_index(organized_root) == 1

    record = organize_index.load_index(organized_root)["run_fallback"]
    assert record["selected_inp"] == "a_first.inp"
    assert record["attempt_count"] == 0
    assert record["organized_path"] == "misc/case_01"


def test_append_failed_rollback_writes_jsonl_and_respects_lock(tmp_path: Path) -> None:
    organized_root = tmp_path / "outputs"
    lock_path = organize_index.index_dir(organized_root) / organize_index.LOCK_FILE_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("{}", encoding="utf-8")

    with patch("orca_auto.orca.organize_index.logger.warning") as warning:
        organize_index.append_failed_rollback(
            organized_root, {"run_id": "run_1", "reason": "rollback_failed"}
        )

    warning.assert_not_called()
    failed_rollbacks = (
        organize_index.index_dir(organized_root) / organize_index.FAILED_ROLLBACKS_FILE_NAME
    )
    lines = failed_rollbacks.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"run_id": "run_1", "reason": "rollback_failed"}
    ]


def test_append_record_warns_without_lock_and_appends_jsonl(tmp_path: Path) -> None:
    organized_root = tmp_path / "outputs"

    with patch("orca_auto.orca.organize_index.logger.warning") as warning:
        organize_index.append_record(organized_root, {"run_id": "run_2"})

    warning.assert_called_once()
    records = organize_index.records_path(organized_root).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in records] == [{"run_id": "run_2"}]


def test_index_lock_timeout_error_mentions_path_and_timeout(tmp_path: Path) -> None:
    error = organize_index._index_lock_timeout_error(tmp_path / "index.lock", 12)
    assert (
        str(error)
        == f"Index lock acquisition timed out after 12s. Lock file: {tmp_path / 'index.lock'}"
    )


def test_acquire_index_lock_passes_expected_payload_and_callbacks(tmp_path: Path) -> None:
    organized_root = tmp_path / "outputs"
    captured: dict[str, object] = {}

    @contextmanager
    def _fake_acquire_file_lock(**kwargs):
        captured.update(kwargs)
        yield

    with (
        patch(
            "orca_auto.orca.organize_index.process_lock.current_process_start_ticks",
            return_value=321,
        ),
        patch(
            "orca_auto.orca.organize_index.process_lock.acquire_file_lock",
            side_effect=_fake_acquire_file_lock,
        ),
    ):
        with organize_index.acquire_index_lock(organized_root, timeout_seconds=7):
            pass

    assert (
        captured["lock_path"]
        == organize_index.index_dir(organized_root) / organize_index.LOCK_FILE_NAME
    )
    assert captured["timeout_seconds"] == 7
    assert captured["timeout_error_builder"] is organize_index._index_lock_timeout_error
    lock_payload = cast(dict[str, Any], captured["lock_payload_obj"])
    assert lock_payload["process_start_ticks"] == 321
    assert captured["parse_lock_info_fn"] is organize_index.process_lock.parse_lock_info
    assert captured["is_process_alive_fn"] is organize_index.process_lock.is_process_alive
    assert captured["process_start_ticks_fn"] is organize_index.process_lock.process_start_ticks


def test_acquire_index_lock_omits_process_ticks_when_unavailable(tmp_path: Path) -> None:
    organized_root = tmp_path / "outputs"
    captured: dict[str, object] = {}

    @contextmanager
    def _fake_acquire_file_lock(**kwargs):
        captured.update(kwargs)
        yield

    with (
        patch(
            "orca_auto.orca.organize_index.process_lock.current_process_start_ticks",
            return_value=None,
        ),
        patch(
            "orca_auto.orca.organize_index.process_lock.acquire_file_lock",
            side_effect=_fake_acquire_file_lock,
        ),
    ):
        with organize_index.acquire_index_lock(organized_root):
            pass

    lock_payload = cast(dict[str, Any], captured["lock_payload_obj"])
    assert "process_start_ticks" not in lock_payload
