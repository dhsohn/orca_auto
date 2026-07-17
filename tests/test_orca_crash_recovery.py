from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    SNAPSHOT_INTENT_TOKEN_KEY,
    transition_snapshot_intent,
)
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.orca import worker_execution as worker_job
from orca_auto.orca.commands.run_inp_submission import _mark_orca_snapshot_owned
from orca_auto.orca.execution_binding import (
    build_orca_execution_snapshot,
    orca_execution_started_evidence,
    verify_orca_execution_snapshot,
)
from orca_auto.orca.queue.adapter import dequeue_next, enqueue, list_queue

_PRISTINE_XYZ = "2\nH2\nH 0 0 0\nH 0 0 0.74\n"
_CRASHED_XYZ = "2\noptimizing\nH 0 0 0\nH 0 0 0.80\n"


def _write_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _mutable_job(tmp_path: Path, job_name: str = "job") -> tuple[Path, Path, Path]:
    job_dir = tmp_path / job_name
    job_dir.mkdir(parents=True, exist_ok=True)
    geometry = job_dir / "h2.xyz"
    geometry.write_text(_PRISTINE_XYZ, encoding="utf-8")
    selected = job_dir / "h2.inp"
    selected.write_text("! HF STO-3G Opt\n* xyzfile 0 1 h2.xyz\n", encoding="utf-8")
    executable = tmp_path / "fake-orca"
    if not executable.exists():
        _write_executable(executable)
    return job_dir, selected, executable


def _build(
    job_dir: Path,
    selected: Path,
    executable: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    return build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz="",
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        max_retries=0,
        orca_executable=executable,
        **kwargs,
    )


def _verify(job_dir: Path, snapshot: dict[str, Any], **kwargs: Any) -> None:
    verify_orca_execution_snapshot(
        job_dir,
        snapshot,
        expected_selected_inp=snapshot["selected_inp"],
        expected_source_selected_inp=snapshot["source_selected_inp"],
        expected_selected_input_xyz="",
        expected_resource_request={"max_cores": 1, "max_memory_gb": 1},
        expected_max_retries=0,
        **kwargs,
    )


def _crash_generation(snapshot: dict[str, Any]) -> Path:
    generation = Path(snapshot["execution_dir"])
    (generation / "h2.out").write_text("interrupted mid-run\n", encoding="utf-8")
    (generation / "h2.xyz").write_text(_CRASHED_XYZ, encoding="utf-8")
    return generation


