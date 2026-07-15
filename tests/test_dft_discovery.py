"""DFT target file discovery tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from orca_auto.core.paths import SMOKE_RESULTS_DIRNAME
from orca_auto.core.queue.engine.input_snapshot import (
    bind_direct_generation_owner,
    require_direct_generation_owner,
)
from orca_auto.orca.dft.discovery import discover_orca_targets
from tests.engine_artifact_helpers import orca_artifact_payload


def _write_orca_state(
    run_dir: Path,
    *,
    status: str,
    reaction_dir: str = "",
    selected_inp: str = "",
    final_result: dict[str, object] | None = None,
    execution_provenance: dict[str, object] | None = None,
) -> None:
    (run_dir / "job_state.json").write_text(
        json.dumps(
            orca_artifact_payload(
                job_id=run_dir.name,
                run_id=run_dir.name,
                reaction_dir=reaction_dir or str(run_dir),
                selected_inp=selected_inp,
                status=status,
                final_result=final_result or {},
                engine_payload_extra=(
                    {"execution_provenance": execution_provenance}
                    if execution_provenance is not None
                    else None
                ),
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _visible_generation_provenance(
    generation_dir: Path,
    selected_inp: Path,
) -> dict[str, object]:
    resolved_generation = generation_dir.resolve(strict=True)
    job_dir = resolved_generation.parent
    job_status = job_dir.stat()
    generation_status = generation_dir.stat()
    selected_payload = selected_inp.read_bytes()
    owner_token = hashlib.sha256(str(resolved_generation).encode()).hexdigest()
    try:
        require_direct_generation_owner(
            job_dir,
            namespace=resolved_generation.name,
            expected_job_identity=(int(job_status.st_dev), int(job_status.st_ino)),
            expected_generation_identity=(
                int(generation_status.st_dev),
                int(generation_status.st_ino),
            ),
            owner_token=owner_token,
        )
    except ValueError:
        bind_direct_generation_owner(
            job_dir,
            namespace=resolved_generation.name,
            expected_job_identity=(int(job_status.st_dev), int(job_status.st_ino)),
            expected_generation_identity=(
                int(generation_status.st_dev),
                int(generation_status.st_ino),
            ),
            owner_token=owner_token,
        )
    return {
        "execution_dir": str(generation_dir),
        "execution_dir_identity": {
            "device": int(generation_status.st_dev),
            "inode": int(generation_status.st_ino),
        },
        "generation_owner_token": owner_token,
        "bound_selected_identity": {
            "path": str(selected_inp.resolve()),
            "sha256": hashlib.sha256(selected_payload).hexdigest(),
            "size_bytes": len(selected_payload),
        },
    }


def test_default_policy_uses_run_state_only(tmp_path: Path) -> None:
    kb_dir = tmp_path / "kb"
    run_dir = kb_dir / "job_report_only"
    run_dir.mkdir(parents=True)

    out_file = run_dir / "final.out"
    out_file.write_text("result", encoding="utf-8")
    (run_dir / "job_report.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "final_result": {
                    "status": "completed",
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)
    assert targets == []


def test_orca_runs_tracks_latest_out_for_running_state(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_runs"
    run_dir = kb_dir / "job_running"
    run_dir.mkdir(parents=True)

    old_out = run_dir / "job.retry01.out"
    new_out = run_dir / "job.retry02.out"
    old_out.write_text("old", encoding="utf-8")
    new_out.write_text("new", encoding="utf-8")
    if new_out.stat().st_mtime <= old_out.stat().st_mtime:
        new_out.touch()

    _write_orca_state(
        run_dir,
        status="running",
        reaction_dir="/home/someone/orca_runs/job_running",
        selected_inp="/home/someone/orca_runs/job_running/job.inp",
    )

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)
    assert [str(t.path) for t in targets] == [str(new_out)]


def test_orca_runs_ignores_reaction_dir_and_uses_state_directory(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_runs"
    run_dir = kb_dir / "job_reaction_dir_ignored"
    run_dir.mkdir(parents=True)

    stale_dir = tmp_path / "host_path" / "job_reaction_dir_ignored"
    stale_dir.mkdir(parents=True)
    (stale_dir / "host_only.out").write_text("host", encoding="utf-8")

    local_out = run_dir / "input.out"
    local_out.write_text("local", encoding="utf-8")

    _write_orca_state(
        run_dir,
        status="running",
        reaction_dir=str(stale_dir),
        selected_inp=str(stale_dir / "input.inp"),
    )

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)
    assert [str(t.path) for t in targets] == [str(local_out)]


def test_public_state_discovers_its_selected_visible_generation_without_indexing_mirror(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "orca_runs"
    run_dir = kb_dir / "job_visible_generation"
    generation_dir = run_dir / "generation-20260715-010203-0123abcd"
    generation_dir.mkdir(parents=True)

    selected_inp = generation_dir / "calc.inp"
    selected_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    stale_root_out = run_dir / "calc.out"
    stale_root_out.write_text("stale root output", encoding="utf-8")
    generation_out = generation_dir / "calc.out"
    generation_out.write_text("current generation output", encoding="utf-8")

    _write_orca_state(
        run_dir,
        status="running",
        selected_inp=str(selected_inp),
        execution_provenance=_visible_generation_provenance(generation_dir, selected_inp),
    )
    # The mirrored state is an artifact of the public job, not another job.
    # Give it a conflicting status so the assertion also proves it was not
    # independently consumed and allowed to overwrite the public state.
    _write_orca_state(
        generation_dir,
        status="failed",
        selected_inp=str(selected_inp),
        execution_provenance=_visible_generation_provenance(generation_dir, selected_inp),
    )

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)

    assert [(target.path, target.run_state_status) for target in targets] == [
        (generation_out, "running")
    ]


def test_visible_generation_deletion_does_not_fall_back_to_stale_root_output(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "orca_runs"
    run_dir = kb_dir / "deleted-generation"
    generation_dir = run_dir / "generation-20260715-010203-0123abcd"
    generation_dir.mkdir(parents=True)
    selected_inp = generation_dir / "calc.inp"
    selected_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    execution_provenance = _visible_generation_provenance(generation_dir, selected_inp)
    stale_root_out = run_dir / "old.out"
    stale_root_out.write_text("stale root output", encoding="utf-8")
    _write_orca_state(
        run_dir,
        status="completed",
        selected_inp=str(selected_inp),
        execution_provenance=execution_provenance,
    )

    selected_inp.unlink()
    generation_dir.rmdir()

    assert discover_orca_targets(kb_dir, max_bytes=1024) == []


def test_visible_generation_aba_replacement_does_not_discover_replacement_or_root(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "orca_runs"
    run_dir = kb_dir / "replaced-generation"
    generation_dir = run_dir / "generation-20260715-010203-0123abcd"
    generation_dir.mkdir(parents=True)
    selected_inp = generation_dir / "calc.inp"
    selected_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    execution_provenance = _visible_generation_provenance(generation_dir, selected_inp)
    _write_orca_state(
        run_dir,
        status="completed",
        selected_inp=str(selected_inp),
        execution_provenance=execution_provenance,
    )
    (run_dir / "old.out").write_text("stale root output", encoding="utf-8")

    moved_generation = run_dir / "moved-original-generation"
    generation_dir.rename(moved_generation)
    generation_dir.mkdir()
    selected_inp.write_text("! SP\n* xyz 0 1\nHe 0 0 0\n*\n", encoding="utf-8")
    (generation_dir / "calc.out").write_text("replacement output", encoding="utf-8")
    # Simulate filesystem ABA even when the recreated directory receives the
    # same identity: let the directory identity match the replacement while
    # retaining the original bound input digest in provenance.
    replacement_status = generation_dir.stat()
    execution_provenance["execution_dir_identity"] = {
        "device": int(replacement_status.st_dev),
        "inode": int(replacement_status.st_ino),
    }
    _write_orca_state(
        run_dir,
        status="completed",
        selected_inp=str(selected_inp),
        execution_provenance=execution_provenance,
    )

    assert discover_orca_targets(kb_dir, max_bytes=1024) == []


def test_visible_generation_discovery_rejects_symlink_escapes(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_runs"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_inp = outside / "calc.inp"
    outside_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    outside_out = outside / "calc.out"
    outside_out.write_text("outside", encoding="utf-8")

    linked_generation_job = kb_dir / "linked-generation"
    linked_generation_job.mkdir(parents=True)
    linked_generation = linked_generation_job / "generation-20260715-010203-0123abcd"
    linked_generation.symlink_to(outside, target_is_directory=True)
    (linked_generation_job / "old.out").write_text("stale root", encoding="utf-8")
    _write_orca_state(
        linked_generation_job,
        status="completed",
        selected_inp=str(linked_generation / "calc.inp"),
        execution_provenance=_visible_generation_provenance(
            linked_generation,
            linked_generation / "calc.inp",
        ),
    )

    linked_input_job = kb_dir / "linked-input"
    linked_input_generation = linked_input_job / "generation-20260715-010204-0123abcd"
    linked_input_generation.mkdir(parents=True)
    linked_input = linked_input_generation / "calc.inp"
    linked_input.symlink_to(outside_inp)
    (linked_input_generation / "calc.out").write_text("local", encoding="utf-8")
    (linked_input_job / "old.out").write_text("stale root", encoding="utf-8")
    _write_orca_state(
        linked_input_job,
        status="completed",
        selected_inp=str(linked_input),
        execution_provenance=_visible_generation_provenance(
            linked_input_generation,
            linked_input,
        ),
    )

    linked_output_job = kb_dir / "linked-output"
    linked_output_generation = linked_output_job / "generation-20260715-010205-0123abcd"
    linked_output_generation.mkdir(parents=True)
    linked_output_inp = linked_output_generation / "calc.inp"
    linked_output_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    (linked_output_generation / "calc.out").symlink_to(outside_out)
    (linked_output_job / "old.out").write_text("stale root", encoding="utf-8")
    _write_orca_state(
        linked_output_job,
        status="completed",
        selected_inp=str(linked_output_inp),
        execution_provenance=_visible_generation_provenance(
            linked_output_generation,
            linked_output_inp,
        ),
    )

    assert discover_orca_targets(kb_dir, max_bytes=1024) == []


def test_visible_generation_discovery_preserves_a_safe_runs_root_symlink(
    tmp_path: Path,
) -> None:
    physical_runs = tmp_path / "physical-runs"
    linked_runs = tmp_path / "linked-runs"
    run_dir = physical_runs / "job"
    generation_dir = run_dir / "generation-20260715-010203-0123abcd"
    generation_dir.mkdir(parents=True)
    linked_runs.symlink_to(physical_runs, target_is_directory=True)

    selected_inp = generation_dir / "calc.inp"
    selected_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    generation_out = generation_dir / "calc.out"
    generation_out.write_text("output", encoding="utf-8")
    _write_orca_state(
        run_dir,
        status="completed",
        selected_inp=str(selected_inp),
        execution_provenance=_visible_generation_provenance(generation_dir, selected_inp),
    )

    targets = discover_orca_targets(linked_runs, max_bytes=1024)

    assert [(target.path, target.run_state_status) for target in targets] == [
        (
            linked_runs / "job" / generation_dir.name / generation_out.name,
            "completed",
        )
    ]


def test_visible_generation_exception_requires_an_exact_direct_child(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_runs"

    sibling_owner = kb_dir / "sibling-owner"
    sibling_generation = sibling_owner / "generation-20260715-010203-0123abcd"
    sibling_generation.mkdir(parents=True)
    sibling_inp = sibling_generation / "calc.inp"
    sibling_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    (sibling_generation / "calc.out").write_text("sibling", encoding="utf-8")
    sibling_referrer = kb_dir / "sibling-referrer"
    sibling_referrer.mkdir()
    (sibling_referrer / "old.out").write_text("stale root", encoding="utf-8")
    _write_orca_state(
        sibling_referrer,
        status="completed",
        selected_inp=str(sibling_inp),
        execution_provenance=_visible_generation_provenance(sibling_generation, sibling_inp),
    )

    nested_job = kb_dir / "nested"
    nested_generation = nested_job / "archive" / "generation-20260715-010204-0123abcd"
    nested_generation.mkdir(parents=True)
    nested_inp = nested_generation / "calc.inp"
    nested_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    (nested_generation / "calc.out").write_text("nested", encoding="utf-8")
    (nested_job / "old.out").write_text("stale root", encoding="utf-8")
    _write_orca_state(
        nested_job,
        status="completed",
        selected_inp=str(nested_inp),
        execution_provenance=_visible_generation_provenance(nested_generation, nested_inp),
    )

    malformed_job = kb_dir / "malformed"
    malformed_generation = malformed_job / "generation-20260715-010205-0123ABCd"
    malformed_generation.mkdir(parents=True)
    malformed_inp = malformed_generation / "calc.inp"
    malformed_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    (malformed_generation / "calc.out").write_text("malformed", encoding="utf-8")
    (malformed_job / "old.out").write_text("stale root", encoding="utf-8")
    _write_orca_state(
        malformed_job,
        status="completed",
        selected_inp=str(malformed_inp),
        execution_provenance=_visible_generation_provenance(
            malformed_generation,
            malformed_inp,
        ),
    )

    assert discover_orca_targets(kb_dir, max_bytes=1024) == []


def test_legacy_hidden_execution_state_remains_discoverable(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_runs"
    execution_dir = (
        kb_dir
        / "legacy-job"
        / ".orca_auto_orca_executions"
        / "generation-0123456789abcdef0123456789abcdef"
    )
    execution_dir.mkdir(parents=True)
    legacy_out = execution_dir / "calc.out"
    legacy_out.write_text("legacy output", encoding="utf-8")
    _write_orca_state(execution_dir, status="completed")

    assert [target.path for target in discover_orca_targets(kb_dir, max_bytes=1024)] == [legacy_out]


def test_orca_runs_tracks_latest_out_for_failed_state(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_runs"
    run_dir = kb_dir / "job_failed"
    run_dir.mkdir(parents=True)

    old_out = run_dir / "job.retry01.out"
    new_out = run_dir / "job.retry02.out"
    old_out.write_text("old", encoding="utf-8")
    new_out.write_text("new", encoding="utf-8")
    if new_out.stat().st_mtime <= old_out.stat().st_mtime:
        new_out.touch()

    _write_orca_state(run_dir, status="failed")

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)
    assert [str(t.path) for t in targets] == [str(new_out)]


def test_orca_runs_ignores_report_only_directory(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_runs"
    run_dir = kb_dir / "report_only"
    run_dir.mkdir(parents=True)

    (run_dir / "result.out").write_text("x", encoding="utf-8")
    (run_dir / "job_report.json").write_text(
        json.dumps({"status": "completed", "final_result": {"status": "completed"}}),
        encoding="utf-8",
    )

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)
    assert targets == []


def test_workflow_workspace_jobs_are_excluded(tmp_path: Path) -> None:
    kb_dir = tmp_path / "runs"
    standalone = kb_dir / "rxn_standalone"
    standalone.mkdir(parents=True)
    (standalone / "calc.out").write_text("standalone", encoding="utf-8")
    _write_orca_state(standalone, status="completed", final_result={"status": "completed"})

    workspace = kb_dir / "wf_20260704"
    stage_job = workspace / "03_orca" / "candidate_01"
    stage_job.mkdir(parents=True)
    (workspace / "workflow.json").write_text("{}", encoding="utf-8")
    (stage_job / "calc.out").write_text("workflow-internal", encoding="utf-8")
    _write_orca_state(stage_job, status="completed", final_result={"status": "completed"})

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)

    assert [str(t.path) for t in targets] == [str(standalone / "calc.out")]


def test_smoke_tree_is_excluded_from_production_dft_discovery_but_not_case_root(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "runs"
    standalone = kb_dir / "standalone"
    case_runs_root = kb_dir / SMOKE_RESULTS_DIRNAME / "batch" / "case" / "runtime" / "runs"
    smoke_job = case_runs_root / "smoke-job"

    for run_dir, output in ((standalone, "production"), (smoke_job, "smoke")):
        run_dir.mkdir(parents=True)
        (run_dir / "calc.out").write_text(output, encoding="utf-8")
        _write_orca_state(run_dir, status="completed")

    assert [target.path for target in discover_orca_targets(kb_dir, max_bytes=1024)] == [
        standalone / "calc.out"
    ]
    assert [target.path for target in discover_orca_targets(case_runs_root, max_bytes=1024)] == [
        smoke_job / "calc.out"
    ]


def test_dft_discovery_skips_state_and_output_symlinks_that_escape_runs_root(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "runs"
    outside = tmp_path / "outside"
    linked_state_job = kb_dir / "linked-state"
    linked_output_job = kb_dir / "linked-output"
    outside.mkdir()
    linked_state_job.mkdir(parents=True)
    linked_output_job.mkdir()

    outside_state = outside / "job_state.json"
    outside_state.write_text(
        json.dumps(
            orca_artifact_payload(
                job_id="outside",
                run_id="outside",
                reaction_dir=str(outside),
                status="completed",
            )
        ),
        encoding="utf-8",
    )
    (linked_state_job / "job_state.json").symlink_to(outside_state)
    (linked_state_job / "calc.out").write_text("local", encoding="utf-8")

    _write_orca_state(linked_output_job, status="completed")
    outside_output = outside / "outside.out"
    outside_output.write_text("outside", encoding="utf-8")
    (linked_output_job / "calc.out").symlink_to(outside_output)

    assert discover_orca_targets(kb_dir, max_bytes=1024) == []
