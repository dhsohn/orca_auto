from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.geometry_limits import MAX_ADMISSION_ATOMS, MAX_HESSIAN_ADMISSION_ATOMS
from orca_auto.core.queue.engine.input_snapshot import MAX_INPUT_SNAPSHOT_BYTES
from orca_auto.orca.execution_binding import (
    MAX_ORCA_INPUT_REFERENCES,
    build_orca_execution_snapshot,
    verify_orca_execution_snapshot,
)


def _write_executable(path: Path, payload: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o755)
    return path


def _snapshot(tmp_path: Path) -> tuple[Path, Path, dict[str, Any], dict[str, int]]:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "input.xyz").write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    (job_dir / "charges.pc").write_text("0\n", encoding="utf-8")
    (job_dir / "guess.gbw").write_bytes(b"checkpoint")
    (job_dir / "initial.hess").write_text("$hessian\n1\n1.0\n$end\n", encoding="utf-8")
    selected = job_dir / "job.inp"
    selected.write_text(
        "\n".join(
            [
                "! Opt MORead",
                '%moinp "guess.gbw"',
                '%pointcharges "charges.pc"',
                "%geom",
                '  InHessName "initial.hess"',
                "end",
                "* xyzfile 0 1 input.xyz",
                "",
            ]
        ),
        encoding="utf-8",
    )
    executable = _write_executable(tmp_path / "orca")
    resources = {"max_cores": 2, "max_memory_gb": 4}
    snapshot = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        resource_request=resources,
        max_retries=2,
        orca_executable=executable,
    )
    return job_dir, selected, snapshot, resources


def _verify(
    job_dir: Path,
    _selected: Path,
    snapshot: dict[str, Any],
    resources: dict[str, int],
) -> tuple[Path, str]:
    return verify_orca_execution_snapshot(
        job_dir,
        snapshot,
        expected_selected_inp=snapshot["selected_inp"],
        expected_selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        expected_resource_request=resources,
        expected_max_retries=2,
    )


def test_orca_execution_snapshot_binds_selected_dependencies_and_executable(
    tmp_path: Path,
) -> None:
    job_dir, selected, snapshot, resources = _snapshot(tmp_path)

    verified_selected, executable = _verify(job_dir, selected, snapshot, resources)

    assert verified_selected == Path(snapshot["selected_inp"])
    assert executable == str((tmp_path / "orca").resolve())
    assert snapshot["dependency_paths"] == [
        str((job_dir / "charges.pc").resolve()),
        str((job_dir / "guess.gbw").resolve()),
        str((job_dir / "initial.hess").resolve()),
        str((job_dir / "input.xyz").resolve()),
    ]
    assert set(snapshot["input_snapshots"]) == {
        "selected_source",
        "dependency_000000",
        "dependency_000001",
        "dependency_000002",
        "dependency_000003",
    }
    input_generation = (
        job_dir / ".orca_auto_input_snapshots" / snapshot["input_snapshot_namespace"]
    ).resolve()
    assert all(
        Path(descriptor["snapshot_path"]).parent == input_generation
        for descriptor in snapshot["input_snapshots"].values()
    )
    assert (
        job_dir / ".orca_auto_snapshot_intents" / f"{snapshot['snapshot_intent_token']}.json"
    ).is_file()
    bound_text = verified_selected.read_text(encoding="utf-8")
    assert str(job_dir) not in bound_text
    assert '".inputs/dependency_000003-' in bound_text
    assert '".inputs/dependency_000001-' in bound_text


def test_orca_cleanup_rejects_a_mismatched_generation_pair(tmp_path: Path) -> None:
    import orca_auto.orca.execution_binding as binding

    job_dir, _selected, snapshot, _resources = _snapshot(tmp_path)
    foreign_namespace = "generation-foreign"
    foreign_generation = job_dir / ".orca_auto_input_snapshots" / foreign_namespace
    foreign_generation.mkdir()
    mismatched = dict(snapshot)
    mismatched["input_snapshot_namespace"] = foreign_namespace

    with pytest.raises(ValueError, match="mismatched"):
        binding.cleanup_unowned_orca_execution_snapshot(job_dir, mismatched)

    assert Path(snapshot["execution_dir"]).is_dir()
    assert foreign_generation.is_dir()


def test_orca_execution_directory_collision_preserves_existing_generation(
    tmp_path: Path,
) -> None:
    import orca_auto.orca.execution_binding as binding

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    generation_name = "generation-existing"
    existing = job_dir / binding.ORCA_EXECUTION_ROOT_NAME / generation_name
    existing.mkdir(parents=True)
    marker = existing / "owner.txt"
    marker.write_text("owner", encoding="utf-8")

    with pytest.raises(FileExistsError):
        binding._execution_directory(job_dir, generation_name)

    assert marker.read_text(encoding="utf-8") == "owner"


