"""Helper coverage for DFT target discovery."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import orca_auto.orca.dft.discovery as discovery
from tests.engine_artifact_helpers import orca_artifact_payload


def _state_payload(
    *,
    status: str,
    final_result: dict[str, object] | None = None,
) -> dict[str, object]:
    return orca_artifact_payload(
        job_id="job",
        run_id="run",
        reaction_dir="/tmp/job",
        status=status,
        final_result=final_result or {"status": status},
    )


def test_discover_orca_targets_routes_by_path_policy(tmp_path: Path) -> None:
    outputs_root = tmp_path / "Team" / "OrCa_OutPuts"
    runs_root = tmp_path / "kb"
    outputs_root.mkdir(parents=True)
    runs_root.mkdir()

    with (
        patch.object(discovery, "_discover_orca_outputs_targets", return_value=[]) as outputs_mock,
        patch.object(discovery, "_discover_orca_runs_targets", return_value=[]) as runs_mock,
    ):
        assert (
            discovery.discover_orca_targets(
                outputs_root,
                max_bytes=111,
                recent_completed_window_minutes=15,
            )
            == []
        )
        assert (
            discovery.discover_orca_targets(
                runs_root,
                max_bytes=222,
                recent_completed_window_minutes=30,
            )
            == []
        )

    outputs_mock.assert_called_once_with(
        kb_path=outputs_root,
        max_bytes=111,
        recent_completed_window_minutes=15,
    )
    runs_mock.assert_called_once_with(
        kb_path=runs_root,
        max_bytes=222,
    )


def test_discover_helpers_cover_non_dict_state_and_missing_outputs(tmp_path: Path) -> None:
    outputs_root = tmp_path / "orca_outputs"
    outputs_root.mkdir()
    outputs_non_dict = outputs_root / "non_dict"
    outputs_non_dict.mkdir()
    (outputs_non_dict / "job_state.json").write_text(json.dumps(["bad"]), encoding="utf-8")

    outputs_missing = outputs_root / "missing_out"
    outputs_missing.mkdir()
    (outputs_missing / "job_state.json").write_text(
        json.dumps(_state_payload(status="completed")),
        encoding="utf-8",
    )

    assert (
        discovery._discover_orca_outputs_targets(
            kb_path=outputs_root,
            max_bytes=1024,
            recent_completed_window_minutes=60,
        )
        == []
    )

    runs_root = tmp_path / "orca_runs"
    runs_root.mkdir()
    runs_non_dict = runs_root / "non_dict"
    runs_non_dict.mkdir()
    (runs_non_dict / "job_state.json").write_text(json.dumps(["bad"]), encoding="utf-8")

    runs_missing = runs_root / "missing_out"
    runs_missing.mkdir()
    (runs_missing / "job_state.json").write_text(
        json.dumps(_state_payload(status="running")), encoding="utf-8"
    )

    assert (
        discovery._discover_orca_runs_targets(
            kb_path=runs_root,
            max_bytes=1024,
        )
        == []
    )


def test_find_latest_out_in_dir_handles_non_dir_stat_errors_and_latest_selection(
    tmp_path: Path,
) -> None:
    missing_dir = tmp_path / "missing"
    assert discovery._find_latest_out_in_dir(missing_dir) is None

    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    skipped_dir = run_dir / "skip.out"
    skipped_dir.mkdir()
    bad_out = run_dir / "bad.out"
    bad_out.write_text("bad", encoding="utf-8")
    old_out = run_dir / "old.out"
    newest_out = run_dir / "new.out"
    old_out.write_text("old", encoding="utf-8")
    newest_out.write_text("new", encoding="utf-8")

    now = datetime.now(UTC).timestamp()
    os.utime(old_out, (now - 20, now - 20))
    os.utime(newest_out, (now - 5, now - 5))

    original_is_file = Path.is_file
    original_stat = Path.stat

    def _is_file(self: Path) -> bool:
        if self == bad_out:
            return True
        return original_is_file(self)

    def _stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == bad_out:
            raise OSError("boom")
        return original_stat(self, follow_symlinks=follow_symlinks)

    with (
        patch("pathlib.Path.is_file", autospec=True, side_effect=_is_file),
        patch("pathlib.Path.stat", autospec=True, side_effect=_stat),
    ):
        assert discovery._find_latest_out_in_dir(run_dir) == newest_out


def test_recent_completed_output_accepts_none_or_negative_window(tmp_path: Path) -> None:
    out_path = tmp_path / "calc.out"
    out_path.write_text("done", encoding="utf-8")
    now_utc = datetime(2026, 3, 22, 12, 0, tzinfo=UTC)

    assert discovery._is_recent_completed_output(
        data=_state_payload(status="running"),
        output_path=out_path,
        now_utc=now_utc,
        recent_completed_window_minutes=None,
    )
    assert discovery._is_recent_completed_output(
        data=_state_payload(status="failed"),
        output_path=out_path,
        now_utc=now_utc,
        recent_completed_window_minutes=-1,
    )


def test_recent_completed_output_respects_status_completed_at_and_future_skew(
    tmp_path: Path,
) -> None:
    out_path = tmp_path / "calc.out"
    out_path.write_text("done", encoding="utf-8")
    now_utc = datetime(2026, 3, 22, 12, 0, tzinfo=UTC)

    assert not discovery._is_recent_completed_output(
        data=_state_payload(status="running", final_result={"status": "failed"}),
        output_path=out_path,
        now_utc=now_utc,
        recent_completed_window_minutes=30,
    )

    assert discovery._is_recent_completed_output(
        data=_state_payload(
            status="completed",
            final_result={
                "status": "completed",
                "completed_at": (now_utc + timedelta(minutes=5)).isoformat(),
            },
        ),
        output_path=out_path,
        now_utc=now_utc,
        recent_completed_window_minutes=30,
    )

    assert not discovery._is_recent_completed_output(
        data=_state_payload(
            status="completed",
            final_result={
                "status": "completed",
                "completed_at": (now_utc + timedelta(minutes=5, seconds=1)).isoformat(),
            },
        ),
        output_path=out_path,
        now_utc=now_utc,
        recent_completed_window_minutes=30,
    )


def test_recent_completed_output_uses_mtime_fallback_and_handles_stat_error(
    tmp_path: Path,
) -> None:
    out_path = tmp_path / "calc.out"
    out_path.write_text("done", encoding="utf-8")
    now_utc = datetime(2026, 3, 22, 12, 0, tzinfo=UTC)
    mtime = (now_utc - timedelta(minutes=10)).timestamp()
    os.utime(out_path, (mtime, mtime))

    data = _state_payload(
        status="completed",
        final_result={
            "status": "completed",
            "completed_at": "not-a-date",
        },
    )

    assert discovery._is_recent_completed_output(
        data=data,
        output_path=out_path,
        now_utc=now_utc,
        recent_completed_window_minutes=30,
    )

    original_stat = Path.stat

    def _stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == out_path:
            raise OSError("boom")
        return original_stat(self, follow_symlinks=follow_symlinks)

    with patch("pathlib.Path.stat", autospec=True, side_effect=_stat):
        assert not discovery._is_recent_completed_output(
            data=data,
            output_path=out_path,
            now_utc=now_utc,
            recent_completed_window_minutes=30,
        )


def test_parse_iso_datetime_utc_covers_invalid_naive_z_and_offset_inputs() -> None:
    assert discovery._parse_iso_datetime_utc("not-a-date") is None
    assert discovery._parse_iso_datetime_utc("") is None

    assert discovery._parse_iso_datetime_utc("2026-03-22T12:00:00") == datetime(
        2026, 3, 22, 12, 0, tzinfo=UTC
    )
    assert discovery._parse_iso_datetime_utc("2026-03-22T12:00:00Z") == datetime(
        2026, 3, 22, 12, 0, tzinfo=UTC
    )
    assert discovery._parse_iso_datetime_utc("2026-03-22T21:00:00+09:00") == datetime(
        2026, 3, 22, 12, 0, tzinfo=UTC
    )


def test_add_if_valid_target_covers_suffix_size_and_stat_errors(tmp_path: Path) -> None:
    targets: dict[str, discovery.DiscoveredTarget] = {}

    txt_path = tmp_path / "calc.txt"
    txt_path.write_text("skip", encoding="utf-8")
    discovery._add_if_valid_target(resolved=txt_path, max_bytes=1024, targets=targets)
    assert targets == {}

    big_out = tmp_path / "big.out"
    big_out.write_text("too-big", encoding="utf-8")
    discovery._add_if_valid_target(resolved=big_out, max_bytes=1, targets=targets)
    assert targets == {}

    original_stat = Path.stat

    def _stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == big_out:
            raise OSError("boom")
        return original_stat(self, follow_symlinks=follow_symlinks)

    with patch("pathlib.Path.stat", autospec=True, side_effect=_stat):
        discovery._add_if_valid_target(resolved=big_out, max_bytes=1024, targets=targets)
    assert targets == {}


def test_load_report_json_handles_invalid_json_and_non_dict(tmp_path: Path, caplog) -> None:
    report_path = tmp_path / "job_report.json"
    report_path.write_text(json.dumps(["bad"]), encoding="utf-8")
    assert discovery._load_report_json(report_path) is None

    report_path.write_text("{bad json", encoding="utf-8")
    assert discovery._load_report_json(report_path) is None
    assert "dft_job_report_parse_failed" in caplog.text
