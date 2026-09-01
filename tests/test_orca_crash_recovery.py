from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    SNAPSHOT_INTENT_TOKEN_KEY,
    discard_snapshot_intent,
    transition_snapshot_intent,
)
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.orca import execution_binding as binding_mod
from orca_auto.orca import worker_execution as worker_job
from orca_auto.orca.execution_binding import (
    build_orca_execution_snapshot,
    orca_execution_started_evidence,
    verify_orca_execution_snapshot,
)
from orca_auto.orca.queue.adapter import dequeue_next, enqueue, list_queue
from orca_auto.orca.submission import mark_orca_snapshot_owned

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


def test_recovery_rejects_runtime_seed_with_substituted_atom_labels(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    generation = _crash_generation(crashed)
    (generation / "h2.xyz").write_text(
        "2\nsubstituted species\nHe 0 0 0\nLi 0 0 0.80\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not preserve the submitted atom count, order"):
        _build(job_dir, selected, executable, recovery_from=crashed)

    assert [
        child
        for child in job_dir.iterdir()
        if child.is_dir() and is_visible_generation_name(child.name)
    ] == [generation]


@pytest.mark.parametrize(
    "invalid_seed",
    [
        "2\nnon-finite\nH 0 0 0\nH nan 0 0\n",
        "2\nextra coordinate\nH 0 0 0\nH 0 0 0.80 1.0\n",
        "2\ntrailing row\nH 0 0 0\nH 0 0 0.80\nHe 0 0 1\n",
    ],
)
def test_recovery_invalid_runtime_seed_falls_back_to_submitted_geometry(
    tmp_path: Path,
    invalid_seed: str,
) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    generation = _crash_generation(crashed)
    (generation / "h2.xyz").write_text(invalid_seed, encoding="utf-8")

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert "H 0 0 0.74" in bound_text
    assert "nan" not in bound_text
    assert "He 0 0 1" not in bound_text
    _verify(job_dir, replacement)


def test_recovery_valid_seed_does_not_use_changed_job_root_geometry(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    _crash_generation(crashed)
    (job_dir / "h2.xyz").write_text(
        "2\nedited after submission\nH 0 0 0\nH 0 0 9.99\n",
        encoding="utf-8",
    )

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert "H 0 0 0.80" in bound_text
    assert "9.99" not in bound_text
    _verify(job_dir, replacement)


@pytest.mark.parametrize("remove_mode", ["deleted", "renamed"])
def test_recovery_valid_seed_does_not_require_current_job_root_geometry(
    tmp_path: Path,
    remove_mode: str,
) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    _crash_generation(crashed)
    current_geometry = job_dir / "h2.xyz"
    if remove_mode == "deleted":
        current_geometry.unlink()
    else:
        current_geometry.rename(job_dir / "h2.original.xyz")

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert "H 0 0 0.80" in bound_text
    assert replacement["recovery"]["seeded_roles"]
    _verify(job_dir, replacement)


def test_recovery_materializes_the_exact_validated_seed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    generation = _crash_generation(crashed)
    seed_path = generation / "h2.xyz"
    real_read = binding_mod.read_stable_regular_file
    seed_reads = 0

    def substitute_on_second_seed_read(path: str | Path, **kwargs: Any) -> bytes:
        nonlocal seed_reads
        if Path(path) == seed_path:
            seed_reads += 1
            if seed_reads > 1:
                return b"1\nsubstituted after validation\nHe 0 0 0\n"
        return real_read(path, **kwargs)

    monkeypatch.setattr(binding_mod, "read_stable_regular_file", substitute_on_second_seed_read)

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    assert seed_reads == 1
    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert "H 0 0 0.80" in bound_text
    assert "He 0 0 0" not in bound_text
    _verify(job_dir, replacement)


def test_recovery_atom_guard_rejects_bytes_outside_bound_selected_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    generation = _crash_generation(crashed)
    bound_selected = Path(crashed["bound_selected_identity"]["path"])
    real_read = binding_mod.read_stable_regular_file

    def substitute_bound_selected(path: str | Path, **kwargs: Any) -> bytes:
        if Path(path) == bound_selected:
            return b"! HF STO-3G Opt\n* xyz 0 1\nHe 0 0 0\n*\n"
        return real_read(path, **kwargs)

    monkeypatch.setattr(binding_mod, "read_stable_regular_file", substitute_bound_selected)

    with pytest.raises(ValueError, match="recovery bound selected input snapshot is corrupt"):
        _build(job_dir, selected, executable, recovery_from=crashed)

    assert not any(
        child.is_dir() and child != generation
        for child in job_dir.iterdir()
        if is_visible_generation_name(child.name)
    )


def test_recovery_accepts_identity_bound_input_larger_than_one_source_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(binding_mod, "MAX_INPUT_SNAPSHOT_BYTES", 240)
    monkeypatch.setattr(binding_mod, "MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES", 960)
    job_dir, selected, executable = _mutable_job(tmp_path)
    selected.write_text(
        "! HF STO-3G Opt\n#" + "x" * 200 + "\n* xyzfile 0 1 h2.xyz\n",
        encoding="utf-8",
    )
    assert selected.stat().st_size <= binding_mod.MAX_INPUT_SNAPSHOT_BYTES

    crashed = _build(job_dir, selected, executable)
    bound_selected = Path(crashed["bound_selected_identity"]["path"])
    assert bound_selected.stat().st_size > binding_mod.MAX_INPUT_SNAPSHOT_BYTES
    _crash_generation(crashed)

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    assert "H 0 0 0.80" in Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    _verify(job_dir, replacement)


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


def test_recovery_build_rejects_changed_source_when_seed_is_missing(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    generation = Path(crashed["execution_dir"])
    (generation / "h2.out").write_text("interrupted\n", encoding="utf-8")
    (generation / "h2.xyz").unlink()
    (job_dir / "h2.xyz").write_text(
        "2\nedited after submission\nH 0 0 0\nH 0 0 9.99\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="dependency changed since the crashed submission"):
        _build(job_dir, selected, executable, recovery_from=crashed)


def test_second_recovery_missing_seed_uses_unchanged_submitted_source(tmp_path: Path) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    first = _build(job_dir, selected, executable)
    _crash_generation(first)
    second = _build(job_dir, selected, executable, recovery_from=first)
    second_generation = Path(second["execution_dir"])
    (second_generation / "h2.out").write_text("interrupted again\n", encoding="utf-8")
    (second_generation / "h2.xyz").unlink()

    third = _build(job_dir, selected, executable, recovery_from=second)

    assert third["recovery"]["seeded_roles"] == {}
    bound_text = Path(third["selected_inp"]).read_text(encoding="utf-8")
    assert "H 0 0 0.74" in bound_text
    _verify(job_dir, third)


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


@pytest.mark.parametrize(
    "scf_block",
    [
        '%scf Guess MORead MOInp "guess.gbw" end',
        "% SCF\n  GUESS   moread\n  MOINP = 'guess.gbw'\nEND",
    ],
)
def test_recovery_checkpoint_skipped_when_scf_block_already_moreads(
    tmp_path: Path,
    scf_block: str,
) -> None:
    job_dir = tmp_path / "moread_scf_job"
    job_dir.mkdir()
    (job_dir / "h2.xyz").write_text(_PRISTINE_XYZ, encoding="utf-8")
    (job_dir / "guess.gbw").write_bytes(b"user-supplied-orbitals")
    selected = job_dir / "h2.inp"
    selected.write_text(
        f"! HF STO-3G Opt\n{scf_block}\n* xyzfile 0 1 h2.xyz\n",
        encoding="utf-8",
    )
    executable = _write_executable(tmp_path / "moread-scf-orca")
    crashed = _build(job_dir, selected, executable)
    generation = _crash_generation(crashed)
    (generation / "h2.gbw").write_bytes(b"runtime-orbitals")

    replacement = _build(job_dir, selected, executable, recovery_from=crashed)

    assert replacement["recovery"]["checkpoint_role"] == ""
    bound_text = Path(replacement["selected_inp"]).read_text(encoding="utf-8")
    assert "guess.gbw" in bound_text
    assert "moinp.gbw" not in bound_text
    _verify(job_dir, replacement)


def test_recovery_build_rejects_changed_executable_before_reserving_generation(
    tmp_path: Path,
) -> None:
    job_dir, selected, executable = _mutable_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    old_generation = _crash_generation(crashed)
    replacement_executable = _write_executable(tmp_path / "replacement-orca")

    with pytest.raises(ValueError, match="executable does not match the submitted identity"):
        _build(
            job_dir,
            selected,
            replacement_executable,
            recovery_from=crashed,
        )

    assert [
        child
        for child in job_dir.iterdir()
        if child.is_dir() and is_visible_generation_name(child.name)
    ] == [old_generation]


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


def test_recovery_build_rejects_changed_immutable_dependency(tmp_path: Path) -> None:
    job_dir, selected, executable = _hessian_job(tmp_path)
    crashed = _build(job_dir, selected, executable)
    generation = Path(crashed["execution_dir"])
    (generation / "ts.out").write_text("interrupted\n", encoding="utf-8")
    (job_dir / "ts.inhess.hess").write_text(
        "$hessian\n1\n9.9\n$end\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="dependency changed since the crashed submission"):
        _build(job_dir, selected, executable, recovery_from=crashed)


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
    assert mark_orca_snapshot_owned(queue_root, snapshot[SNAPSHOT_INTENT_TOKEN_KEY]) is None
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


def test_rebind_replay_after_budget_claim_reuses_the_same_ordinal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    old_generation = _crash_generation(snapshot)
    cfg = _worker_cfg(queue_root, executable)
    real_build = worker_job.build_orca_execution_snapshot
    build_count = 0

    def crash_first_build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal build_count
        build_count += 1
        if build_count == 1:
            raise RuntimeError("simulated crash after budget claim")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(worker_job, "build_orca_execution_snapshot", crash_first_build)

    with pytest.raises(RuntimeError, match="simulated crash after budget claim"):
        worker_job._maybe_rebind_recovery_generation(
            running,
            queue_root=queue_root,
            cfg_factory=lambda: cfg,
        )

    (claimed,) = list_queue(queue_root)
    assert claimed.metadata[worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY] == 1
    durable_claim = claimed.metadata[worker_job.RECOVERY_REBIND_CLAIM_METADATA_KEY]
    assert durable_claim["ordinal"] == 1
    assert durable_claim["source_generation_name"] == snapshot["generation_name"]
    intent_token = durable_claim["intent_token"]
    target_generation_name = durable_claim["target_generation_name"]
    assert intent_token
    assert is_visible_generation_name(target_generation_name)

    result = worker_job._maybe_rebind_recovery_generation(
        claimed,
        queue_root=queue_root,
        cfg_factory=lambda: cfg,
    )

    assert build_count == 2
    assert result.metadata[worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY] == 1
    assert result.metadata[worker_job.RECOVERY_REBIND_CLAIM_METADATA_KEY] is None
    replacement = result.metadata["execution_snapshot"]
    assert replacement[SNAPSHOT_INTENT_TOKEN_KEY] == intent_token
    assert replacement["generation_name"] == target_generation_name
    assert replacement["generation_name"] != snapshot["generation_name"]
    assert sorted(
        child.name
        for child in Path(result.metadata["reaction_dir"]).iterdir()
        if child.is_dir() and is_visible_generation_name(child.name)
    ) == sorted([old_generation.name, replacement["generation_name"]])


def test_rebind_replay_resumes_a_pending_claim_at_the_recovery_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    _crash_generation(snapshot)
    cfg = _worker_cfg(queue_root, executable)
    from orca_auto.orca.queue.adapter import update_metadata

    assert update_metadata(
        queue_root,
        str(running.queue_id),
        {worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY: (worker_job.RECOVERY_REBIND_LIMIT - 1)},
        expected_entry=running,
    )
    (penultimate,) = list_queue(queue_root)
    real_build = worker_job.build_orca_execution_snapshot
    build_count = 0

    def crash_first_build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal build_count
        build_count += 1
        if build_count == 1:
            raise RuntimeError("simulated crash after final budget claim")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(worker_job, "build_orca_execution_snapshot", crash_first_build)

    with pytest.raises(RuntimeError, match="simulated crash after final budget claim"):
        worker_job._maybe_rebind_recovery_generation(
            penultimate,
            queue_root=queue_root,
            cfg_factory=lambda: cfg,
        )

    (claimed,) = list_queue(queue_root)
    assert (
        claimed.metadata[worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY]
        == worker_job.RECOVERY_REBIND_LIMIT
    )
    durable_claim = claimed.metadata.get("recovery_rebind_claim")
    intent_token = (
        str(durable_claim.get("intent_token") or "") if isinstance(durable_claim, dict) else ""
    )

    result = worker_job._maybe_rebind_recovery_generation(
        claimed,
        queue_root=queue_root,
        cfg_factory=lambda: cfg,
    )

    assert build_count == 2
    assert (
        result.metadata[worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY]
        == worker_job.RECOVERY_REBIND_LIMIT
    )
    assert result.metadata["execution_snapshot"][SNAPSHOT_INTENT_TOKEN_KEY] == intent_token


def test_rebind_rejects_a_mismatched_durable_claim_without_consuming_budget(
    tmp_path: Path,
) -> None:
    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    old_generation = _crash_generation(snapshot)
    cfg = _worker_cfg(queue_root, executable)
    from orca_auto.orca.queue.adapter import update_metadata

    assert update_metadata(
        queue_root,
        str(running.queue_id),
        {
            worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY: 1,
            "recovery_rebind_claim": {
                "ordinal": 1,
                "source_generation_name": "20000101-000000-deadbeef",
                "intent_token": "snapshot_intent-forged-0123456789abcdef",
                "target_generation_name": "20000101-000001-cafebabe",
            },
        },
        expected_entry=running,
    )
    (claimed,) = list_queue(queue_root)

    with pytest.raises(ValueError, match="durable rebind claim does not match"):
        worker_job._maybe_rebind_recovery_generation(
            claimed,
            queue_root=queue_root,
            cfg_factory=lambda: cfg,
        )

    (row,) = list_queue(queue_root)
    assert row.metadata[worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY] == 1
    assert row.metadata["execution_snapshot"]["generation_name"] == snapshot["generation_name"]
    assert [
        child
        for child in Path(row.metadata["reaction_dir"]).iterdir()
        if child.is_dir() and is_visible_generation_name(child.name)
    ] == [old_generation]


def test_rebind_rejects_boolean_count_with_pending_claim_without_mutation(
    tmp_path: Path,
) -> None:
    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    old_generation = _crash_generation(snapshot)
    cfg = _worker_cfg(queue_root, executable)
    from orca_auto.orca.queue.adapter import update_metadata

    durable_claim = {
        "ordinal": worker_job.RECOVERY_REBIND_LIMIT,
        "source_generation_name": snapshot["generation_name"],
        "intent_token": "snapshot_intent-boolean-count-0123456789abcdef",
        "target_generation_name": "20000101-000001-cafebabe",
    }
    assert update_metadata(
        queue_root,
        str(running.queue_id),
        {
            worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY: True,
            worker_job.RECOVERY_REBIND_CLAIM_METADATA_KEY: durable_claim,
        },
        expected_entry=running,
    )
    (claimed,) = list_queue(queue_root)

    with pytest.raises(ValueError, match="invalid durable rebind count"):
        worker_job._maybe_rebind_recovery_generation(
            claimed,
            queue_root=queue_root,
            cfg_factory=lambda: cfg,
        )

    (row,) = list_queue(queue_root)
    assert row.metadata[worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY] is True
    assert row.metadata[worker_job.RECOVERY_REBIND_CLAIM_METADATA_KEY] == durable_claim
    assert row.metadata["execution_snapshot"] == snapshot
    assert [
        child
        for child in Path(row.metadata["reaction_dir"]).iterdir()
        if child.is_dir() and is_visible_generation_name(child.name)
    ] == [old_generation]


@pytest.mark.parametrize(
    "invalid_intent_token",
    [
        1234567890123456,
        True,
        " snapshot_intent_20260901_212345_0123456789abcdef0123456789abcdef ",
        "snapshot_intent-forged-0123456789abcdef",
        {"token": "snapshot_intent_20260901_212345_0123456789abcdef0123456789abcdef"},
    ],
    ids=["integer", "boolean", "whitespace", "nonproducer-format", "mapping"],
)
@pytest.mark.parametrize(
    "cancellation_committed",
    [False, True],
    ids=["without-cancellation", "with-cancellation"],
)
def test_rebind_rejects_noncanonical_intent_token_without_mutation(
    tmp_path: Path,
    invalid_intent_token: Any,
    cancellation_committed: bool,
) -> None:
    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    old_generation = _crash_generation(snapshot)
    cfg = _worker_cfg(queue_root, executable)
    from orca_auto.orca.queue.adapter import cancel, update_metadata

    durable_claim = {
        "ordinal": 1,
        "source_generation_name": snapshot["generation_name"],
        "intent_token": invalid_intent_token,
        "target_generation_name": "20000101-000001-cafebabe",
    }
    assert update_metadata(
        queue_root,
        str(running.queue_id),
        {
            worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY: 1,
            worker_job.RECOVERY_REBIND_CLAIM_METADATA_KEY: durable_claim,
        },
        expected_entry=running,
    )
    if cancellation_committed:
        cancelled = cancel(queue_root, str(running.queue_id))
        assert cancelled is not None and cancelled.cancel_requested
    (claimed,) = list_queue(queue_root)
    queue_path = queue_root / "queue.json"
    before = queue_path.read_bytes()
    intent_dir = queue_root / ".orca_auto_snapshot_intents"
    intent_dir_existed = intent_dir.exists()
    intent_payloads_before = (
        {path.name: path.read_bytes() for path in intent_dir.glob("*.json")}
        if intent_dir.is_dir()
        else {}
    )

    with pytest.raises(ValueError, match="durable rebind claim does not match"):
        worker_job._maybe_rebind_recovery_generation(
            claimed,
            queue_root=queue_root,
            cfg_factory=lambda: cfg,
        )

    assert queue_path.read_bytes() == before
    assert [
        child
        for child in Path(claimed.metadata["reaction_dir"]).iterdir()
        if child.is_dir() and is_visible_generation_name(child.name)
    ] == [old_generation]
    assert intent_dir.exists() is intent_dir_existed
    assert (
        {path.name: path.read_bytes() for path in intent_dir.glob("*.json")}
        if intent_dir.is_dir()
        else {}
    ) == intent_payloads_before


def test_rebind_does_not_publish_after_cancellation_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    old_generation = _crash_generation(snapshot)
    cfg = _worker_cfg(queue_root, executable)
    from orca_auto.orca.queue.adapter import cancel

    real_update = worker_job.update_metadata
    cancellation_committed = False

    def cancel_before_publication(
        root: Path,
        queue_id: str,
        metadata_update: dict[str, Any],
        **kwargs: Any,
    ) -> bool:
        nonlocal cancellation_committed
        expected_entry = kwargs.get("expected_entry")
        if "execution_snapshot" in metadata_update and not cancellation_committed:
            cancelled = cancel(root, queue_id, expected_entry=expected_entry)
            assert cancelled is not None and cancelled.cancel_requested
            cancellation_committed = True
        return real_update(root, queue_id, metadata_update, **kwargs)

    monkeypatch.setattr(worker_job, "update_metadata", cancel_before_publication)

    with pytest.raises(ValueError, match="could not publish its replacement generation"):
        worker_job._maybe_rebind_recovery_generation(
            running,
            queue_root=queue_root,
            cfg_factory=lambda: cfg,
        )

    assert cancellation_committed
    (row,) = list_queue(queue_root)
    assert row.cancel_requested
    assert row.metadata["execution_snapshot"] == snapshot
    durable_claim = row.metadata.get(worker_job.RECOVERY_REBIND_CLAIM_METADATA_KEY)
    assert isinstance(durable_claim, dict) and durable_claim["ordinal"] == 1
    assert [
        child
        for child in Path(row.metadata["reaction_dir"]).iterdir()
        if child.is_dir() and is_visible_generation_name(child.name)
    ] == [old_generation]


def test_rebind_prebind_crash_reuses_one_durable_target_without_generation_growth(
    tmp_path: Path,
) -> None:
    import os

    from orca_auto.core.queue.engine.snapshot_intent import (
        reconcile_orphaned_snapshot_generations,
    )

    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    old_generation = _crash_generation(snapshot)
    cfg = _worker_cfg(queue_root, executable)
    reaction_dir = Path(running.metadata["reaction_dir"])

    for iteration in range(worker_job.RECOVERY_REBIND_LIMIT + 1):
        child_pid = os.fork()
        if child_pid == 0:

            def exit_before_identity_bind(*_args: Any, **_kwargs: Any) -> None:
                os._exit(73)

            binding_mod.bind_snapshot_intent_generation_identities = exit_before_identity_bind
            try:
                current = list_queue(queue_root)[0]
                worker_job._maybe_rebind_recovery_generation(
                    current,
                    queue_root=queue_root,
                    cfg_factory=lambda: cfg,
                )
            except FileExistsError:
                os._exit(74)
            os._exit(75)

        waited_pid, wait_status = os.waitpid(child_pid, 0)
        assert waited_pid == child_pid
        exit_code = os.waitstatus_to_exitcode(wait_status)
        assert exit_code == (73 if iteration == 0 else 74)
        reconcile_orphaned_snapshot_generations([queue_root])
        visible_generations = sorted(
            child.name
            for child in reaction_dir.iterdir()
            if child.is_dir() and is_visible_generation_name(child.name)
        )
        assert len(visible_generations) == 2
        assert old_generation.name in visible_generations

    (claimed,) = list_queue(queue_root)
    assert claimed.metadata[worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY] == 1
    durable_claim = claimed.metadata.get(worker_job.RECOVERY_REBIND_CLAIM_METADATA_KEY)
    assert isinstance(durable_claim, dict)
    target_generation_name = durable_claim.get("target_generation_name")
    assert isinstance(target_generation_name, str) and is_visible_generation_name(
        target_generation_name
    )
    assert target_generation_name != old_generation.name
    assert (reaction_dir / target_generation_name).is_dir()


def test_rebind_replay_after_process_exit_reuses_claim_after_orphan_reconcile(
    tmp_path: Path,
) -> None:
    import os

    from orca_auto.core.queue.engine.snapshot_intent import (
        reconcile_orphaned_snapshot_generations,
    )

    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    old_generation = _crash_generation(snapshot)
    cfg = _worker_cfg(queue_root, executable)

    child_pid = os.fork()
    if child_pid == 0:

        def exit_after_snapshot_build(*_args: Any, **_kwargs: Any) -> None:
            os._exit(73)

        worker_job.transition_snapshot_intent = exit_after_snapshot_build
        worker_job._maybe_rebind_recovery_generation(
            running,
            queue_root=queue_root,
            cfg_factory=lambda: cfg,
        )
        os._exit(74)

    waited_pid, wait_status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(wait_status) == 73

    (claimed,) = list_queue(queue_root)
    assert claimed.metadata[worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY] == 1
    durable_claim = claimed.metadata.get("recovery_rebind_claim")
    intent_token = (
        str(durable_claim.get("intent_token") or "") if isinstance(durable_claim, dict) else ""
    )
    intent_dir = queue_root / ".orca_auto_snapshot_intents"
    assert intent_dir.is_dir() and any(intent_dir.glob("*.json"))
    assert (
        len(
            [
                child
                for child in Path(claimed.metadata["reaction_dir"]).iterdir()
                if child.is_dir() and is_visible_generation_name(child.name)
            ]
        )
        == 2
    )

    assert reconcile_orphaned_snapshot_generations([queue_root]) == 1
    assert not intent_dir.exists() or not any(intent_dir.glob("*.json"))
    assert [
        child
        for child in Path(claimed.metadata["reaction_dir"]).iterdir()
        if child.is_dir() and is_visible_generation_name(child.name)
    ] == [old_generation]

    result = worker_job._maybe_rebind_recovery_generation(
        claimed,
        queue_root=queue_root,
        cfg_factory=lambda: cfg,
    )

    assert result.metadata[worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY] == 1
    assert result.metadata.get("recovery_rebind_claim") is None
    replacement = result.metadata["execution_snapshot"]
    assert replacement[SNAPSHOT_INTENT_TOKEN_KEY] == intent_token
    assert sorted(
        child.name
        for child in Path(result.metadata["reaction_dir"]).iterdir()
        if child.is_dir() and is_visible_generation_name(child.name)
    ) == sorted([old_generation.name, replacement["generation_name"]])


@pytest.mark.parametrize("mismatch_kind", ["path", "sha256", "size"])
def test_rebind_rejects_executable_mismatch_before_consuming_budget(
    tmp_path: Path,
    mismatch_kind: str,
) -> None:
    queue_root, running, snapshot, executable = _claimed_mutable_entry(tmp_path)
    old_generation = _crash_generation(snapshot)
    if mismatch_kind == "path":
        configured_executable = _write_executable(tmp_path / "replacement-orca")
    elif mismatch_kind == "sha256":
        executable.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        configured_executable = executable
    else:
        executable.write_text("#!/bin/sh\n# replacement\nexit 0\n", encoding="utf-8")
        configured_executable = executable
    configured_executable.chmod(0o755)
    cfg = _worker_cfg(queue_root, configured_executable)

    with pytest.raises(ValueError, match="executable does not match the submitted identity"):
        worker_job._maybe_rebind_recovery_generation(
            running,
            queue_root=queue_root,
            cfg_factory=lambda: cfg,
        )

    (row,) = list_queue(queue_root)
    assert worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY not in row.metadata
    assert row.metadata["execution_snapshot"]["generation_name"] == snapshot["generation_name"]
    assert [
        child
        for child in Path(row.metadata["reaction_dir"]).iterdir()
        if child.is_dir() and is_visible_generation_name(child.name)
    ] == [old_generation]


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
    assert mark_orca_snapshot_owned(queue_root, snapshot[SNAPSHOT_INTENT_TOKEN_KEY]) is None
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


def test_marker_finalize_is_quiet_when_worker_already_retired_the_intent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue_root = tmp_path / "queue"
    queue_root.mkdir(exist_ok=True)
    job_dir, selected, executable = _mutable_job(queue_root, job_name="rxn")
    snapshot = _build(job_dir, selected, executable, queue_root=queue_root)
    token = snapshot[SNAPSHOT_INTENT_TOKEN_KEY]
    transition_snapshot_intent(
        queue_root,
        token,
        target_state=SNAPSHOT_INTENT_STATE_ENQUEUEING,
        expected_states={SNAPSHOT_INTENT_STATE_CREATING},
    )
    # The worker retires an intent as soon as a committed queue row references it.
    discard_snapshot_intent(queue_root, token)

    with caplog.at_level(logging.INFO, logger="orca_auto.core.queue.engine.snapshot_intent"):
        assert mark_orca_snapshot_owned(queue_root, token) is None

    records = [
        record
        for record in caplog.records
        if record.name == "orca_auto.core.queue.engine.snapshot_intent"
    ]
    assert not [record for record in records if record.levelno >= logging.WARNING]
    assert any("already retired" in record.getMessage() for record in records)


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
