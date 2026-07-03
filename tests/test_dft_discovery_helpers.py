"""Helper coverage for DFT target discovery."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
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


def test_discover_runs_targets_cover_non_dict_state_and_missing_outputs(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    runs_non_dict = runs_root / "non_dict"
    runs_non_dict.mkdir()
    (runs_non_dict / "job_state.json").write_text(json.dumps(["bad"]), encoding="utf-8")

    runs_missing = runs_root / "missing_out"
    runs_missing.mkdir()
    (runs_missing / "job_state.json").write_text(
        json.dumps(_state_payload(status="running")), encoding="utf-8"
    )

    assert discovery.discover_orca_targets(runs_root, max_bytes=1024) == []


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
