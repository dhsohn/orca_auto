from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orca_auto.orca.dft.discovery import DiscoveredTarget
from orca_auto.orca.dft.monitor import (
    DFTMonitor,
    _canonical_path_key,
    _file_signature,
    _load_signature,
    _load_state,
    _same_signature,
    _save_state,
    _short_path,
)
from orca_auto.orca.parser import OrcaResult


def _write_output(tmp_path: Path, relative_path: str = "calc.out") -> Path:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ORCA\n", encoding="utf-8")
    return path


def _signature(path: Path, state: str = "") -> tuple[float, int | None, str]:
    stat_result = path.stat()
    return (float(stat_result.st_mtime), int(stat_result.st_size), state)


def _parsed_result(
    path: Path,
    *,
    status: str = "completed",
    opt_converged: bool | None = None,
    has_imaginary_freq: bool | None = None,
) -> OrcaResult:
    return OrcaResult(
        source_path=str(path),
        calc_type="opt",
        method="B3LYP",
        basis_set="def2-SVP",
        formula="CH4",
        energy_hartree=-100.123456,
        opt_converged=opt_converged,
        has_imaginary_freq=has_imaginary_freq,
        status=status,
    )


def test_short_path_returns_original_when_already_short() -> None:
    assert _short_path("a/b/c") == "a/b/c"


def test_short_path_keeps_last_three_segments_and_normalizes_backslashes() -> None:
    assert _short_path(r"C:\runs\batch\job\calc.out") == "batch/job/calc.out"


def test_canonical_path_key_falls_back_to_absolute_when_resolve_raises() -> None:
    sample = Path("~/orca_runs/job/calc.out")
    path_type = type(sample.expanduser())

    with patch.object(path_type, "resolve", side_effect=OSError("resolve failed")):
        canonical = _canonical_path_key(sample)

    assert canonical == str(sample.expanduser().absolute())


def test_file_signature_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert _file_signature(tmp_path / "missing.out") is None


def test_same_signature_requires_matching_size() -> None:
    assert _same_signature((10.0, None, ""), (10.0, 999, "")) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (12.5, None),
        ({"mtime": "7.25", "size": None}, None),
        ({"mtime": 7, "size": "bad"}, None),
        ({"mtime": 7, "size": 10, "state": "FAILED"}, (7.0, 10, "failed")),
        ({"mtime": []}, None),
        ({"mtime": "bad"}, None),
        (["not", "a", "mapping"], None),
    ],
)
def test_load_signature_handles_supported_and_malformed_values(
    value: object,
    expected: tuple[float, int | None] | None,
) -> None:
    assert _load_signature(value) == expected


def test_load_state_keeps_latest_signature_for_duplicate_normalized_keys(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "alias_a": {"mtime": 10.0, "size": 100},
                "alias_b": {"mtime": 20.0, "size": 200},
                "bad_payload": {"mtime": []},
            }
        ),
        encoding="utf-8",
    )

    def normalize(path_text: object) -> str:
        if path_text in {"alias_a", "alias_b"}:
            return "/canonical/calc.out"
        return str(path_text)

    with patch("orca_auto.orca.dft.monitor._canonical_path_key", side_effect=normalize):
        state = _load_state(str(state_file))

    assert state == {"/canonical/calc.out": (20.0, 200, "")}


def test_load_state_returns_empty_when_state_file_is_none() -> None:
    assert _load_state(None) == {}


def test_load_state_returns_empty_for_non_mapping_payload(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")

    assert _load_state(str(state_file)) == {}


def test_load_state_skips_non_string_keys_from_loaded_payload(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")

    with patch(
        "orca_auto.orca.dft.monitor.json.load",
        return_value={
            1: {"mtime": 1.0},
            "good": {"mtime": 2.0},
        },
    ):
        state = _load_state(str(state_file))

    assert state == {}


def test_load_state_logs_warning_on_malformed_json(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("{not-json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        state = _load_state(str(state_file))

    assert state == {}
    assert "dft_monitor_state_load_failed" in caplog.text


def test_save_state_returns_early_without_state_file() -> None:
    _save_state(None, {"/tmp/calc.out": (1.0, 2, "")})


def test_save_state_logs_warning_when_write_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state_file = tmp_path / "state.json"

    with patch("orca_auto.orca.dft.monitor.json.dump", side_effect=OSError("disk full")):
        with caplog.at_level(logging.WARNING):
            _save_state(str(state_file), {"/tmp/calc.out": (1.0, 2, "")})

    assert "dft_monitor_state_save_failed" in caplog.text


def test_scan_warns_for_missing_kb_dir(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monitor = DFTMonitor(MagicMock(), [str(tmp_path / "missing-kb")])
    monitor._baseline_seeded = True

    with caplog.at_level(logging.WARNING):
        report = monitor.scan()

    assert report.scanned_files == 0
    assert report.new_results == []
    assert "dft_monitor_kb_dir_missing" in caplog.text


def test_scan_records_parse_failure_for_changed_target(tmp_path: Path) -> None:
    out_file = _write_output(tmp_path, "a/b/c/calc.out")
    index = MagicMock()
    monitor = DFTMonitor(index, [str(tmp_path)])
    monitor._baseline_seeded = True

    with patch(
        "orca_auto.orca.dft.monitor.discover_orca_targets",
        return_value=[
            DiscoveredTarget(path=out_file, run_state_status="completed"),
        ],
    ):
        with patch("orca_auto.orca.dft.monitor.parse_orca_output", side_effect=ValueError("boom")):
            report = monitor.scan()

    assert len(report.failures) == 1
    assert report.failures[0].error == "boom"
    assert report.failures[0].error_type == "ValueError"
    assert report.failures[0].path == _short_path(_canonical_path_key(out_file))
    index.upsert_single.assert_not_called()


def test_scan_skips_targets_with_missing_file_signature(tmp_path: Path) -> None:
    missing_file = tmp_path / "missing.out"
    index = MagicMock()
    monitor = DFTMonitor(index, [str(tmp_path)])
    monitor._baseline_seeded = True

    with patch(
        "orca_auto.orca.dft.monitor.discover_orca_targets",
        return_value=[
            DiscoveredTarget(path=missing_file, run_state_status="completed"),
        ],
    ):
        with patch("orca_auto.orca.dft.monitor.parse_orca_output") as parse_mock:
            report = monitor.scan()

    assert report.scanned_files == 0
    assert report.new_results == []
    assert report.failures == []
    parse_mock.assert_not_called()
    index.upsert_single.assert_not_called()


def test_scan_suppresses_duplicate_canonical_target_after_parse_failure(tmp_path: Path) -> None:
    out_file = _write_output(tmp_path, "dupe/calc.out")
    index = MagicMock()
    monitor = DFTMonitor(index, [str(tmp_path)])
    monitor._baseline_seeded = True

    with patch(
        "orca_auto.orca.dft.monitor.discover_orca_targets",
        return_value=[
            DiscoveredTarget(path=out_file, run_state_status="completed"),
            DiscoveredTarget(path=out_file, run_state_status="completed"),
        ],
    ):
        with patch(
            "orca_auto.orca.dft.monitor.parse_orca_output", side_effect=ValueError("boom")
        ) as parse_mock:
            report = monitor.scan()

    assert len(report.failures) == 1
    assert parse_mock.call_count == 1
    index.upsert_single.assert_not_called()


def test_scan_running_result_updates_cache_without_upsert(tmp_path: Path) -> None:
    out_file = _write_output(tmp_path, "running.out")
    index = MagicMock()
    monitor = DFTMonitor(index, [str(tmp_path)])
    monitor._baseline_seeded = True

    with patch(
        "orca_auto.orca.dft.monitor.discover_orca_targets",
        return_value=[
            DiscoveredTarget(path=out_file, run_state_status="running"),
        ],
    ):
        with patch(
            "orca_auto.orca.dft.monitor.parse_orca_output",
            return_value=_parsed_result(
                out_file,
                status="completed",
                opt_converged=False,
                has_imaginary_freq=True,
            ),
        ):
            report = monitor.scan()

    canonical = _canonical_path_key(out_file)
    assert len(report.new_results) == 1
    assert report.new_results[0].status == "running"
    assert report.new_results[0].note == " (NOT CONVERGED, imaginary freq)"
    assert monitor._last_seen[canonical] == _signature(out_file, "running")
    index.upsert_single.assert_not_called()


def test_scan_failed_run_state_overrides_completed_parser_status(tmp_path: Path) -> None:
    out_file = _write_output(tmp_path, "failed/calc.out")
    index = MagicMock()
    index.upsert_single.return_value = True
    monitor = DFTMonitor(index, [str(tmp_path)])
    monitor._baseline_seeded = True

    with patch(
        "orca_auto.orca.dft.monitor.discover_orca_targets",
        return_value=[
            DiscoveredTarget(path=out_file, run_state_status="failed"),
        ],
    ):
        with patch(
            "orca_auto.orca.dft.monitor.parse_orca_output",
            return_value=_parsed_result(
                out_file,
                status="completed",
            ),
        ):
            report = monitor.scan()

    assert len(report.new_results) == 1
    assert report.new_results[0].status == "failed"
    index.upsert_single.assert_called_once_with(str(out_file), status_override="failed")


def test_scan_detects_run_state_only_transition_without_file_change(tmp_path: Path) -> None:
    out_file = _write_output(tmp_path, "transition/calc.out")
    index = MagicMock()
    index.upsert_single.return_value = True
    monitor = DFTMonitor(index, [str(tmp_path)])
    monitor._baseline_seeded = True

    with patch(
        "orca_auto.orca.dft.monitor.discover_orca_targets",
        return_value=[
            DiscoveredTarget(path=out_file, run_state_status="running"),
        ],
    ):
        with patch(
            "orca_auto.orca.dft.monitor.parse_orca_output",
            return_value=_parsed_result(
                out_file,
                status="completed",
            ),
        ):
            running_report = monitor.scan()

    assert len(running_report.new_results) == 1
    assert running_report.new_results[0].status == "running"
    index.upsert_single.assert_not_called()

    with patch(
        "orca_auto.orca.dft.monitor.discover_orca_targets",
        return_value=[
            DiscoveredTarget(path=out_file, run_state_status="failed"),
        ],
    ):
        with patch(
            "orca_auto.orca.dft.monitor.parse_orca_output",
            return_value=_parsed_result(
                out_file,
                status="completed",
            ),
        ):
            failed_report = monitor.scan()

    assert len(failed_report.new_results) == 1
    assert failed_report.new_results[0].status == "failed"
    index.upsert_single.assert_called_once_with(str(out_file), status_override="failed")


def test_scan_seeds_baseline_without_parsing_on_first_run(tmp_path: Path) -> None:
    out_file = _write_output(tmp_path, "baseline/calc.out")
    state_file = tmp_path / "automation" / "state.json"
    index = MagicMock()
    monitor = DFTMonitor(index, [str(tmp_path)], state_file=str(state_file))

    with patch(
        "orca_auto.orca.dft.monitor.discover_orca_targets",
        return_value=[
            DiscoveredTarget(path=out_file, run_state_status="completed"),
        ],
    ):
        with patch("orca_auto.orca.dft.monitor.parse_orca_output") as parse_mock:
            with patch("orca_auto.orca.dft.monitor._save_state") as save_mock:
                report = monitor.scan()

    canonical = _canonical_path_key(out_file)
    assert report.baseline_seeded is True
    assert report.scanned_files == 1
    assert monitor._last_seen[canonical] == _signature(out_file, "completed")
    parse_mock.assert_not_called()
    index.upsert_single.assert_not_called()
    save_mock.assert_called_once_with(str(state_file), monitor._last_seen)


def test_scan_removes_stale_paths_and_saves_state(tmp_path: Path) -> None:
    out_file = _write_output(tmp_path, "live/calc.out")
    live_canonical = _canonical_path_key(out_file)
    stale_canonical = str(tmp_path / "stale" / "old.out")
    state_file = tmp_path / "automation" / "state.json"
    index = MagicMock()
    monitor = DFTMonitor(index, [str(tmp_path)], state_file=str(state_file))
    monitor._baseline_seeded = True
    monitor._last_seen = {
        live_canonical: _signature(out_file, "completed"),
        stale_canonical: (1.0, 1, ""),
    }

    with patch(
        "orca_auto.orca.dft.monitor.discover_orca_targets",
        return_value=[
            DiscoveredTarget(path=out_file, run_state_status="completed"),
        ],
    ):
        with patch("orca_auto.orca.dft.monitor.parse_orca_output") as parse_mock:
            with patch("orca_auto.orca.dft.monitor._save_state") as save_mock:
                report = monitor.scan()

    assert report.scanned_files == 1
    assert stale_canonical not in monitor._last_seen
    parse_mock.assert_not_called()
    save_mock.assert_called_once_with(str(state_file), monitor._last_seen)


def test_scan_skips_completed_result_when_upsert_fails(tmp_path: Path) -> None:
    out_file = _write_output(tmp_path, "failed-upsert/calc.out")
    index = MagicMock()
    index.upsert_single.return_value = False
    monitor = DFTMonitor(index, [str(tmp_path)])
    monitor._baseline_seeded = True

    with patch(
        "orca_auto.orca.dft.monitor.discover_orca_targets",
        return_value=[
            DiscoveredTarget(path=out_file, run_state_status="completed"),
        ],
    ):
        with patch(
            "orca_auto.orca.dft.monitor.parse_orca_output", return_value=_parsed_result(out_file)
        ):
            report = monitor.scan()

    assert report.new_results == []
    assert _canonical_path_key(out_file) not in monitor._last_seen
    index.upsert_single.assert_called_once_with(str(out_file), status_override="completed")
