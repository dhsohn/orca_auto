from __future__ import annotations

import errno
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orca_auto.orca import result_organizer_filesystem as organizer_fs
from orca_auto.orca import result_organizer_planning as organizer_planning
from orca_auto.orca import result_organizer_state as organizer_state
from orca_auto.orca.result_organizer_models import OrganizePlan
from orca_auto.orca.state import report_json_path, save_state


def _write_state(reaction_dir: Path, state: Mapping[str, object]) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    save_state(reaction_dir, dict(state))


def _write_report(reaction_dir: Path, payload: object) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    report_json_path(reaction_dir).write_text(
        json.dumps(payload, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


def _plan(source_dir: Path, target_dir: Path) -> OrganizePlan:
    return OrganizePlan(
        reaction_dir=source_dir,
        run_id="run_test",
        job_type="opt",
        molecule_key="H2",
        selected_inp=str(source_dir / "calc.inp"),
        last_out_path=str(source_dir / "calc.out"),
        attempt_count=1,
        status="completed",
        analyzer_status="completed",
        reason="normal_termination",
        completed_at="2026-03-22T00:00:00+00:00",
        source_dir=source_dir,
        target_rel_path="opt/H2/run_test",
        target_abs_path=target_dir,
    )


def test_route_and_attempt_helpers_cover_edge_cases(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"

    inp_path = reaction_dir / "calc.inp"
    reaction_dir.mkdir()
    inp_path.write_text("\n# comment\n! Opt TightSCF\n", encoding="utf-8")
    assert organizer_planning._read_route_line(inp_path) == "! Opt TightSCF"
    assert organizer_planning._read_route_line(reaction_dir / "missing.inp") == ""

    assert organizer_planning._attempt_is_successful({"analyzer_status": "completed"}) is True
    assert organizer_planning._attempt_is_successful({"return_code": 0}) is True
    assert organizer_planning._attempt_is_successful({"return_code": 1}) is False


def test_metadata_selection_and_eligibility_cover_missing_artifacts_and_attempt_fallback(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()

    state: dict[str, object] = {
        "run_id": "run_test",
        "status": "completed",
        "selected_inp": str(reaction_dir / "missing.inp"),
        "final_result": {"status": "completed"},
    }
    _write_state(reaction_dir, state)
    loaded, skip = organizer_planning.check_eligibility(reaction_dir)
    assert loaded is None
    assert skip is not None
    assert skip.reason == "artifact_missing"

    selected_inp = reaction_dir / "selected.inp"
    selected_inp.write_text("! Opt\n* xyzfile 0 1 missing.xyz\n", encoding="utf-8")
    retry_inp = reaction_dir / "retry.inp"
    retry_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    retry_out = reaction_dir / "retry.out"
    retry_out.write_text("done\n", encoding="utf-8")

    fallback_state: dict[str, object] = {
        "selected_inp": str(selected_inp),
        "attempts": [
            {
                "inp_path": str(retry_inp),
                "out_path": str(retry_out),
                "return_code": 0,
            }
        ],
        "final_result": {"last_out_path": str(retry_out)},
    }
    with patch(
        "orca_auto.orca.result_organizer_planning.resolve_molecule_key",
        side_effect=[
            SimpleNamespace(source="directory_fallback", key="selected"),
            SimpleNamespace(source="parsed_input", key="retry"),
        ],
    ):
        assert (
            organizer_planning.select_organize_metadata_inp_path(fallback_state, reaction_dir)
            == retry_inp.resolve()
        )

    assert organizer_planning.resolve_organize_metadata({"selected_inp": ""}, reaction_dir) == (
        None,
        "other",
        "unknown",
    )


def test_plan_root_scan_handles_scan_errors_and_skips_special_dirs(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    organized = tmp_path / "outputs"
    root.mkdir()
    organized.mkdir()

    with patch("pathlib.Path.rglob", side_effect=OSError("boom")):
        plans, skips = organizer_planning.plan_root_scan(root, organized)
    assert plans == []
    assert skips == []

    good_dir = root / "good"
    symlink_dir = root / "symlinked"
    organized_dir = organized / "inside"
    good_state = good_dir / "job_state.json"
    good_report = good_dir / "job_report.json"
    symlink_report = symlink_dir / "job_report.json"
    organized_state = organized_dir / "job_state.json"
    plan = _plan(good_dir, organized / "opt" / "H2" / "run_test")

    with (
        patch(
            "pathlib.Path.rglob",
            side_effect=[
                [good_state, organized_state],
                [good_report, symlink_report],
            ],
        ),
        patch(
            "pathlib.Path.is_symlink", autospec=True, side_effect=lambda path: path == symlink_dir
        ),
        patch(
            "orca_auto.orca.result_organizer_planning.plan_single",
            return_value=(plan, None),
        ) as plan_single,
    ):
        plans, skips = organizer_planning.plan_root_scan(root, organized)

    assert plans == [plan]
    assert skips == []
    plan_single.assert_called_once_with(good_dir, organized)


def test_move_helpers_cover_copytree_execute_and_rollback_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "calc.out").write_text("content", encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing"):
        organizer_fs._verify_copytree(source, target)

    target.mkdir()
    (target / "calc.out").write_text("mismatch", encoding="utf-8")
    with pytest.raises(RuntimeError, match="size mismatch"):
        organizer_fs._verify_copytree(source, target)

    cross_source = tmp_path / "cross_source"
    cross_target = tmp_path / "cross_target"
    cross_source.mkdir()
    (cross_source / "nested").mkdir()
    (cross_source / "nested" / "calc.out").write_text("done", encoding="utf-8")
    with patch("orca_auto.orca.result_organizer_filesystem._fsync_directory"):
        organizer_fs._cross_device_move(cross_source, cross_target)
    assert not cross_source.exists()
    assert (cross_target / "nested" / "calc.out").read_text(encoding="utf-8") == "done"

    plan = _plan(tmp_path / "move_source", tmp_path / "move_target")
    plan.source_dir.mkdir()
    with (
        patch(
            "orca_auto.orca.result_organizer_filesystem.os.rename",
            side_effect=OSError(errno.EXDEV, "cross-device"),
        ),
        patch(
            "orca_auto.orca.result_organizer_filesystem._cross_device_move",
        ) as cross_move,
    ):
        organizer_fs.execute_move(plan)
    cross_move.assert_called_once_with(plan.source_dir, plan.target_abs_path)

    with patch(
        "orca_auto.orca.result_organizer_filesystem.os.rename",
        side_effect=OSError(errno.EPERM, "nope"),
    ):
        with pytest.raises(OSError):
            organizer_fs.execute_move(plan)

    rollback_plan = _plan(tmp_path / "rollback_source", tmp_path / "rollback_target")
    organizer_fs.rollback_move(rollback_plan)

    rollback_plan.source_dir.mkdir(parents=True)
    rollback_plan.target_abs_path.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="Rollback blocked"):
        organizer_fs.rollback_move(rollback_plan)

    shutil_source = tmp_path / "rollback_source_ok"
    shutil_target = tmp_path / "rollback_target_ok"
    shutil_target.mkdir()
    rollback_plan = _plan(shutil_source, shutil_target)
    with (
        patch(
            "orca_auto.orca.result_organizer_filesystem.os.rename",
            side_effect=OSError(errno.EXDEV, "cross-device"),
        ),
        patch(
            "orca_auto.orca.result_organizer_filesystem._cross_device_move",
        ) as cross_move,
    ):
        organizer_fs.rollback_move(rollback_plan)
    cross_move.assert_called_once_with(shutil_target, shutil_source)


def test_path_normalization_and_state_sync_cover_relocation_branches(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()

    selected_inp = source_dir / "calc.inp"
    attempt_out = source_dir / "calc.out"
    selected_inp.write_text("! Opt\n", encoding="utf-8")
    attempt_out.write_text("out\n", encoding="utf-8")
    moved_inp = target_dir / "calc.inp"
    moved_out = target_dir / "calc.out"
    moved_inp.write_text("! Opt\n", encoding="utf-8")
    moved_out.write_text("out\n", encoding="utf-8")

    _write_state(
        target_dir,
        {
            "run_id": "run_sync",
            "reaction_dir": str(source_dir),
            "selected_inp": str(selected_inp),
            "status": "completed",
            "attempts": [{"inp_path": str(selected_inp), "out_path": str(attempt_out)}],
            "final_result": {"last_out_path": str(attempt_out)},
        },
    )
    _write_report(target_dir, {"run_id": "run_sync"})

    state = organizer_state._sync_state_after_relocation(
        state_dir=target_dir,
        source_dir=source_dir,
        target_dir=target_dir,
    )

    assert state["reaction_dir"] == str(target_dir)
    assert state["selected_inp"] == str(moved_inp.resolve())
    assert state["attempts"][0]["inp_path"] == str(moved_inp.resolve())
    assert state["attempts"][0]["out_path"] == str(moved_out.resolve())
    assert state["final_result"] is not None
    assert state["final_result"]["last_out_path"] == str(moved_out.resolve())

    assert (
        organizer_state._remap_moved_path("relative/file.out", source_dir, target_dir)
        == "relative/file.out"
    )
    assert organizer_state._remap_moved_path(
        str(tmp_path / "outside.out"), source_dir, target_dir
    ) == str(tmp_path / "outside.out")
    assert organizer_state._remap_moved_path(str(attempt_out), source_dir, target_dir) == str(
        target_dir / "calc.out"
    )
    assert organizer_state._normalize_moved_artifact_path(
        str(source_dir / "missing.out"),
        source_dir,
        target_dir,
    ) == str(target_dir / "missing.out")

    organizer_state._normalize_attempt_artifact_paths(
        [{"inp_path": str(selected_inp), "out_path": str(attempt_out)}, "ignored"],
        source_dir=source_dir,
        target_dir=target_dir,
    )
    organizer_state._normalize_attempt_artifact_paths(
        "not-a-list", source_dir=source_dir, target_dir=target_dir
    )
    organizer_state._normalize_final_result_artifact_path(
        {"last_out_path": str(attempt_out)},
        source_dir=source_dir,
        target_dir=target_dir,
    )
    organizer_state._normalize_final_result_artifact_path(
        "not-a-dict",
        source_dir=source_dir,
        target_dir=target_dir,
    )

    with pytest.raises(RuntimeError, match="invalid state"):
        organizer_state._sync_state_after_relocation(
            state_dir=tmp_path / "missing_state",
            source_dir=source_dir,
            target_dir=target_dir,
        )


def test_fsync_directory_closes_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "dir"
    path.mkdir()
    with (
        patch("orca_auto.orca.result_organizer_filesystem.os.open", return_value=7) as open_mock,
        patch(
            "orca_auto.orca.result_organizer_filesystem.os.fsync",
        ) as fsync_mock,
        patch("orca_auto.orca.result_organizer_filesystem.os.close") as close_mock,
    ):
        organizer_fs._fsync_directory(path)

    open_mock.assert_called_once()
    fsync_mock.assert_called_once_with(7)
    close_mock.assert_called_once_with(7)