def test_orca_cleanup_failure_retains_durable_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orca_auto.orca.execution_binding as binding

    job_dir, _selected, snapshot, _resources = _snapshot(tmp_path)
    intent_path = (
        job_dir / ".orca_auto_snapshot_intents" / f"{snapshot['snapshot_intent_token']}.json"
    )

    def fail_remove(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated execution cleanup failure")

    monkeypatch.setattr(binding, "_cleanup_unowned_generation_directory", fail_remove)
    with pytest.raises(OSError, match="simulated"):
        binding.cleanup_unowned_orca_execution_snapshot(job_dir, snapshot)

    assert Path(snapshot["execution_dir"]).is_dir()
    assert intent_path.is_file()


def test_orca_cleanup_does_not_follow_substituted_execution_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orca_auto.core.queue.engine.input_snapshot as input_snapshot
    import orca_auto.orca.execution_binding as binding

    job_dir, _selected, snapshot, _resources = _snapshot(tmp_path)
    namespace = snapshot["input_snapshot_namespace"]
    execution_root = job_dir / binding.ORCA_EXECUTION_ROOT_NAME
    moved_root = job_dir / "moved-execution-root"
    outside_root = tmp_path / "outside-executions"
    outside_generation = outside_root / namespace
    outside_generation.mkdir(parents=True)
    sentinel = outside_generation / "sentinel.txt"
    sentinel.write_text("must survive", encoding="utf-8")
    original_remove = input_snapshot._remove_directory_contents_at
    substituted = False

    def substitute_root(directory_fd: int, *, label: str) -> None:
        nonlocal substituted
        if not substituted and label == "ORCA execution snapshot generation":
            substituted = True
            execution_root.rename(moved_root)
            execution_root.symlink_to(outside_root, target_is_directory=True)
        original_remove(directory_fd, label=label)

    monkeypatch.setattr(input_snapshot, "_remove_directory_contents_at", substitute_root)

    with pytest.raises(ValueError, match="ORCA execution snapshot root"):
        binding.cleanup_unowned_orca_execution_snapshot(job_dir, snapshot)

    assert substituted
    assert sentinel.read_text(encoding="utf-8") == "must survive"
    assert execution_root.is_symlink()


@pytest.mark.parametrize(
    "geometry_block",
    [
        "* int 0 1\nH 0 0 0\n*\n",
        "* internal 0 1\nH 0 0 0\n*\n",
        "* gzmtfile 0 1 geometry.gzmt\n",
        "%coords\n  CTyp xyz\n  Charge 0\n  Mult 1\n  coords\n    H 0 0 0\n  end\nend\n",
        "# hidden # % coords\n  CTyp xyz\nend\n",
        "%compound\nend\n",
        "%compound_file payload.inp\n",
        "Compound payload.inp\n",
        "! Compound\n",
        "* xyzfile 0 1\n",
    ],
)
def test_orca_execution_snapshot_rejects_unbounded_geometry_formats(
    tmp_path: Path,
    geometry_block: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected = job_dir / "job.inp"
    selected.write_text("! Freq\n" + geometry_block, encoding="utf-8")
    executable = _write_executable(tmp_path / "orca")

    with pytest.raises(ValueError, match="unsupported|invalid"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 2, "max_memory_gb": 4},
            max_retries=0,
            orca_executable=executable,
        )


@pytest.mark.parametrize(
    ("geometry_block", "message"),
    [
        ("* xyz 0 1\nH 0 0 0\n", "unterminated"),
        ("* xyzfile 0 1 input.xyz\n*\n", "unexpected"),
    ],
)
def test_orca_execution_snapshot_rejects_malformed_xyz_terminators(
    tmp_path: Path,
    geometry_block: str,
    message: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "input.xyz").write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    selected = job_dir / "job.inp"
    selected.write_text("! SP\n" + geometry_block, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz=str(job_dir / "input.xyz"),
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )


def test_orca_execution_snapshot_rejects_multiple_geometry_blocks(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected = job_dir / "job.inp"
    selected.write_text(
        "! SP\n* xyz 0 1\nH 0 0 0\n*\n$new_job\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )
    executable = _write_executable(tmp_path / "orca")

    with pytest.raises(ValueError, match="multiple"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 2, "max_memory_gb": 4},
            max_retries=0,
            orca_executable=executable,
        )


@pytest.mark.parametrize(
    "directives",
    [
        "%maxcore 1000\n# hidden # %maxcore 999999",
        "%pal nprocs 4 end\n# hidden # %pal nprocs 999 end",
        "%pal nprocs 4 nprocs 999 end",
        "! PAL4 PAL999",
        "%pal nprocs 4 end\n! PAL999",
        '%moinp "first.gbw"\n# hidden # %moinp "second.gbw"',
    ],
)
def test_orca_execution_snapshot_rejects_ambiguous_duplicate_directives(
    tmp_path: Path,
    directives: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected = job_dir / "job.inp"
    selected.write_text(
        f"! SP\n{directives}\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ambiguous duplicate"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )


def test_orca_execution_snapshot_binds_spaced_percent_moinp(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "guess.gbw").write_bytes(b"checkpoint")
    selected = job_dir / "job.inp"
    selected.write_text(
        '% moinp "guess.gbw"\n! SP\n* xyz 0 1\nH 0 0 0\n*\n',
        encoding="utf-8",
    )
    executable = _write_executable(tmp_path / "orca")

    snapshot = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz="",
        resource_request={"max_cores": 2, "max_memory_gb": 4},
        max_retries=0,
        orca_executable=executable,
    )

    assert snapshot["dependency_paths"] == [str((job_dir / "guess.gbw").resolve())]
    assert ".inputs/dependency_000000-" in Path(snapshot["selected_inp"]).read_text(
        encoding="utf-8"
    )


def test_orca_execution_snapshot_allows_unquoted_progress_input_filenames(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    checkpoint = job_dir / "progress.gbw"
    checkpoint.write_bytes(b"checkpoint")
    geometry = job_dir / "progress.xyz"
    geometry.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    selected = job_dir / "job.inp"
    selected.write_text(
        "%moinp progress.gbw\n! SP\n* xyzfile 0 1 progress.xyz\n",
        encoding="utf-8",
    )

    snapshot = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz=str(geometry),
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        max_retries=0,
        orca_executable=_write_executable(tmp_path / "orca"),
    )

    assert set(snapshot["dependency_paths"]) == {
        str(checkpoint.resolve()),
        str(geometry.resolve()),
    }


def test_orca_execution_snapshot_allows_builtin_gcpmethod(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected = job_dir / "job.inp"
    selected.write_text(
        '! SP\n%method GCPMETHOD "dft/svp" end\n* xyz 0 1\nH 0 0 0\n*\n',
        encoding="utf-8",
    )

    snapshot = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz="",
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        max_retries=0,
        orca_executable=_write_executable(tmp_path / "orca"),
    )

    assert snapshot["dependency_paths"] == []


@pytest.mark.parametrize(
    "directive",
    [
        '%cclib "aux.dat"',
        'orcafffilename "aux.dat"',
        'neb_end_pdbfile "aux.dat"',
        '%eda\n  Frag1_MethodFile "aux.dat"\nend',
        '%qmmm\n  QM2CustomFile "aux.dat"\nend',
        '%method\n  ProgExt "aux.dat"\n  Ext_Params "payload"\nend',
        "! ExtOpt",
        "!ExtOpt",
        "! SP GCP(FILE)",
        '%xtb\n  XTBINPUTSTRING "--input aux.dat"\nend',
        '%basis\n  GTOName "aux.dat"\nend',
        '%basis\n  ReadFragBasis "aux.dat"\nend',
        '%method\n  XTBParamFile "aux.dat"\nend',
        '%method\n  ProgCIS "aux.dat"\nend',
        '%method\n  ProgXTB "aux.dat"\nend',
    ],
)
def test_orca_execution_snapshot_rejects_unbound_auxiliary_directives(
    tmp_path: Path,
    directive: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "aux.dat").write_text("aux", encoding="utf-8")
    selected = job_dir / "job.inp"
    selected.write_text(
        f"! SP\n{directive}\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )
    executable = _write_executable(tmp_path / "orca")

    with pytest.raises(ValueError, match="Unsupported ORCA auxiliary"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 2, "max_memory_gb": 4},
            max_retries=0,
            orca_executable=executable,
        )


@pytest.mark.parametrize("target_name", ["job.inp", "input.xyz", "initial.hess"])
def test_orca_execution_snapshot_ignores_source_mutation_after_submission(
    tmp_path: Path,
    target_name: str,
) -> None:
    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    (job_dir / target_name).write_bytes(b"mutated")

    verified_selected, _executable = _verify(job_dir, selected, snapshot, resources)

    assert verified_selected.read_text(encoding="utf-8") != "mutated"


def test_orca_execution_snapshot_rejects_private_dependency_mutation(tmp_path: Path) -> None:
    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    private_dependency = Path(snapshot["materialized_inputs"]["dependency_000000"]["path"])
    private_dependency.chmod(0o600)
    private_dependency.write_bytes(b"mutated")

    with pytest.raises(ValueError, match="private dependency"):
        _verify(job_dir, selected, snapshot, resources)


def test_orca_execution_snapshot_rejects_executable_replacement(tmp_path: Path) -> None:
    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    _write_executable(tmp_path / "orca", "#!/bin/sh\nexit 1\n")

    with pytest.raises(ValueError, match="executable no longer matches"):
        _verify(job_dir, selected, snapshot, resources)


def test_orca_execution_snapshot_rejects_cross_field_tamper(tmp_path: Path) -> None:
    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    snapshot["resource_request"] = {"max_cores": 8, "max_memory_gb": 4}

    with pytest.raises(ValueError, match="resource request"):
        _verify(job_dir, selected, snapshot, resources)


def test_orca_execution_snapshot_rejects_referenced_path_escape(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    outside = tmp_path / "outside.xyz"
    outside.write_text("1\noutside\nH 0 0 0\n", encoding="utf-8")
    selected = job_dir / "job.inp"
    selected.write_text("! SP\n* xyzfile 0 1 ../outside.xyz\n", encoding="utf-8")

    with pytest.raises(ValueError, match="stay inside its root"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz=str(outside),
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )


def test_orca_execution_snapshot_rejects_selected_input_symlink(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    source = job_dir / "source.inp"
    source.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    selected = job_dir / "job.inp"
    selected.symlink_to(source.name)

    with pytest.raises(ValueError, match="must not be a symlink"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )


def test_orca_execution_snapshot_caps_external_reference_count(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "input.xyz").write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    selected = job_dir / "job.inp"
    selected.write_text(
        "! SP\n"
        + "\n".join(
            f'%pointcharges "input.xyz" # {index}' for index in range(MAX_ORCA_INPUT_REFERENCES + 1)
        )
        + "\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="external file references"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )


def test_orca_execution_snapshot_checks_aggregate_budget_before_dependency_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orca_auto.orca.execution_binding as binding

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    dependency = job_dir / "input.xyz"
    dependency.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    selected = job_dir / "job.inp"
    selected.write_text("! SP\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")
    monkeypatch.setattr(
        binding,
        "MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES",
        selected.stat().st_size + dependency.stat().st_size - 1,
    )

    with pytest.raises(ValueError, match="aggregate snapshot size"):
        binding.build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz=str(dependency),
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )

    snapshot_root = job_dir / ".orca_auto_input_snapshots"
    snapshot_names = (
        {path.name for path in snapshot_root.iterdir()} if snapshot_root.exists() else set()
    )
    assert all(not name.startswith("dependency_") for name in snapshot_names)
    execution_root = job_dir / binding.ORCA_EXECUTION_ROOT_NAME
    assert list(execution_root.iterdir()) == []


def test_orca_execution_snapshot_rejects_oversized_dependency_before_copy(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    dependency = job_dir / "large.gbw"
    with dependency.open("wb") as handle:
        handle.truncate(MAX_INPUT_SNAPSHOT_BYTES + 1)
    selected = job_dir / "job.inp"
    selected.write_text('! SP MORead\n%moinp "large.gbw"\n* xyz 0 1\nH 0 0 0\n*\n')

    with pytest.raises(ValueError, match="exceeds"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )

    snapshot_root = job_dir / ".orca_auto_input_snapshots"
    snapshot_names = (
        {path.name for path in snapshot_root.iterdir()} if snapshot_root.exists() else set()
    )
    assert all(not name.startswith("dependency_") for name in snapshot_names)


def test_orca_execution_snapshot_rejects_inline_geometry_above_atom_cap(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected = job_dir / "job.inp"
    selected.write_text(
        "! SP\n* xyz 0 1\n" + "H 0 0 0\n" * (MAX_ADMISSION_ATOMS + 1) + "*\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="server atom-count limit"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )


def test_orca_execution_snapshot_rejects_xyzfile_geometry_above_atom_cap(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "input.xyz").write_text(
        f"{MAX_ADMISSION_ATOMS + 1}\ninput\n",
        encoding="utf-8",
    )
    selected = job_dir / "job.inp"
    selected.write_text("! SP\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")

    with pytest.raises(ValueError, match="server atom-count limit"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz=str(job_dir / "input.xyz"),
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )


@pytest.mark.parametrize("route_line", ["! Freq", "# hidden # ! Freq"])
def test_orca_frequency_snapshot_uses_stricter_hessian_atom_cap(
    tmp_path: Path,
    route_line: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    atom_count = MAX_HESSIAN_ADMISSION_ATOMS + 1
    (job_dir / "input.xyz").write_text(
        f"{atom_count}\ninput\n" + "H 0 0 0\n" * atom_count,
        encoding="utf-8",
    )
    selected = job_dir / "job.inp"
    selected.write_text(f"{route_line}\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")

    with pytest.raises(ValueError, match="server atom-count limit"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz=str(job_dir / "input.xyz"),
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )
