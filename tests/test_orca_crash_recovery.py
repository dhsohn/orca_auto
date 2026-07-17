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


def test_recovery_build_seeds_scf_checkpoint(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    old_generation = _crash_generation(crashed)
    (old_generation / "h2.gbw").write_bytes(b"scf-orbitals-v1")

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    checkpoint_role = replacement["recovery"]["checkpoint_role"]
    assert checkpoint_role
    materialized = replacement["materialized_inputs"][checkpoint_role]
    private = Path(str(materialized["path"]))
    assert private.name == "h2.moinp.gbw"
    assert private.read_bytes() == b"scf-orbitals-v1"
    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert "MORead" in bound_text
    assert '%moinp "h2.moinp.gbw"' in bound_text
    # Still a pristine replacement: strict claim-time verification passes.
    _verify(job_dir, replacement)
    # The crashed generation's runtime checkpoint stays frozen.
    assert (old_generation / "h2.gbw").read_bytes() == b"scf-orbitals-v1"


def test_recovery_checkpoint_skipped_when_absent_or_empty(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    _crash_generation(crashed)

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)
    assert replacement["recovery"]["checkpoint_role"] == ""
    assert "MORead" not in Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    _verify(job_dir, replacement)

    (Path(crashed["execution_dir"]) / "h2.gbw").write_bytes(b"")
    empty_case = _build(job_dir, selected, executable, recovery_from=crashed)
    assert empty_case["recovery"]["checkpoint_role"] == ""
    _verify(job_dir, empty_case)


def test_recovery_checkpoint_skipped_when_source_already_moreads(tmp_path: Path) -> None:
    job_dir = tmp_path / "moread_job"
    job_dir.mkdir()
    (job_dir / "h2.xyz").write_text(_PRISTINE_XYZ, encoding="utf-8")
    (job_dir / "guess.gbw").write_bytes(b"user-supplied-orbitals")
    selected = job_dir / "h2.inp"
    selected.write_text(
        '! HF STO-3G Opt MORead\n%moinp "guess.gbw"\n* xyzfile 0 1 h2.xyz\n',
        encoding="utf-8",
    )
    executable = _write_executable(tmp_path / "moread-orca")
    crashed = _build(job_dir, selected, executable)
    generation = Path(crashed["execution_dir"])
    (generation / "h2.out").write_text("interrupted\n", encoding="utf-8")
    (generation / "h2.xyz").write_text(_CRASHED_XYZ, encoding="utf-8")
    (generation / "h2.gbw").write_bytes(b"runtime-orbitals")

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    # The user-directed orbital chain wins; the runtime checkpoint is ignored.
    assert replacement["recovery"]["checkpoint_role"] == ""
    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert '"guess.gbw"' in bound_text
    assert "moinp.gbw" not in bound_text
    verify_orca_execution_snapshot(
        job_dir,
        replacement,
        expected_selected_inp=replacement["selected_inp"],
        expected_source_selected_inp=replacement["source_selected_inp"],
        expected_selected_input_xyz="",
        expected_resource_request={"max_cores": 1, "max_memory_gb": 1},
        expected_max_retries=0,
    )


def test_second_crash_reseeds_the_latest_checkpoint(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    first = _build(job_dir, selected, executable)
    first_generation = _crash_generation(first)
    (first_generation / "h2.gbw").write_bytes(b"scf-orbitals-v1")
    second = _build(job_dir, selected, executable, recovery_from=first)

    second_generation = Path(second["execution_dir"])
    (second_generation / "h2.out").write_text("interrupted again\n", encoding="utf-8")
    (second_generation / "h2.xyz").write_text(_CRASHED_XYZ, encoding="utf-8")
    (second_generation / "h2.gbw").write_bytes(b"scf-orbitals-v2")

    third = _build(job_dir, selected, executable, recovery_from=second)

    checkpoint_role = third["recovery"]["checkpoint_role"]
    assert checkpoint_role
    private = Path(str(third["materialized_inputs"][checkpoint_role]["path"]))
    assert private.read_bytes() == b"scf-orbitals-v2"
    _verify(job_dir, third)


def _hessian_job(tmp_path: Path) -> tuple[Path, Path, Path]:
    job_dir = tmp_path / "freq_job"
    job_dir.mkdir()
    (job_dir / "ts.xyz").write_text(_PRISTINE_XYZ, encoding="utf-8")
    (job_dir / "ts.inhess.hess").write_text("$hessian\n1\n1.0\n$end\n", encoding="utf-8")
    selected = job_dir / "ts.inp"
    selected.write_text(
        "\n".join(
            [
                "! HF STO-3G Opt Freq",
                "%geom",
                '  InHessName "ts.inhess.hess"',
                "end",
                "* xyzfile 0 1 ts.xyz",
                "",
            ]
        ),
        encoding="utf-8",
    )
    executable = _write_executable(tmp_path / "freq-orca")
    return job_dir, selected, executable


def test_recovery_checkpoint_with_freq_and_hessian_dependency(tmp_path: Path) -> None:
    job_dir, selected, executable = _hessian_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    generation = Path(crashed["execution_dir"])
    (generation / "ts.out").write_text("interrupted\n", encoding="utf-8")
    (generation / "ts.xyz").write_text(_CRASHED_XYZ, encoding="utf-8")
    (generation / "ts.gbw").write_bytes(b"freq-orbitals")

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    checkpoint_role = replacement["recovery"]["checkpoint_role"]
    assert checkpoint_role
    private = Path(str(replacement["materialized_inputs"][checkpoint_role]["path"]))
    assert private.name == "ts.moinp.gbw"
    assert replacement["dependency_paths"] == sorted(
        replacement["dependency_paths"],
        key=lambda path: Path(path).relative_to(job_dir).as_posix(),
    )
    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert "MORead" in bound_text
    assert '%moinp "ts.moinp.gbw"' in bound_text
    verify_orca_execution_snapshot(
        job_dir,
        replacement,
        expected_selected_inp=replacement["selected_inp"],
        expected_source_selected_inp=replacement["source_selected_inp"],
        expected_selected_input_xyz="",
        expected_resource_request={"max_cores": 1, "max_memory_gb": 1},
        expected_max_retries=0,
    )


def test_verify_rejects_checkpoint_rename_contract_violation(tmp_path: Path) -> None:
    # A forged recovery block on an ordinary snapshot cannot bless a normal
    # dependency as the renamed checkpoint: its source is not `<stem>.gbw`.
    job_dir, selected, executable = _hessian_job(tmp_path)
    snapshot = _build(job_dir, selected, executable)
    hess_role = next(
        role
        for role, descriptor in snapshot["source_inputs"].items()
        if role != "selected_source" and str(descriptor.get("source_path") or "").endswith(".hess")
    )

    tampered = dict(snapshot)
    tampered["recovery"] = {
        "previous_generation_name": "",
        "previous_execution_dir": "",
        "seeded_roles": {},
        "checkpoint_role": hess_role,
    }

    with pytest.raises(ValueError, match="rename contract"):
        verify_orca_execution_snapshot(
            job_dir,
            tampered,
            expected_selected_inp=tampered["selected_inp"],
            expected_source_selected_inp=tampered["source_selected_inp"],
            expected_selected_input_xyz="",
            expected_resource_request={"max_cores": 1, "max_memory_gb": 1},
            expected_max_retries=0,
        )


def test_recovery_checkpoint_skipped_when_oversized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orca_auto.orca.execution_binding as binding

    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    old_generation = _crash_generation(crashed)
    (old_generation / "h2.gbw").write_bytes(b"x" * 200_000)
    monkeypatch.setattr(binding, "MAX_INPUT_SNAPSHOT_BYTES", 100_000)

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    assert replacement["recovery"]["checkpoint_role"] == ""
    assert "MORead" not in Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    _verify(job_dir, replacement)


def test_recovery_checkpoint_for_single_point_input(tmp_path: Path) -> None:
    job_dir = tmp_path / "sp_job"
    job_dir.mkdir()
    (job_dir / "h2.xyz").write_text(_PRISTINE_XYZ, encoding="utf-8")
    selected = job_dir / "h2.inp"
    selected.write_text("! HF STO-3G\n* xyzfile 0 1 h2.xyz\n", encoding="utf-8")
    executable = _write_executable(tmp_path / "sp-orca")
    crashed = _build(job_dir, selected, executable)
    generation = Path(crashed["execution_dir"])
    (generation / "h2.out").write_text("interrupted\n", encoding="utf-8")
    (generation / "h2.gbw").write_bytes(b"sp-orbitals")

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    assert replacement["recovery"]["checkpoint_role"]
    assert replacement["recovery"]["seeded_roles"] == {}
    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert '%moinp "h2.moinp.gbw"' in bound_text
    assert "xyzfile" in bound_text
    _verify(job_dir, replacement)


def test_verify_rejects_checkpoint_role_tamper(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    old_generation = _crash_generation(crashed)
    (old_generation / "h2.gbw").write_bytes(b"scf-orbitals-v1")
    replacement = _build(job_dir, selected, executable, recovery_from=crashed)
    checkpoint_role = replacement["recovery"]["checkpoint_role"]
    other_role = next(
        role for role in replacement["materialized_inputs"] if role != checkpoint_role
    )

    tampered = dict(replacement)
    tampered["recovery"] = dict(replacement["recovery"], checkpoint_role=other_role)

    with pytest.raises(ValueError):
        _verify(job_dir, tampered)


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


_COMPLETED_OUT = "\n".join(
    [
        "FINAL SINGLE POINT ENERGY      -1.123456789012",
        "",
        "                             ****ORCA TERMINATED NORMALLY****",
        "TOTAL RUN TIME: 0 days 0 hours 0 minutes 2 seconds 3 msec",
        "",
    ]
)


def _sp_job(tmp_path: Path) -> tuple[Path, Path, Path]:
    job_dir = tmp_path / "sp_completed_job"
    job_dir.mkdir()
    (job_dir / "h2.xyz").write_text(_PRISTINE_XYZ, encoding="utf-8")
    selected = job_dir / "h2.inp"
    selected.write_text("! HF STO-3G\n* xyzfile 0 1 h2.xyz\n", encoding="utf-8")
    executable = _write_executable(tmp_path / "sp-completed-orca")
    return job_dir, selected, executable


def test_rebind_keeps_a_completed_generation_for_adoption(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    job_dir, selected, executable = _sp_job(queue_root)
    snapshot = _build(job_dir, selected, executable, queue_root=queue_root)
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
    enqueue(queue_root, str(job_dir), force=True, task_id="task-completed", metadata=metadata)
    running = dequeue_next(queue_root)
    assert running is not None
    generation = Path(snapshot["execution_dir"])
    # The crash landed after ORCA finished: a completed, analyzer-verified
    # output exists next to the bound input.
    (generation / "h2.out").write_text(_COMPLETED_OUT, encoding="utf-8")
    (generation / "h2.gbw").write_bytes(b"final-orbitals")

    def unexpected_cfg() -> Any:
        raise AssertionError("a completed claim must not rebind")

    result = worker_job._maybe_rebind_recovery_generation(
        running,
        queue_root=queue_root,
        cfg_factory=unexpected_cfg,
    )

    assert result is running
    (row,) = list_queue(queue_root)
    assert worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY not in row.metadata
    assert row.metadata["execution_snapshot"]["generation_name"] == snapshot["generation_name"]
    # The ordinary context build accepts the finished generation so the
    # completed-adoption path can claim the result.
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(queue_root)))
    context = worker_job._build_execution_context(
        cfg,
        result,
        worker_config_path="/tmp/config.yaml",
        admission_token=None,
    )
    assert context.execution_snapshot["generation_name"] == snapshot["generation_name"]


def test_recovery_checkpoint_prefers_the_newest_attempt_gbw(tmp_path: Path) -> None:
    import os

    job_dir = tmp_path / "scants_job"
    job_dir.mkdir()
    (job_dir / "ts.xyz").write_text(_PRISTINE_XYZ, encoding="utf-8")
    selected = job_dir / "ts.inp"
    selected.write_text("! HF STO-3G ScanTS\n* xyzfile 0 1 ts.xyz\n", encoding="utf-8")
    executable = _write_executable(tmp_path / "scants-orca")
    crashed = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz="",
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        max_retries=3,
        orca_executable=executable,
    )
    generation = Path(crashed["execution_dir"])
    (generation / "ts.out").write_text("interrupted\n", encoding="utf-8")
    (generation / "ts.gbw").write_bytes(b"attempt-base")
    (generation / "ts.retry01.gbw").write_bytes(b"attempt-retry01")
    base_ns = 1_700_000_000_000_000_000
    os.utime(generation / "ts.gbw", ns=(base_ns, base_ns))
    os.utime(generation / "ts.retry01.gbw", ns=(base_ns + 5_000_000_000, base_ns + 5_000_000_000))

    replacement = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz="",
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        max_retries=3,
        orca_executable=executable,
        recovery_from=crashed,
    )

    checkpoint_role = replacement["recovery"]["checkpoint_role"]
    assert checkpoint_role
    materialized = replacement["materialized_inputs"][checkpoint_role]
    private = Path(str(materialized["path"]))
    assert private.name == "ts.moinp.gbw"
    assert private.read_bytes() == b"attempt-retry01"
    assert replacement["source_inputs"][checkpoint_role]["source_path"] == str(
        generation / "ts.retry01.gbw"
    )
    verify_orca_execution_snapshot(
        job_dir,
        replacement,
        expected_selected_inp=replacement["selected_inp"],
        expected_source_selected_inp=replacement["source_selected_inp"],
        expected_selected_input_xyz="",
        expected_resource_request={"max_cores": 1, "max_memory_gb": 1},
        expected_max_retries=3,
    )


