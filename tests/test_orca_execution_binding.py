from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.geometry_limits import MAX_ADMISSION_ATOMS, MAX_HESSIAN_ADMISSION_ATOMS
from orca_auto.core.queue.engine.input_snapshot import MAX_INPUT_SNAPSHOT_BYTES
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.orca import input_blocks, input_references
from orca_auto.orca.execution_binding import (
    build_orca_execution_snapshot,
    verify_orca_execution_snapshot,
)


@pytest.mark.parametrize(
    "name",
    [
        "MAX_ORCA_INPUT_REFERENCES",
        "_BLOCK_FILE_REFERENCE_KEYS",
        "_NEB_FILE_REFERENCE_KEYS",
        "_SIMPLE_FILE_REFERENCE_KEYS",
        "_UNSUPPORTED_EXTERNAL_HOOK_KEYS",
        "_UNSUPPORTED_FILE_REFERENCE_KEYS",
        "neb_file_reference_context",
        "scan_orca_file_references",
    ],
)
def test_input_blocks_does_not_forward_reference_scanner_symbols(name: str) -> None:
    assert not hasattr(input_blocks, name)


@pytest.mark.parametrize(
    "name",
    [
        "OrcaFileReference",
        "OrcaLineToken",
        "input_blocks",
        "orca_line_tokens",
        "orca_moinp_references",
    ],
)
def test_input_references_does_not_forward_input_syntax_symbols(name: str) -> None:
    assert not hasattr(input_references, name)


def test_input_reference_scanner_resolves_syntax_helpers_from_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = input_blocks.OrcaFileReference(0, "owner.gbw", 0, 9, "auxiliary")
    calls = {"moinp": 0, "tokens": 0}

    def owner_moinp_references(lines: list[str]) -> list[input_blocks.OrcaFileReference]:
        calls["moinp"] += 1
        assert lines == ["owner lookup"]
        return [reference]

    def owner_line_tokens(line: str) -> list[input_blocks.OrcaLineToken]:
        calls["tokens"] += 1
        assert line == "owner lookup"
        return []

    monkeypatch.setattr(input_blocks, "orca_moinp_references", owner_moinp_references)
    monkeypatch.setattr(input_blocks, "orca_line_tokens", owner_line_tokens)

    assert input_references.scan_orca_file_references(["owner lookup"]) == [reference]
    assert calls == {"moinp": 1, "tokens": 1}


def _visible_generations(job_dir: Path) -> list[Path]:
    return [path for path in job_dir.iterdir() if is_visible_generation_name(path.name)]


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


def _same_basename_neb_sources(
    tmp_path: Path,
    *,
    reactant_payload: str,
    product_payload: str,
) -> tuple[Path, Path, Path, Path]:
    job_dir = tmp_path / "job"
    reactant = job_dir / "reactant" / "input.xyz"
    product = job_dir / "product" / "input.xyz"
    reactant.parent.mkdir(parents=True)
    product.parent.mkdir()
    reactant.write_text(reactant_payload, encoding="utf-8")
    product.write_text(product_payload, encoding="utf-8")
    selected = job_dir / "neb.inp"
    selected.write_text(
        '! NEB-TS\n%neb\n  Product "product/input.xyz"\nend\n* xyzfile 0 1 reactant/input.xyz\n',
        encoding="utf-8",
    )
    return job_dir, selected, reactant, product


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
        expected_source_selected_inp=snapshot["source_selected_inp"],
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
    assert snapshot["version"] == 2
    assert set(snapshot["source_inputs"]) == {
        "selected_source",
        "dependency_000000",
        "dependency_000001",
        "dependency_000002",
        "dependency_000003",
    }
    assert "input_snapshots" not in snapshot
    assert "input_snapshot_namespace" not in snapshot
    execution_dir = Path(snapshot["execution_dir"])
    assert execution_dir.parent == job_dir.resolve()
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}", execution_dir.name)
    assert all(
        Path(identity["path"]).parent == execution_dir
        for identity in snapshot["materialized_inputs"].values()
    )
    assert {
        role: Path(identity["path"]).name
        for role, identity in snapshot["materialized_inputs"].items()
    } == {
        "dependency_000000": "charges.pc",
        "dependency_000001": "guess.gbw",
        "dependency_000002": "initial.hess",
        "dependency_000003": "input.xyz",
    }
    assert not (job_dir / ".orca_auto_orca_executions").exists()
    assert not (job_dir / ".orca_auto_input_snapshots").exists()
    assert (
        job_dir / ".orca_auto_snapshot_intents" / f"{snapshot['snapshot_intent_token']}.json"
    ).is_file()
    bound_text = verified_selected.read_text(encoding="utf-8")
    assert str(job_dir) not in bound_text
    assert "* xyzfile 0 1 input.xyz" in bound_text
    assert '%moinp "guess.gbw"' in bound_text
    assert '%pointcharges "charges.pc"' in bound_text
    assert 'InHessName "initial.hess"' in bound_text
    assert ".inputs/" not in bound_text


def test_visible_generation_name_contract() -> None:
    assert is_visible_generation_name("20260716-224400-3da546fd")
    assert not is_visible_generation_name("generation-20260716-224400-3da546fd")
    assert not is_visible_generation_name("20260716-224400-3DA546FD")
    assert not is_visible_generation_name("20260716-224400-3da546f")
    assert not is_visible_generation_name("20260716-224400-3da546fd0")
    assert not is_visible_generation_name("2026716-224400-3da546fd")
    assert not is_visible_generation_name("20260716-224400-3da546fd\n")
    assert not is_visible_generation_name("٢٠٢٦٠٧١٦-٢٢٤٤٠٠-3da546fd")


def test_orca_execution_snapshot_allows_same_stem_xyz_dependency(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    geometry = job_dir / "h2.xyz"
    geometry.write_text("2\nH2\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    selected = job_dir / "h2.inp"
    selected.write_text(
        "! HF STO-3G SP\n* xyzfile 0 1 h2.xyz\n",
        encoding="utf-8",
    )

    snapshot = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz=str(geometry.resolve()),
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        max_retries=0,
        orca_executable=_write_executable(tmp_path / "orca"),
    )

    generation = Path(snapshot["execution_dir"])
    assert {path.name for path in generation.iterdir()} == {"h2.inp", "h2.xyz"}
    assert Path(snapshot["materialized_inputs"]["dependency_000000"]["path"]) == (
        generation / "h2.xyz"
    )
    assert "* xyzfile 0 1 h2.xyz" in Path(snapshot["selected_inp"]).read_text(encoding="utf-8")


def test_orca_execution_snapshot_inlines_same_stem_xyz_for_optimization(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    geometry = job_dir / "h2.xyz"
    geometry.write_text("2\nH2\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    selected = job_dir / "h2.inp"
    selected.write_text("! HF STO-3G Opt\n* xyzfile 0 1 h2.xyz\n", encoding="utf-8")

    resources = {"max_cores": 1, "max_memory_gb": 1}
    snapshot = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz=str(geometry.resolve()),
        resource_request=resources,
        max_retries=0,
        orca_executable=_write_executable(tmp_path / "orca"),
    )

    generation = Path(snapshot["execution_dir"])
    bound_text = Path(snapshot["selected_inp"]).read_text(encoding="utf-8")
    assert "* xyzfile" not in bound_text
    assert "* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*" in bound_text
    assert snapshot["runtime_mutable_input_roles"] == ["dependency_000000"]
    runtime_xyz = generation / "h2.xyz"
    runtime_xyz.write_text("1\noptimized\nH 1 2 3\n", encoding="utf-8")

    with pytest.raises(ValueError, match="private dependency"):
        verify_orca_execution_snapshot(
            job_dir,
            snapshot,
            expected_selected_inp=snapshot["selected_inp"],
            expected_source_selected_inp=snapshot["source_selected_inp"],
            expected_selected_input_xyz=str(geometry.resolve()),
            expected_resource_request=resources,
            expected_max_retries=0,
        )
    verify_orca_execution_snapshot(
        job_dir,
        snapshot,
        expected_selected_inp=snapshot["selected_inp"],
        expected_source_selected_inp=snapshot["source_selected_inp"],
        expected_selected_input_xyz=str(geometry.resolve()),
        expected_resource_request=resources,
        expected_max_retries=0,
        allow_runtime_outputs=True,
    )


def test_orca_execution_snapshot_rejects_same_stem_hessian_for_frequency(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    hessian = job_dir / "h2.hess"
    hessian.write_text("$hessian\n1\n1.0\n$end\n", encoding="utf-8")
    selected = job_dir / "h2.inp"
    selected.write_text(
        '! HF STO-3G Freq\n%geom InHessName "h2.hess" end\n* xyz 0 1\nH 0 0 0\n*\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runtime/output file: h2.hess"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )

    assert not _visible_generations(job_dir)


@pytest.mark.parametrize(
    "dependency_name",
    ["h2.out", "h2.gbw", "job_state.json"],
)
def test_orca_execution_snapshot_rejects_generation_runtime_name_collisions(
    tmp_path: Path,
    dependency_name: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    dependency = job_dir / dependency_name
    dependency.write_bytes(b"runtime-name collision")
    selected = job_dir / "h2.inp"
    selected.write_text(
        f'%moinp "{dependency_name}"\n* xyz 0 1\nH 0 0 0\n*\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runtime/output file"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )

    assert not _visible_generations(job_dir)


@pytest.mark.parametrize(
    ("route", "max_retries", "dependency_name"),
    [
        ("! HF STO-3G SP", 0, "h2.resume.inp"),
        ("! HF STO-3G SP", 0, "h2.resume.out"),
        ("! HF STO-3G SP", 0, "h2.resume.gbw"),
        ("! HF STO-3G Freq", 0, "h2.resume.hess"),
        ("! HF STO-3G Opt", 0, "h2.resume.xyz"),
        ("! HF STO-3G ScanTS", 1, "h2.retry01.inp"),
        ("! HF STO-3G ScanTS", 1, "h2.retry01.out"),
        ("! HF STO-3G ScanTS", 1, "h2.retry03.gbw"),
        ("! HF STO-3G ScanTS", 1, "h2.retry03.xyz"),
        ("! HF STO-3G ScanTS Freq", 1, "h2.retry03.hess"),
        ("! HF STO-3G ScanTS", 1, "h2.retry02.resume.inp"),
        ("! HF STO-3G ScanTS", 1, "h2.retry02.resume.out"),
    ],
)
def test_orca_execution_snapshot_rejects_retry_and_resume_name_collisions(
    tmp_path: Path,
    route: str,
    max_retries: int,
    dependency_name: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    dependency = job_dir / dependency_name
    dependency.write_text("0\n", encoding="utf-8")
    selected = job_dir / "h2.inp"
    selected.write_text(
        f'{route}\n%pointcharges "{dependency_name}"\n* xyz 0 1\nH 0 0 0\n*\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runtime/output file"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=max_retries,
            orca_executable=_write_executable(tmp_path / "orca"),
        )

    assert not _visible_generations(job_dir)


def test_orca_execution_snapshot_allows_retry_name_outside_effective_budget(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    dependency = job_dir / "h2.retry04.out"
    dependency.write_text("0\n", encoding="utf-8")
    selected = job_dir / "h2.inp"
    selected.write_text(
        '! HF STO-3G ScanTS\n%pointcharges "h2.retry04.out"\n* xyz 0 1\nH 0 0 0\n*\n',
        encoding="utf-8",
    )

    snapshot = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz="",
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        max_retries=1,
        orca_executable=_write_executable(tmp_path / "orca"),
    )

    assert Path(snapshot["materialized_inputs"]["dependency_000000"]["path"]).name == (
        dependency.name
    )


def test_orca_execution_snapshot_creates_sequential_sibling_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orca_auto.orca.execution_binding as binding

    generation_names = iter(
        (
            "20260714-224054-959479f2",
            "20260714-224055-deadbeef",
        )
    )
    monkeypatch.setattr(binding, "new_visible_generation_name", lambda: next(generation_names))
    job_dir, selected, first, resources = _snapshot(tmp_path)
    second = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        resource_request=resources,
        max_retries=2,
        orca_executable=tmp_path / "orca",
    )

    first_dir = Path(first["execution_dir"])
    second_dir = Path(second["execution_dir"])
    assert first_dir != second_dir
    assert first_dir.parent == second_dir.parent == job_dir.resolve()
    assert {path.name for path in _visible_generations(job_dir)} == {
        "20260714-224054-959479f2",
        "20260714-224055-deadbeef",
    }
    assert first_dir.is_dir()
    assert second_dir.is_dir()
    _verify(job_dir, selected, first, resources)
    _verify(job_dir, selected, second, resources)


@pytest.mark.parametrize(
    ("reactant_payload", "product_payload"),
    [
        (
            "1\nsame endpoint\nH 0 0 0\n",
            "1\nsame endpoint\nH 0 0 0\n",
        ),
        (
            "1\nreactant\nH 0 0 0\n",
            "1\nproduct\nH 1 0 0\n",
        ),
    ],
    ids=("identical-content", "different-content"),
)
def test_orca_execution_snapshot_rejects_distinct_sources_with_same_basename_and_cleans_up(
    tmp_path: Path,
    reactant_payload: str,
    product_payload: str,
) -> None:
    job_dir, selected, reactant, product = _same_basename_neb_sources(
        tmp_path,
        reactant_payload=reactant_payload,
        product_payload=product_payload,
    )

    with pytest.raises(ValueError) as exc_info:
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz=str(reactant.resolve()),
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )

    message = str(exc_info.value)
    assert "different source paths use the same basename" in message
    assert "input.xyz" in message
    assert "product/input.xyz" in message
    assert "reactant/input.xyz" in message
    assert reactant.read_text(encoding="utf-8") == reactant_payload
    assert product.read_text(encoding="utf-8") == product_payload
    assert not _visible_generations(job_dir)
    assert not (job_dir / ".orca_auto_orca_executions").exists()
    assert not (job_dir / ".orca_auto_input_snapshots").exists()
    assert not list((job_dir / ".orca_auto_snapshot_intents").glob("*.json"))


def test_orca_execution_snapshot_allows_repeated_references_to_one_source_path(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    geometry = job_dir / "input.xyz"
    geometry.write_text("1\nsame source\nH 0 0 0\n", encoding="utf-8")
    selected = job_dir / "neb.inp"
    selected.write_text(
        '! NEB-TS\n%neb\n  Product "./input.xyz"\nend\n* xyzfile 0 1 input.xyz\n',
        encoding="utf-8",
    )

    snapshot = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz=str(geometry.resolve()),
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        max_retries=0,
        orca_executable=_write_executable(tmp_path / "orca"),
    )

    assert snapshot["dependency_paths"] == [str(geometry.resolve())]
    assert set(snapshot["materialized_inputs"]) == {"dependency_000000"}
    bound_text = Path(snapshot["selected_inp"]).read_text(encoding="utf-8")
    assert 'Product "input.xyz"' in bound_text
    assert "* xyzfile 0 1 input.xyz" in bound_text


@pytest.mark.parametrize(
    "neb_block",
    [
        '%neb\n  Product "output.xyz"\n  TS = "guessTS.xyz"\nend',
        '% neb Product "output.xyz" TS = "guessTS.xyz" end',
    ],
)
def test_orca_execution_snapshot_binds_official_neb_geometry_files(
    tmp_path: Path,
    neb_block: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for name, label in (
        ("input.xyz", "reactant"),
        ("output.xyz", "product"),
        ("guessTS.xyz", "TS guess"),
    ):
        (job_dir / name).write_text(f"1\n{label}\nH 0 0 0\n", encoding="utf-8")
    selected = job_dir / "neb.inp"
    selected.write_text(
        f"! NEB-TS HF STO-3G\n{neb_block}\n* xyzfile 0 1 input.xyz\n",
        encoding="utf-8",
    )

    snapshot = build_orca_execution_snapshot(
        job_dir,
        selected,
        selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        max_retries=0,
        orca_executable=_write_executable(tmp_path / "orca"),
    )

    assert snapshot["dependency_paths"] == [
        str((job_dir / "guessTS.xyz").resolve()),
        str((job_dir / "input.xyz").resolve()),
        str((job_dir / "output.xyz").resolve()),
    ]
    assert set(snapshot["materialized_inputs"]) == {
        "dependency_000000",
        "dependency_000001",
        "dependency_000002",
    }
    assert set(snapshot["source_inputs"]) == {
        "selected_source",
        "dependency_000000",
        "dependency_000001",
        "dependency_000002",
    }
    generation = Path(snapshot["execution_dir"])
    assert {path.name for path in generation.iterdir()} == {
        "neb.inp",
        "input.xyz",
        "output.xyz",
        "guessTS.xyz",
    }
    bound_text = Path(snapshot["selected_inp"]).read_text(encoding="utf-8")
    assert "* xyzfile 0 1 input.xyz" in bound_text
    assert 'Product "output.xyz"' in bound_text
    assert 'TS = "guessTS.xyz"' in bound_text
    assert ".inputs/" not in bound_text
    assert all(
        Path(identity["path"]).is_file() for identity in snapshot["materialized_inputs"].values()
    )


def test_orca_execution_snapshot_does_not_bind_product_or_ts_outside_neb_block(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected = job_dir / "job.inp"
    selected.write_text(
        "! SP TS Product\n%scf Product missing-product.xyz TS missing-ts.xyz end\n"
        "* xyz 0 1\nH 0 0 0\n*\n",
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


def test_orca_execution_snapshot_limits_neb_file_keys_to_end_boundary(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "product.xyz").write_text("1\nproduct\nH 0 0 0\n", encoding="utf-8")
    (job_dir / "guessTS.xyz").write_text("1\nTS guess\nH 0 0 0\n", encoding="utf-8")
    selected = job_dir / "job.inp"
    selected.write_text(
        '! NEB-TS\n%NEB Product = # kept # "product.xyz" TS "guessTS.xyz" end '
        'Product "missing-product.xyz" TS "missing-ts.xyz"\n'
        "%scf Product missing-scf-product.xyz TS missing-scf-ts.xyz end\n"
        "* xyz 0 1\nH 0 0 0\n*\n",
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

    assert snapshot["dependency_paths"] == [
        str((job_dir / "guessTS.xyz").resolve()),
        str((job_dir / "product.xyz").resolve()),
    ]
    bound_text = Path(snapshot["selected_inp"]).read_text(encoding="utf-8")
    assert '# kept # "product.xyz"' in bound_text
    assert 'TS "guessTS.xyz"' in bound_text
    assert 'Product "missing-product.xyz" TS "missing-ts.xyz"' in bound_text
    assert "%scf Product missing-scf-product.xyz TS missing-scf-ts.xyz end" in bound_text
    assert ".inputs/" not in bound_text


def test_orca_cleanup_rejects_a_mismatched_visible_generation(tmp_path: Path) -> None:
    import orca_auto.orca.execution_binding as binding

    job_dir, _selected, snapshot, _resources = _snapshot(tmp_path)
    foreign_namespace = "20000101-000000-deadbeef"
    foreign_generation = job_dir / foreign_namespace
    foreign_generation.mkdir()
    mismatched = dict(snapshot)
    mismatched["generation_name"] = foreign_namespace

    with pytest.raises(ValueError, match="mismatched"):
        binding.cleanup_unowned_orca_execution_snapshot(job_dir, mismatched)

    assert Path(snapshot["execution_dir"]).is_dir()
    assert foreign_generation.is_dir()


def test_orca_cleanup_rejects_same_name_replacement_even_if_inode_identity_matches(
    tmp_path: Path,
) -> None:
    from orca_auto.orca.execution_binding import cleanup_unowned_orca_execution_snapshot

    job_dir, _selected, snapshot, _resources = _snapshot(tmp_path)
    generation = Path(snapshot["execution_dir"])
    shutil.rmtree(generation)
    generation.mkdir()
    sentinel = generation / "sentinel.txt"
    sentinel.write_text("user replacement", encoding="utf-8")
    replacement_status = generation.stat()
    simulated_inode_reuse = dict(snapshot)
    simulated_inode_reuse["execution_dir_identity"] = {
        "device": replacement_status.st_dev,
        "inode": replacement_status.st_ino,
    }

    with pytest.raises(ValueError, match="owner identity"):
        cleanup_unowned_orca_execution_snapshot(job_dir, simulated_inode_reuse)

    assert sentinel.read_text(encoding="utf-8") == "user replacement"


def test_orca_execution_directory_collision_preserves_existing_generation(
    tmp_path: Path,
) -> None:
    import orca_auto.orca.execution_binding as binding

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    generation_name = "20260714-224054-959479f2"
    existing = job_dir / generation_name
    existing.mkdir()
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

    monkeypatch.setattr(binding, "cleanup_unowned_direct_generation_directory", fail_remove)
    with pytest.raises(OSError, match="simulated"):
        binding.cleanup_unowned_orca_execution_snapshot(job_dir, snapshot)

    assert Path(snapshot["execution_dir"]).is_dir()
    assert intent_path.is_file()


def test_orca_cleanup_does_not_follow_substituted_visible_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orca_auto.core.queue.engine.input_snapshot as input_snapshot
    import orca_auto.orca.execution_binding as binding

    job_dir, _selected, snapshot, _resources = _snapshot(tmp_path)
    execution_generation = Path(snapshot["execution_dir"])
    moved_generation = job_dir / "moved-execution-generation"
    outside_generation = tmp_path / "outside-execution-generation"
    outside_generation.mkdir()
    sentinel = outside_generation / "sentinel.txt"
    sentinel.write_text("must survive", encoding="utf-8")
    original_remove = input_snapshot._remove_directory_contents_at
    substituted = False

    def substitute_root(directory_fd: int, *, label: str) -> None:
        nonlocal substituted
        if not substituted and label == "ORCA execution snapshot generation":
            substituted = True
            execution_generation.rename(moved_generation)
            execution_generation.symlink_to(outside_generation, target_is_directory=True)
        original_remove(directory_fd, label=label)

    monkeypatch.setattr(input_snapshot, "_remove_directory_contents_at", substitute_root)

    with pytest.raises(ValueError, match="ORCA execution snapshot generation"):
        binding.cleanup_unowned_orca_execution_snapshot(job_dir, snapshot)

    assert substituted
    assert sentinel.read_text(encoding="utf-8") == "must survive"
    assert execution_generation.is_symlink()
    assert moved_generation.is_dir()


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
        '%moinp "first.gbw"\n%scf Guess MORead MOInp "second.gbw" end',
        '%scf MOInp "first.gbw" MOInp "second.gbw" end',
        '%scf\n  MOInp "first.gbw"\n  moinp = "second.gbw"\nend',
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
    assert '% moinp "guess.gbw"' in Path(snapshot["selected_inp"]).read_text(encoding="utf-8")
    assert Path(snapshot["materialized_inputs"]["dependency_000000"]["path"]).name == ("guess.gbw")


@pytest.mark.parametrize(
    "moread_directive",
    [
        "! SP MORead",
        "! SP\n%scf Guess MORead end",
        "! SP\n% SCF\n  GUESS   moread\nend",
    ],
)
def test_orca_execution_snapshot_rejects_moread_without_explicit_moinp(
    tmp_path: Path,
    moread_directive: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected = job_dir / "job.inp"
    selected.write_text(
        f"{moread_directive}\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="MORead requires an explicit MOInp"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )

    assert not any(
        child.is_dir() and is_visible_generation_name(child.name) for child in job_dir.iterdir()
    )


@pytest.mark.parametrize(
    "scf_block",
    [
        '%scf Guess MORead MOInp "guess.gbw" end',
        "% SCF\n  GUESS   moread\n  MOINP = 'guess.gbw'\nEND",
    ],
)
def test_orca_execution_snapshot_binds_scf_block_moinp(
    tmp_path: Path,
    scf_block: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "guess.gbw").write_bytes(b"checkpoint")
    selected = job_dir / "job.inp"
    selected.write_text(
        f"! SP\n{scf_block}\n* xyz 0 1\nH 0 0 0\n*\n",
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

    assert snapshot["dependency_paths"] == [str((job_dir / "guess.gbw").resolve())]
    assert Path(snapshot["materialized_inputs"]["dependency_000000"]["path"]).read_bytes() == (
        b"checkpoint"
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


def test_orca_execution_snapshot_rejects_unsafe_generated_xyzfile_path_and_cleans_up(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    geometry = job_dir / "geometry.xyz bad"
    geometry.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    selected = job_dir / "job.inp"
    selected.write_text(
        '! SP\n* xyzfile 0 1 "geometry.xyz bad"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsafe unquoted ORCA input path"):
        build_orca_execution_snapshot(
            job_dir,
            selected,
            selected_input_xyz=str(geometry.resolve()),
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            max_retries=0,
            orca_executable=_write_executable(tmp_path / "orca"),
        )

    assert not _visible_generations(job_dir)
    assert not (job_dir / ".orca_auto_orca_executions").exists()
    assert not (job_dir / ".orca_auto_input_snapshots").exists()
    assert not list((job_dir / ".orca_auto_snapshot_intents").glob("*.json"))


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
        '%base "safe_name"',
        '%base "../../escape"',
        '%neb\n  neb_restart_gbwname "../../escape"\nend',
        '%neb\n  restart_gbw_basename "elsewhere"\nend',
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


def test_orca_execution_snapshot_ignores_source_symlink_replacement_after_submission(
    tmp_path: Path,
) -> None:
    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    source_dependency = job_dir / "guess.gbw"
    source_dependency.unlink()
    source_dependency.symlink_to(job_dir / "charges.pc")

    verified_selected, _executable = _verify(job_dir, selected, snapshot, resources)

    assert verified_selected == Path(snapshot["selected_inp"])


def test_orca_execution_snapshot_rejects_private_dependency_mutation(tmp_path: Path) -> None:
    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    private_dependency = Path(snapshot["materialized_inputs"]["dependency_000000"]["path"])
    private_dependency.chmod(0o600)
    private_dependency.write_bytes(b"mutated")

    with pytest.raises(ValueError, match="private dependency"):
        _verify(job_dir, selected, snapshot, resources)


def test_orca_execution_snapshot_rejects_materialized_basename_metadata_tamper(
    tmp_path: Path,
) -> None:
    import orca_auto.orca.execution_binding as binding

    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    generation = Path(snapshot["execution_dir"])
    tampered = generation / "renamed.pc"
    tampered.write_bytes((job_dir / "charges.pc").read_bytes())
    snapshot["materialized_inputs"]["dependency_000000"] = binding._file_identity(tampered)

    with pytest.raises(ValueError, match="does not preserve its source basename"):
        _verify(job_dir, selected, snapshot, resources)

    assert generation.is_dir()


def test_verify_orca_execution_snapshot_rejects_resume_output_name_tamper(
    tmp_path: Path,
) -> None:
    import orca_auto.orca.execution_binding as binding

    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    role = "dependency_000000"
    original_private = Path(snapshot["materialized_inputs"][role]["path"])
    reserved_source = job_dir / "job.resume.out"
    reserved_private = Path(snapshot["execution_dir"]) / reserved_source.name
    original_private.rename(reserved_private)
    snapshot["dependency_paths"][0] = str(reserved_source.resolve())
    snapshot["source_inputs"][role]["source_path"] = str(reserved_source.resolve())
    snapshot["materialized_inputs"][role] = binding._file_identity(reserved_private)
    bound_selected = Path(snapshot["selected_inp"])
    bound_selected.chmod(0o600)
    bound_selected.write_text(
        bound_selected.read_text(encoding="utf-8").replace("charges.pc", reserved_source.name),
        encoding="utf-8",
    )
    bound_selected.chmod(0o400)
    snapshot["bound_selected_identity"] = binding._file_identity(bound_selected)

    with pytest.raises(ValueError, match="runtime/output file: job.resume.out"):
        _verify(job_dir, selected, snapshot, resources)


def test_verify_orca_execution_snapshot_rejects_distinct_sources_with_same_basename(
    tmp_path: Path,
) -> None:
    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    conflicting_source = job_dir / "nested" / "charges.pc"
    snapshot["dependency_paths"][1] = str(conflicting_source.resolve())
    snapshot["source_inputs"]["dependency_000001"]["source_path"] = str(
        conflicting_source.resolve()
    )

    with pytest.raises(ValueError) as exc_info:
        _verify(job_dir, selected, snapshot, resources)

    message = str(exc_info.value)
    assert "different source paths use the same basename" in message
    assert "charges.pc" in message
    assert "nested/charges.pc" in message


def test_verify_orca_execution_snapshot_rejects_duplicate_canonical_dependency_roles(
    tmp_path: Path,
) -> None:
    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    snapshot["dependency_paths"][1] = snapshot["dependency_paths"][0]
    snapshot["source_inputs"]["dependency_000001"] = {
        **snapshot["source_inputs"]["dependency_000000"],
        "role": "dependency_000001",
    }
    snapshot["materialized_inputs"]["dependency_000001"] = dict(
        snapshot["materialized_inputs"]["dependency_000000"]
    )

    with pytest.raises(ValueError, match="duplicate dependency source paths"):
        _verify(job_dir, selected, snapshot, resources)


def test_verify_orca_execution_snapshot_rejects_dependency_role_permutation(
    tmp_path: Path,
) -> None:
    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    snapshot["dependency_paths"][0], snapshot["dependency_paths"][1] = (
        snapshot["dependency_paths"][1],
        snapshot["dependency_paths"][0],
    )
    first_source = snapshot["source_inputs"]["dependency_000000"]
    second_source = snapshot["source_inputs"]["dependency_000001"]
    snapshot["source_inputs"]["dependency_000000"] = {
        **second_source,
        "role": "dependency_000000",
    }
    snapshot["source_inputs"]["dependency_000001"] = {
        **first_source,
        "role": "dependency_000001",
    }
    first_materialized = snapshot["materialized_inputs"]["dependency_000000"]
    second_materialized = snapshot["materialized_inputs"]["dependency_000001"]
    snapshot["materialized_inputs"]["dependency_000000"] = second_materialized
    snapshot["materialized_inputs"]["dependency_000001"] = first_materialized

    with pytest.raises(ValueError, match="canonical source path order"):
        _verify(job_dir, selected, snapshot, resources)


def test_verify_orca_execution_snapshot_rejects_selected_source_metadata_substitution(
    tmp_path: Path,
) -> None:
    job_dir, _selected, snapshot, resources = _snapshot(tmp_path)
    original_source = snapshot["source_selected_inp"]
    substituted_source = str((job_dir / "nested" / Path(original_source).name).resolve())
    snapshot["source_selected_inp"] = substituted_source
    snapshot["source_inputs"]["selected_source"]["source_path"] = substituted_source

    with pytest.raises(ValueError, match="does not match its queue metadata"):
        verify_orca_execution_snapshot(
            job_dir,
            snapshot,
            expected_selected_inp=snapshot["selected_inp"],
            expected_source_selected_inp=original_source,
            expected_selected_input_xyz=str((job_dir / "input.xyz").resolve()),
            expected_resource_request=resources,
            expected_max_retries=2,
        )


def test_verify_orca_execution_snapshot_rejects_selected_input_as_dependency(
    tmp_path: Path,
) -> None:
    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    omitted_private = Path(snapshot["materialized_inputs"]["dependency_000001"]["path"])
    snapshot["dependency_paths"][1] = snapshot["source_selected_inp"]
    snapshot["source_inputs"]["dependency_000001"] = {
        **snapshot["source_inputs"]["selected_source"],
        "role": "dependency_000001",
    }
    snapshot["materialized_inputs"]["dependency_000001"] = dict(snapshot["bound_selected_identity"])
    omitted_private.chmod(0o600)
    omitted_private.write_bytes(b"tampered but no longer listed")

    with pytest.raises(ValueError, match="lists its selected input as a dependency"):
        _verify(job_dir, selected, snapshot, resources)


def test_verify_orca_execution_snapshot_rejects_dependency_role_substitution(
    tmp_path: Path,
) -> None:
    import orca_auto.orca.execution_binding as binding

    job_dir, selected, snapshot, resources = _snapshot(tmp_path)
    role = "dependency_000001"
    original_private = Path(snapshot["materialized_inputs"][role]["path"])
    replacement_source = job_dir / "replacement.gbw"
    replacement_private = Path(snapshot["execution_dir"]) / replacement_source.name
    replacement_private.write_bytes(original_private.read_bytes())
    snapshot["dependency_paths"][1] = str(replacement_source.resolve())
    snapshot["source_inputs"][role]["source_path"] = str(replacement_source.resolve())
    snapshot["materialized_inputs"][role] = binding._file_identity(replacement_private)

    with pytest.raises(ValueError, match="bound input references do not match"):
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
            f'%pointcharges "input.xyz" # {index}'
            for index in range(input_references.MAX_ORCA_INPUT_REFERENCES + 1)
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

    assert not _visible_generations(job_dir)
    assert not (job_dir / ".orca_auto_orca_executions").exists()
    assert not (job_dir / ".orca_auto_input_snapshots").exists()
    assert not list((job_dir / ".orca_auto_snapshot_intents").glob("*.json"))


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

    assert not _visible_generations(job_dir)
    assert not (job_dir / ".orca_auto_orca_executions").exists()
    assert not (job_dir / ".orca_auto_input_snapshots").exists()
    assert not list((job_dir / ".orca_auto_snapshot_intents").glob("*.json"))


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


@pytest.mark.parametrize("neb_key", ["Product", "TS"])
def test_orca_execution_snapshot_rejects_neb_geometry_above_atom_cap(
    tmp_path: Path,
    neb_key: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "input.xyz").write_text("1\nreactant\nH 0 0 0\n", encoding="utf-8")
    (job_dir / "oversized.xyz").write_text(
        f"{MAX_ADMISSION_ATOMS + 1}\noversized\n",
        encoding="utf-8",
    )
    selected = job_dir / "job.inp"
    selected.write_text(
        f'! NEB-TS\n%neb\n  {neb_key} "oversized.xyz"\nend\n* xyzfile 0 1 input.xyz\n',
        encoding="utf-8",
    )

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