def test_started_evidence_is_false_for_pristine_generation(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    snapshot = _build(job_dir, selected, executable)
    assert orca_execution_started_evidence(job_dir, snapshot) is False


def test_started_evidence_detects_runtime_outputs(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    snapshot = _build(job_dir, selected, executable)
    (Path(snapshot["execution_dir"]) / "h2.out").write_text("started\n", encoding="utf-8")
    assert orca_execution_started_evidence(job_dir, snapshot) is True


def test_started_evidence_detects_mutated_runtime_geometry(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    snapshot = _build(job_dir, selected, executable)
    (Path(snapshot["execution_dir"]) / "h2.xyz").write_text(_CRASHED_XYZ, encoding="utf-8")
    assert orca_execution_started_evidence(job_dir, snapshot) is True


def test_recovery_build_seeds_frozen_runtime_geometry(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    old_generation = _crash_generation(crashed)

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    assert replacement["generation_name"] != crashed["generation_name"]
    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert "H 0 0 0.80" in bound_text
    assert "H 0 0 0.74" not in bound_text
    recovery = replacement["recovery"]
    assert recovery["previous_generation_name"] == crashed["generation_name"]
    seeded = recovery["seeded_roles"]
    assert set(seeded) == {"dependency_000000"}
    assert seeded["dependency_000000"]["path"] == str(old_generation / "h2.xyz")
    # The replacement is pristine by construction: the strict claim-time
    # verification passes without any runtime-output allowance.
    _verify(job_dir, replacement)
    # The crashed generation stays frozen as that attempt's record.
    assert (old_generation / "h2.out").read_text(encoding="utf-8") == "interrupted mid-run\n"
    assert (old_generation / "h2.xyz").read_text(encoding="utf-8") == _CRASHED_XYZ


def test_recovery_build_falls_back_when_seed_is_truncated(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    generation = Path(crashed["execution_dir"])
    (generation / "h2.out").write_text("interrupted\n", encoding="utf-8")
    (generation / "h2.xyz").write_text("2\ntruncated mid-write\nH 0 0", encoding="utf-8")

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    assert replacement["recovery"]["seeded_roles"] == {}
    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert "H 0 0 0.74" in bound_text
    _verify(job_dir, replacement)


def test_recovery_build_falls_back_when_seed_is_missing(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    generation = Path(crashed["execution_dir"])
    (generation / "h2.out").write_text("interrupted\n", encoding="utf-8")
    (generation / "h2.xyz").unlink()

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    assert replacement["recovery"]["seeded_roles"] == {}
    _verify(job_dir, replacement)


def test_recovery_build_orders_roles_by_stored_source_paths(tmp_path: Path) -> None:
    # A TS-style job with a hessian co-dependency: the recovery seed lives in
    # the previous generation whose timestamp-named path sorts before the
    # job-root hessian, so role order must follow the stored (seed) paths for
    # the claim-time canonical-order verification to hold.
    job_dir = tmp_path / "ts_job"
    job_dir.mkdir()
    (job_dir / "ts.xyz").write_text(_PRISTINE_XYZ, encoding="utf-8")
    (job_dir / "ts.inhess.hess").write_text("$hessian\n1\n1.0\n$end\n", encoding="utf-8")
    selected = job_dir / "ts.inp"
    selected.write_text(
        "\n".join(
            [
                "! HF STO-3G Opt",
                "%geom",
                '  InHessName "ts.inhess.hess"',
                "end",
                "* xyzfile 0 1 ts.xyz",
                "",
            ]
        ),
        encoding="utf-8",
    )
    executable = _write_executable(tmp_path / "ts-orca")
    crashed = _build(job_dir, selected, executable)
    assert crashed["runtime_mutable_input_roles"] == ["dependency_000001"]
    generation = Path(crashed["execution_dir"])
    (generation / "ts.out").write_text("interrupted\n", encoding="utf-8")
    (generation / "ts.xyz").write_text(_CRASHED_XYZ, encoding="utf-8")

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    # The seeded geometry's stored path (inside the previous generation) sorts
    # first, so it now owns the first role.
    assert replacement["runtime_mutable_input_roles"] == ["dependency_000000"]
    assert set(replacement["recovery"]["seeded_roles"]) == {"dependency_000000"}
    assert replacement["dependency_paths"] == sorted(
        replacement["dependency_paths"],
        key=lambda path: Path(path).relative_to(job_dir).as_posix(),
    )
    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert "H 0 0 0.80" in bound_text
    verify_orca_execution_snapshot(
        job_dir,
        replacement,
        expected_selected_inp=replacement["selected_inp"],
        expected_source_selected_inp=replacement["source_selected_inp"],
        expected_selected_input_xyz="",
        expected_resource_request={"max_cores": 1, "max_memory_gb": 1},
        expected_max_retries=0,
    )


def test_recovery_build_rejects_seed_atom_count_change(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    generation = Path(crashed["execution_dir"])
    (generation / "h2.xyz").write_text("1\nsubstituted\nH 0 0 0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="atom count"):
        _build(job_dir, selected, executable, recovery_from=crashed)


def test_recovery_build_rejects_changed_source_input(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    _crash_generation(crashed)
    selected.write_text("! HF STO-3G Opt TightSCF\n* xyzfile 0 1 h2.xyz\n", encoding="utf-8")

    with pytest.raises(ValueError, match="recovery source input changed"):
        _build(job_dir, selected, executable, recovery_from=crashed)


@dataclass(frozen=True)
class _WorkerResources:
    max_cores_per_task: int = 1
    max_memory_gb_per_task: int = 1


def _worker_cfg(queue_root: Path, executable: Path) -> Any:
    return SimpleNamespace(
        runtime=SimpleNamespace(allowed_root=str(queue_root)),
        resources=_WorkerResources(),
        paths=SimpleNamespace(orca_executable=str(executable)),
    )


def _claimed_mutable_entry(tmp_path: Path) -> tuple[Path, Any, dict[str, Any], Path]:
    queue_root = tmp_path / "queue"
    queue_root.mkdir(exist_ok=True)
    job_dir, selected, executable = _mutable_job(queue_root, job_name="rxn")
    snapshot = _build(
        job_dir,
        selected,
        executable,
        queue_root=queue_root,
    )
    # Retire the submission intent the way the enqueue-publication flow does.
    transition_snapshot_intent(
        queue_root,
        snapshot[SNAPSHOT_INTENT_TOKEN_KEY],
        target_state=SNAPSHOT_INTENT_STATE_ENQUEUEING,
        expected_states={SNAPSHOT_INTENT_STATE_CREATING},
    )
    assert _mark_orca_snapshot_owned(queue_root, snapshot[SNAPSHOT_INTENT_TOKEN_KEY]) is None
    metadata = {
        "reaction_dir": str(job_dir),
        "force": True,
        "source_selected_inp": str(selected),
        "selected_inp": snapshot["selected_inp"],
        "selected_input_xyz": "",
        "resource_request": {"max_cores": 1, "max_memory_gb": 1},
        "max_retries": 0,
        "execution_snapshot": snapshot,
    }
    enqueue(queue_root, str(job_dir), force=True, task_id="task-recovery", metadata=metadata)
    running = dequeue_next(queue_root)
    assert running is not None
    return queue_root, running, snapshot, executable


def test_rebind_passes_through_a_pristine_claim(tmp_path: Path) -> None:
    queue_root, running, _snapshot, _executable = _claimed_mutable_entry(tmp_path)

    def unexpected_cfg() -> Any:
        raise AssertionError("a pristine claim must not load worker configuration")

    result = worker_job._maybe_rebind_recovery_generation(
        running,
        queue_root=queue_root,
        cfg_factory=unexpected_cfg,
    )

    assert result is running
    (row,) = list_queue(queue_root)
    assert worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY not in row.metadata


def test_rebind_moves_crashed_claim_into_new_generation(tmp_path: Path) -> None:
    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    old_generation = _crash_generation(snapshot)
    cfg = _worker_cfg(queue_root, executable)

    result = worker_job._maybe_rebind_recovery_generation(
        running,
        queue_root=queue_root,
        cfg_factory=lambda: cfg,
    )

    replacement = result.metadata["execution_snapshot"]
    assert replacement["generation_name"] != snapshot["generation_name"]
    assert is_visible_generation_name(replacement["generation_name"])
    assert result.metadata[worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY] == 1
    assert result.metadata["selected_inp"] == replacement["selected_inp"]
    (row,) = list_queue(queue_root)
    assert row.metadata["execution_snapshot"]["generation_name"] == (replacement["generation_name"])
    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert "H 0 0 0.80" in bound_text
    job_dir = Path(result.metadata["reaction_dir"])
    _verify(job_dir, replacement)
    assert (old_generation / "h2.out").exists()
    assert (old_generation / "h2.xyz").read_text(encoding="utf-8") == _CRASHED_XYZ
    # No orphan snapshot intents survive a completed rebind.
    intent_dir = queue_root / ".orca_auto_snapshot_intents"
    assert not intent_dir.exists() or not any(intent_dir.glob("*.json"))


def test_rebind_honors_a_pending_cancellation(tmp_path: Path) -> None:
    queue_root, running, snapshot, _executable = _claimed_mutable_entry(tmp_path)
    _crash_generation(snapshot)
    from orca_auto.core.queue.types import QueueStatus
    from orca_auto.orca.queue.adapter import cancel

    cancelled = cancel(queue_root, str(running.queue_id))
    assert cancelled is not None and cancelled.cancel_requested

    def unexpected_cfg() -> Any:
        raise AssertionError("a cancelled claim must not load worker configuration")

    result = worker_job._maybe_rebind_recovery_generation(
        running,
        queue_root=queue_root,
        cfg_factory=unexpected_cfg,
    )

    assert result.status is QueueStatus.CANCELLED
    (row,) = list_queue(queue_root)
    assert row.status is QueueStatus.CANCELLED
    assert worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY not in row.metadata
    assert row.metadata["execution_snapshot"]["generation_name"] == snapshot["generation_name"]


def test_rebind_fails_closed_at_the_recovery_limit(tmp_path: Path) -> None:
    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    _crash_generation(snapshot)
    cfg = _worker_cfg(queue_root, executable)
    from orca_auto.orca.queue.adapter import update_metadata

    assert update_metadata(
        queue_root,
        str(running.queue_id),
        {worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY: worker_job.RECOVERY_REBIND_LIMIT},
        expected_entry=running,
    )
    (claimed,) = list_queue(queue_root)

    with pytest.raises(ValueError, match="recovery limit"):
        worker_job._maybe_rebind_recovery_generation(
            claimed,
            queue_root=queue_root,
            cfg_factory=lambda: cfg,
        )

    (row,) = list_queue(queue_root)
    assert row.metadata["execution_snapshot"]["generation_name"] == snapshot["generation_name"]


def test_worker_child_runs_the_replacement_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    _crash_generation(snapshot)
    cfg = _worker_cfg(queue_root, executable)
    cfg.runtime.admission_root = str(tmp_path / "admission")
    calls: dict[str, Any] = {}

    monkeypatch.setattr(worker_job, "load_config", lambda _path: cfg)
    monkeypatch.setattr(worker_job, "install_shutdown_signal_handlers", lambda _cb: None)
    monkeypatch.setattr(worker_job, "release_slot", lambda _root, _token: None)

    def fake_execute_run_job(*args: Any, **kwargs: Any) -> int:
        calls["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(worker_job, "execute_run_job", fake_execute_run_job)

    rc = worker_job.run_worker_child_job(
        config_path="/tmp/config.yaml",
        queue_root=queue_root,
        queue_id=str(running.queue_id),
        admission_token=None,
        await_parent_admission_handoff_fn=lambda *_args: True,
    )

    assert rc == 0
    (row,) = list_queue(queue_root)
    replacement = row.metadata["execution_snapshot"]
    assert replacement["generation_name"] != snapshot["generation_name"]
    # The child executes the replacement generation's bound input, not the
    # crashed generation's.
    assert calls["kwargs"]["selected_inp"] == replacement["selected_inp"]


def test_rebind_consumes_budget_before_building(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    _crash_generation(snapshot)
    cfg = _worker_cfg(queue_root, executable)

    def explode(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated crash during rebuild")

    monkeypatch.setattr(worker_job, "build_orca_execution_snapshot", explode)

    with pytest.raises(RuntimeError, match="simulated crash"):
        worker_job._maybe_rebind_recovery_generation(
            running,
            queue_root=queue_root,
            cfg_factory=lambda: cfg,
        )

    (row,) = list_queue(queue_root)
    assert row.metadata[worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY] == 1
    assert row.metadata["execution_snapshot"]["generation_name"] == snapshot["generation_name"]