def test_checkpoint_verify_is_independent_of_later_source_edits(tmp_path: Path) -> None:
    import os

    job_dir = tmp_path / "scants_edit_job"
    job_dir.mkdir()
    (job_dir / "ts.xyz").write_text(_PRISTINE_XYZ, encoding="utf-8")
    selected = job_dir / "ts.inp"
    selected.write_text("! HF STO-3G ScanTS\n* xyzfile 0 1 ts.xyz\n", encoding="utf-8")
    executable = _write_executable(tmp_path / "scants-edit-orca")
    crashed = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz="",
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        max_retries=3,
        orca_executable=executable,
    )
    generation = Path(crashed["execution_dir"])
    (generation / "ts.out").write_text("interrupted\n", encoding="utf-8")
    (generation / "ts.retry01.gbw").write_bytes(b"retry-orbitals")
    base_ns = 1_700_000_000_000_000_000
    os.utime(generation / "ts.retry01.gbw", ns=(base_ns, base_ns))
    replacement = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz="",
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        max_retries=3,
        orca_executable=executable,
        recovery_from=crashed,
    )
    assert replacement["recovery"]["checkpoint_role"]

    # A later benign edit of the mutable source input (route no longer ScanTS)
    # must not brick the intact, hash-pinned recovery snapshot at re-claim.
    selected.write_text("! HF STO-3G Opt\n* xyzfile 0 1 ts.xyz\n", encoding="utf-8")

    verify_orca_execution_snapshot(
        job_dir,
        replacement,
        expected_selected_inp=replacement["selected_inp"],
        expected_source_selected_inp=replacement["source_selected_inp"],
        expected_selected_input_xyz="",
        expected_resource_request={"max_cores": 1, "max_memory_gb": 1},
        expected_max_retries=3,
    )
