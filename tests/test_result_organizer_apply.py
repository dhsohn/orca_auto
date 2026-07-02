from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import patch

import pytest

from orca_auto.orca.result_organizer import filesystem as organizer_fs
from orca_auto.orca.result_organizer import planning as organizer_planning
from orca_auto.orca.result_organizer import state as organizer_state
from orca_auto.orca.result_organizer.models import OrganizePlan
from orca_auto.orca.state import save_state


def _plan(tmp_path: Path) -> OrganizePlan:
    source_dir = tmp_path / "runs" / "run_1"
    target_abs_path = tmp_path / "organized" / "opt" / "H2" / "run_1"
    source_dir.mkdir(parents=True, exist_ok=True)
    return OrganizePlan(
        reaction_dir=source_dir,
        run_id="run_1",
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
        target_rel_path="opt/H2/run_1",
        target_abs_path=target_abs_path,
    )


def _write_state(dir_path: Path, payload: dict[str, object]) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    save_state(dir_path, payload)


def test_attempt_and_metadata_helpers_cover_edge_cases(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "selected.inp"
    selected_inp.write_text("! Opt\n* xyzfile 0 1 missing.xyz\n", encoding="utf-8")
    retry_inp = reaction_dir / "retry.inp"
    retry_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 1\n*\n", encoding="utf-8")
    retry_out = reaction_dir / "retry.out"
    retry_out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")

    assert organizer_planning._attempt_is_successful({"analyzer_status": "completed"}) is True
    assert organizer_planning._attempt_is_successful({"return_code": 0}) is True
    assert organizer_planning._attempt_is_successful({"return_code": 3}) is False

    state = {
        "selected_inp": str(selected_inp),
        "attempts": [
            {"inp_path": "", "out_path": str(retry_out), "return_code": 1},
            {"inp_path": str(retry_inp), "out_path": str(retry_out), "return_code": 0},
        ],
        "final_result": {"last_out_path": str(retry_out)},
    }

    with patch("orca_auto.orca.result_organizer.planning.resolve_molecule_key") as resolve_key:
        resolve_key.side_effect = [
            type("Resolution", (), {"source": "directory_fallback", "key": "unknown"})(),
            type("Resolution", (), {"source": "input_file", "key": "H2"})(),
        ]
        assert (
            organizer_planning._last_successful_attempt_inp_path(state, reaction_dir)
            == retry_inp.resolve()
        )
        assert (
            organizer_planning.select_organize_metadata_inp_path(state, reaction_dir)
            == retry_inp.resolve()
        )

    assert organizer_planning.resolve_organize_metadata({"selected_inp": ""}, reaction_dir) == (
        None,
        "other",
        "unknown",
    )


def test_plan_root_scan_handles_rglob_failure(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    organized_root = tmp_path / "organized"
    root.mkdir()
    organized_root.mkdir()

    def _bad_rglob(_self: Path, _pattern: str):
        raise OSError("scan failed")

    with patch.object(Path, "rglob", autospec=True, side_effect=_bad_rglob):
        plans, skips = organizer_planning.plan_root_scan(root, organized_root)

    assert plans == []
    assert skips == []


def test_verify_copytree_raises_for_missing_and_size_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    src_file = source / "calc.out"
    src_file.write_text("12345", encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing"):
        organizer_fs._verify_copytree(source, target)

    dst_file = target / "calc.out"
    dst_file.write_text("12", encoding="utf-8")
    with pytest.raises(RuntimeError, match="size mismatch"):
        organizer_fs._verify_copytree(source, target)


def test_execute_move_and_rollback_cover_cross_device_and_existing_source(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    with (
        patch(
            "orca_auto.orca.result_organizer.filesystem.os.rename",
            side_effect=OSError(errno.EXDEV, "cross-device"),
        ),
        patch("orca_auto.orca.result_organizer.filesystem._cross_device_move") as cross_device_move,
    ):
        organizer_fs.execute_move(plan)

    cross_device_move.assert_called_once_with(plan.source_dir, plan.target_abs_path)

    source_dir = plan.source_dir
    if source_dir.exists():
        source_dir.rmdir()
    plan.target_abs_path.mkdir(parents=True, exist_ok=True)
    with (
        patch(
            "orca_auto.orca.result_organizer.filesystem.os.rename",
            side_effect=OSError(errno.EXDEV, "cross-device"),
        ),
        patch("orca_auto.orca.result_organizer.filesystem._cross_device_move") as cross_device_move,
    ):
        organizer_fs.rollback_move(plan)

    cross_device_move.assert_called_once_with(plan.target_abs_path, plan.source_dir)

    plan.target_abs_path.mkdir(parents=True, exist_ok=True)
    plan.source_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeError, match="Rollback blocked"):
        organizer_fs.rollback_move(plan)


def test_sync_state_after_relocation_updates_paths_and_raises_for_invalid_state(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "runs" / "run_1"
    target_dir = tmp_path / "organized" / "run_1"
    source_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    target_inp = target_dir / "calc.inp"
    target_out = target_dir / "calc.out"
    target_inp.write_text("! Opt\n", encoding="utf-8")
    target_out.write_text("done\n", encoding="utf-8")

    state_payload = {
        "run_id": "run_1",
        "reaction_dir": str(source_dir),
        "status": "completed",
        "started_at": "2026-03-22T00:00:00+00:00",
        "updated_at": "2026-03-22T00:10:00+00:00",
        "max_retries": 2,
        "selected_inp": str(source_dir / "calc.inp"),
        "attempts": [
            {
                "index": 1,
                "inp_path": str(source_dir / "calc.inp"),
                "out_path": str(source_dir / "calc.out"),
                "return_code": 0,
                "analyzer_status": "completed",
            },
            {
                "index": 2,
                "inp_path": str(source_dir / "retry.inp"),
                "out_path": str(source_dir / "retry.out"),
                "return_code": 1,
                "analyzer_status": "failed",
            },
        ],
        "final_result": {
            "status": "completed",
            "last_out_path": str(source_dir / "calc.out"),
        },
    }
    _write_state(target_dir, state_payload)

    synced = organizer_state._sync_state_after_relocation(
        state_dir=target_dir,
        source_dir=source_dir,
        target_dir=target_dir,
    )

    assert synced["reaction_dir"] == str(target_dir)
    assert synced["selected_inp"] == str(target_inp.resolve())
    assert synced["attempts"][0]["inp_path"] == str(target_inp.resolve())
    assert synced["attempts"][0]["out_path"] == str(target_out.resolve())
    assert synced["final_result"] is not None
    assert synced["final_result"]["last_out_path"] == str(target_out.resolve())

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(RuntimeError, match="invalid state"):
        organizer_state._sync_state_after_relocation(
            state_dir=empty_dir,
            source_dir=source_dir,
            target_dir=target_dir,
        )


def test_moved_path_helpers_and_fsync_directory_cover_noop_and_success(tmp_path: Path) -> None:
    source_dir = tmp_path / "runs" / "run_1"
    target_dir = tmp_path / "organized" / "run_1"
    source_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    target_inp = target_dir / "calc.inp"
    target_inp.write_text("! Opt\n", encoding="utf-8")

    assert (
        organizer_state._remap_moved_path("relative.out", source_dir, target_dir) == "relative.out"
    )
    assert organizer_state._remap_moved_path(
        str(tmp_path / "other" / "calc.out"), source_dir, target_dir
    ) == str(tmp_path / "other" / "calc.out")
    assert organizer_state._normalize_moved_artifact_path(
        str(source_dir / "calc.inp"),
        source_dir,
        target_dir,
    ) == str(target_inp.resolve())
    assert organizer_state._normalize_moved_artifact_path(
        str(source_dir / "missing.inp"),
        source_dir,
        target_dir,
    ) == str(target_dir / "missing.inp")

    with (
        patch("orca_auto.orca.result_organizer.filesystem.os.open", return_value=11) as os_open,
        patch("orca_auto.orca.result_organizer.filesystem.os.fsync") as fsync,
        patch("orca_auto.orca.result_organizer.filesystem.os.close") as close,
    ):
        organizer_fs._fsync_directory(target_dir)

    os_open.assert_called_once()
    fsync.assert_called_once_with(11)
    close.assert_called_once_with(11)
