from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto.core.utils import normalize_text
from orca_auto.flow.adapters.orca import load_orca_artifact_contract
from orca_auto.flow.orchestration.stage_views import WorkflowTaskView
from orca_auto.flow.restart.orca_input import rematerialize_orca_restart_input
from orca_auto.orca.input_blocks import set_block_key_value
from orca_auto.orca.resource_directives import read_maxcore, read_nprocs


def _restart_stage(reaction_dir: Path) -> dict[str, object]:
    selected_inp = reaction_dir / "input.inp"
    selected_xyz = reaction_dir / "input.xyz"
    return {
        "stage_id": "orca_01",
        "metadata": {"reaction_dir": str(reaction_dir)},
        "task": {
            "engine": "orca",
            "payload": {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
                "selected_input_xyz": str(selected_xyz),
            },
            "metadata": {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
            },
            "enqueue_payload": {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
                "force": False,
                "command": f"orca_auto run-dir '{reaction_dir}' --priority 10",
                "command_argv": [
                    "python",
                    "-m",
                    "orca_auto",
                    "run-dir",
                    str(reaction_dir),
                    "--priority",
                    "10",
                ],
            },
        },
    }


def test_rematerialize_orca_restart_input_advances_generation_and_repoints_command(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    (original / "input.xyz").write_text(
        "2\nsource\nH 0 0 0\nH 0 0 0.74\n",
        encoding="utf-8",
    )
    (original / "input.inp").write_text(
        "# provenance # ! OLD Opt\n%pal\n  nprocs 1\nend\n%maxcore 1024\n* xyzfile 0 1 input.xyz\n",
        encoding="utf-8",
    )
    stage = _restart_stage(original)
    settings = {
        "orca_input_updates": True,
        "orca_route_line_present": True,
        "orca_route_line": "! NEW Opt",
        "orca_charge": -1,
        "orca_multiplicity": 2,
        "resources": {"max_cores": 4, "max_memory_gb": 8},
    }

    assert rematerialize_orca_restart_input(stage, settings, allowed_root=tmp_path) is True
    first = tmp_path / "orca_stage.restart-001"
    (first / "input.out").write_text("first output", encoding="utf-8")
    (first / "job_state.json").write_text('{"first": true}', encoding="utf-8")

    assert rematerialize_orca_restart_input(stage, settings, allowed_root=tmp_path) is True
    second = tmp_path / "orca_stage.restart-002"
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    enqueue_payload = task["enqueue_payload"]
    assert isinstance(payload, dict)
    assert isinstance(enqueue_payload, dict)
    assert payload["reaction_dir"] == str(second)
    assert enqueue_payload["reaction_dir"] == str(second)
    assert enqueue_payload["command_argv"][4] == str(second)
    assert str(second) in enqueue_payload["command"]
    assert str(first) not in enqueue_payload["command"]
    assert enqueue_payload["force"] is True

    assert (first / "input.out").read_text(encoding="utf-8") == "first output"
    assert not (second / "input.out").exists()
    assert not (second / "job_state.json").exists()
    restarted_input = (second / "input.inp").read_text(encoding="utf-8")
    assert "! NEW Opt" in restarted_input
    assert "OLD Opt" not in restarted_input
    provenance = json.loads((second / "source_candidate.json").read_text())["restart_provenance"]
    assert provenance["previous_reaction_dir"] == str(first)
    persisted_enqueue = json.loads((second / "enqueue_payload.json").read_text())
    assert persisted_enqueue == enqueue_payload


def test_rematerialize_orca_restart_input_copies_safe_relative_auxiliary_file(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    checkpoint = original / "checkpoints" / "seed.gbw"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"checkpoint")
    (original / "input.xyz").write_text("1\nsource\nH 0 0 0\n", encoding="utf-8")
    (original / "input.inp").write_text(
        '! OLD MOREAD\n%moinp "checkpoints/seed.gbw"\n* xyzfile 0 1 input.xyz\n',
        encoding="utf-8",
    )
    stage = _restart_stage(original)

    assert rematerialize_orca_restart_input(
        stage,
        {
            "orca_input_updates": True,
            "orca_route_line_present": True,
            "orca_route_line": "! NEW MOREAD",
        },
        allowed_root=tmp_path,
    )

    restarted = tmp_path / "orca_stage.restart-001"
    assert (restarted / "checkpoints" / "seed.gbw").read_bytes() == b"checkpoint"
    assert '%moinp "checkpoints/seed.gbw"' in (restarted / "input.inp").read_text()


def test_rematerialize_orca_restart_input_preserves_nested_geometry_path(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    selected_xyz = original / "coords" / "selected.xyz"
    selected_xyz.parent.mkdir(parents=True)
    selected_xyz.write_text("1\nsource\nH 0 0 0\n", encoding="utf-8")
    (original / "input.inp").write_text(
        "! OLD\n* xyzfile 0 1 coords/selected.xyz\n",
        encoding="utf-8",
    )
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    assert isinstance(payload, dict)
    payload["selected_input_xyz"] = str(selected_xyz)

    assert rematerialize_orca_restart_input(
        stage,
        {"orca_input_updates": True},
        allowed_root=tmp_path,
    )

    restarted = tmp_path / "orca_stage.restart-001"
    restarted_xyz = restarted / "coords" / "selected.xyz"
    assert restarted_xyz.read_text(encoding="utf-8") == selected_xyz.read_text(encoding="utf-8")
    assert "* xyzfile 0 1 coords/selected.xyz" in (restarted / "input.inp").read_text()
    assert payload["selected_input_xyz"] == str(restarted_xyz)


def test_rematerialize_orca_restart_input_keeps_distinct_nested_geometry_and_auxiliary(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    selected_xyz = original / "coords" / "shared.xyz"
    selected_xyz.parent.mkdir(parents=True)
    selected_xyz.write_text("1\ngeometry\nH 0 0 0\n", encoding="utf-8")
    (original / "shared.xyz").write_text("POINT-CHARGE-DATA\n", encoding="utf-8")
    (original / "input.inp").write_text(
        '! OLD\n%pointcharges "shared.xyz"\n* xyzfile 0 1 coords/shared.xyz\n',
        encoding="utf-8",
    )
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    assert isinstance(payload, dict)
    payload["selected_input_xyz"] = str(selected_xyz)

    # Nested geometry paths remain nested, so this formerly destructive
    # basename collision is now materialized without either file overwriting
    # the other.
    assert rematerialize_orca_restart_input(
        stage,
        {"orca_input_updates": True},
        allowed_root=tmp_path,
    )
    restarted = tmp_path / "orca_stage.restart-001"
    assert (restarted / "coords" / "shared.xyz").read_text() == selected_xyz.read_text()
    assert (restarted / "shared.xyz").read_text() == "POINT-CHARGE-DATA\n"
    assert "* xyzfile 0 1 coords/shared.xyz" in (restarted / "input.inp").read_text()


def test_rematerialize_orca_restart_input_preserves_nested_input_relative_auxiliary(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    geometry = original / "coords" / "shared.xyz"
    geometry.parent.mkdir(parents=True)
    geometry.write_text("1\ngeometry\nH 0 0 0\n", encoding="utf-8")
    nested = original / "nested"
    auxiliary = nested / "coords" / "shared.xyz"
    auxiliary.parent.mkdir(parents=True)
    auxiliary.write_text("POINT-CHARGE-DATA\n", encoding="utf-8")
    selected_inp = nested / "input.inp"
    selected_inp.write_text(
        '%pointcharges "coords/shared.xyz"\n* xyzfile 0 1 ../coords/shared.xyz\n',
        encoding="utf-8",
    )
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    enqueue_payload = task["enqueue_payload"]
    assert isinstance(payload, dict)
    assert isinstance(enqueue_payload, dict)
    payload["selected_inp"] = str(selected_inp)
    payload["selected_input_xyz"] = str(geometry)
    enqueue_payload["selected_inp"] = str(selected_inp)

    assert rematerialize_orca_restart_input(
        stage,
        {"orca_input_updates": True},
        allowed_root=tmp_path,
    )

    restarted = tmp_path / "orca_stage.restart-001"
    assert (restarted / "coords" / "shared.xyz").read_text() == geometry.read_text()
    assert (restarted / "nested" / "coords" / "shared.xyz").read_text() == auxiliary.read_text()
    restarted_input = (restarted / "input.inp").read_text()
    assert '%pointcharges "nested/coords/shared.xyz"' in restarted_input
    assert "* xyzfile 0 1 coords/shared.xyz" in restarted_input


def test_rematerialize_orca_restart_input_allows_nested_input_parent_auxiliary(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    checkpoint = original / "checkpoints" / "seed.gbw"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"checkpoint")
    nested = original / "nested"
    nested.mkdir()
    selected_inp = nested / "input.inp"
    selected_inp.write_text(
        '%moinp "../checkpoints/seed.gbw"\n* xyz 0 1\nH 0 0 0\n*\n',
        encoding="utf-8",
    )
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    enqueue_payload = task["enqueue_payload"]
    assert isinstance(payload, dict)
    assert isinstance(enqueue_payload, dict)
    payload["selected_inp"] = str(selected_inp)
    payload["selected_input_xyz"] = ""
    enqueue_payload["selected_inp"] = str(selected_inp)

    assert rematerialize_orca_restart_input(
        stage,
        {"orca_input_updates": True},
        allowed_root=tmp_path,
    )

    restarted = tmp_path / "orca_stage.restart-001"
    assert (restarted / "checkpoints" / "seed.gbw").read_bytes() == b"checkpoint"
    assert '%moinp "checkpoints/seed.gbw"' in (restarted / "input.inp").read_text()


def test_rematerialize_orca_restart_input_allows_same_source_copy_target(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    geometry = original / "coords" / "shared.xyz"
    geometry.parent.mkdir(parents=True)
    geometry.write_text("1\ngeometry\nH 0 0 0\n", encoding="utf-8")
    (original / "input.inp").write_text(
        '%geom neb_end_xyzfile "coords/shared.xyz" end\n* xyzfile 0 1 coords/shared.xyz\n',
        encoding="utf-8",
    )
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    assert isinstance(payload, dict)
    payload["selected_input_xyz"] = str(geometry)

    assert rematerialize_orca_restart_input(
        stage,
        {"orca_input_updates": True},
        allowed_root=tmp_path,
    )

    restarted_geometry = tmp_path / "orca_stage.restart-001" / "coords" / "shared.xyz"
    assert restarted_geometry.read_text() == geometry.read_text()


def test_rematerialize_orca_restart_input_copies_official_neb_geometry_files(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    (original / "input.xyz").write_text("1\nreactant\nH 0 0 0\n", encoding="utf-8")
    product = original / "endpoints" / "product.xyz"
    product.parent.mkdir()
    product.write_text("1\nproduct\nH 0 0 0\n", encoding="utf-8")
    guess = original / "guesses" / "guessTS.xyz"
    guess.parent.mkdir()
    guess.write_text("1\nTS guess\nH 0 0 0\n", encoding="utf-8")
    (original / "input.inp").write_text(
        '! NEB-TS\n%neb\n  Product "endpoints/product.xyz"\n'
        '  TS = "guesses/guessTS.xyz"\nend\n* xyzfile 0 1 input.xyz\n',
        encoding="utf-8",
    )

    assert rematerialize_orca_restart_input(
        _restart_stage(original),
        {"orca_input_updates": True},
        allowed_root=tmp_path,
    )

    restarted = tmp_path / "orca_stage.restart-001"
    assert (restarted / "endpoints" / "product.xyz").read_bytes() == product.read_bytes()
    assert (restarted / "guesses" / "guessTS.xyz").read_bytes() == guess.read_bytes()
    restarted_text = (restarted / "input.inp").read_text(encoding="utf-8")
    assert 'Product "endpoints/product.xyz"' in restarted_text
    assert 'TS = "guesses/guessTS.xyz"' in restarted_text


@pytest.mark.parametrize(
    "reserved_name",
    ["input.inp", "source_candidate.json", "enqueue_payload.json"],
)
def test_rematerialize_orca_restart_input_rejects_generated_file_collision(
    tmp_path: Path,
    reserved_name: str,
) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    (original / "input.inp").write_text(
        f'%pointcharges "{reserved_name}"\n* xyz 0 1\nH 0 0 0\n*\n',
        encoding="utf-8",
    )
    if reserved_name != "input.inp":
        (original / reserved_name).write_text("{}\n", encoding="utf-8")
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    assert isinstance(payload, dict)
    payload["selected_input_xyz"] = ""

    with pytest.raises(ValueError, match="copy target collision"):
        rematerialize_orca_restart_input(
            stage,
            {"orca_input_updates": True},
            allowed_root=tmp_path,
        )

    assert not (tmp_path / "orca_stage.restart-001").exists()


def test_rematerialize_orca_restart_input_copies_inline_geom_auxiliary(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    hessian = original / "hessian files" / "seed.hess"
    hessian.parent.mkdir(parents=True)
    hessian.write_text("HESSIAN-DATA\n", encoding="utf-8")
    (original / "input.xyz").write_text("1\nsource\nH 0 0 0\n", encoding="utf-8")
    (original / "input.inp").write_text(
        '%geom InHess Read InHessName "hessian files/seed.hess" end\n* xyzfile 0 1 input.xyz\n',
        encoding="utf-8",
    )

    assert rematerialize_orca_restart_input(
        _restart_stage(original),
        {"orca_input_updates": True},
        allowed_root=tmp_path,
    )

    restarted = tmp_path / "orca_stage.restart-001"
    assert (restarted / "hessian files" / "seed.hess").read_text() == "HESSIAN-DATA\n"
    assert (
        '%geom InHess Read InHessName "hessian files/seed.hess" end'
        in (restarted / "input.inp").read_text()
    )


def test_rematerialize_orca_restart_input_rejects_auxiliary_escape(tmp_path: Path) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    outside = tmp_path / "outside.gbw"
    outside.write_bytes(b"checkpoint")
    (original / "input.xyz").write_text("1\nsource\nH 0 0 0\n", encoding="utf-8")
    (original / "input.inp").write_text(
        '! OLD MOREAD\n%moinp "../outside.gbw"\n* xyzfile 0 1 input.xyz\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="escapes the reaction directory"):
        rematerialize_orca_restart_input(
            _restart_stage(original),
            {
                "orca_input_updates": True,
                "orca_route_line_present": True,
                "orca_route_line": "! NEW MOREAD",
            },
            allowed_root=tmp_path,
        )

    assert not (tmp_path / "orca_stage.restart-001").exists()


def test_rematerialize_orca_restart_input_rejects_selected_input_escape(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    outside_inp = tmp_path / "outside.inp"
    outside_inp.write_text("! OLD\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    enqueue_payload = task["enqueue_payload"]
    assert isinstance(payload, dict)
    assert isinstance(enqueue_payload, dict)
    payload["selected_inp"] = str(outside_inp)
    payload["selected_input_xyz"] = ""
    enqueue_payload["selected_inp"] = str(outside_inp)

    with pytest.raises(ValueError, match="selected input escapes the reaction directory"):
        rematerialize_orca_restart_input(
            stage,
            {"orca_input_updates": True},
            allowed_root=tmp_path,
        )

    assert not (tmp_path / "orca_stage.restart-001").exists()


def test_rematerialize_orca_restart_input_rejects_reaction_dir_outside_allowed_root(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "workflow"
    allowed_root.mkdir()
    outside = tmp_path / "outside_stage"
    outside.mkdir()
    (outside / "input.inp").write_text(
        "! OLD\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )
    stage = _restart_stage(outside)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    assert isinstance(payload, dict)
    payload["selected_input_xyz"] = ""

    with pytest.raises(ValueError, match="reaction directory escapes"):
        rematerialize_orca_restart_input(
            stage,
            {"orca_input_updates": True},
            allowed_root=allowed_root,
        )

    assert not (tmp_path / "outside_stage.restart-001").exists()


def test_rematerialize_orca_restart_input_rejects_source_metadata_symlink_escape(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    (original / "input.inp").write_text(
        "! OLD\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )
    outside_source = tmp_path / "outside_source.json"
    outside_source.write_text('{"secret": "must-not-copy"}', encoding="utf-8")
    (original / "source_candidate.json").symlink_to(outside_source)
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    assert isinstance(payload, dict)
    payload["selected_input_xyz"] = ""

    with pytest.raises(ValueError, match="source metadata escapes"):
        rematerialize_orca_restart_input(
            stage,
            {"orca_input_updates": True},
            allowed_root=tmp_path,
        )

    assert not (tmp_path / "orca_stage.restart-001").exists()


def test_rematerialize_orca_restart_input_replaces_all_simple_input_lines(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    (original / "input.inp").write_text(
        "! B3LYP\n! def2-SVP Opt\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    assert isinstance(payload, dict)
    payload["selected_input_xyz"] = ""

    assert rematerialize_orca_restart_input(
        stage,
        {
            "orca_input_updates": True,
            "orca_route_line_present": True,
            "orca_route_line": "! PBE0 def2-TZVP",
        },
        allowed_root=tmp_path,
    )

    restarted_lines = (tmp_path / "orca_stage.restart-001" / "input.inp").read_text().splitlines()
    assert [line for line in restarted_lines if line.strip().startswith("!")] == [
        "! PBE0 def2-TZVP"
    ]


def test_rematerialize_orca_restart_input_updates_inline_pal_and_uses_safe_xyzfile_path(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    xyz = original / "coords with space.xyz"
    inp = original / "input.inp"
    xyz.write_text("1\nsource\nH 0 0 0\n", encoding="utf-8")
    inp.write_text(
        '! OLD\n%pal nprocs 1 end\n%maxcore 1024\n* xyzfile 0 1 "coords with space.xyz"\n',
        encoding="utf-8",
    )
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    assert isinstance(payload, dict)
    payload["selected_input_xyz"] = str(xyz)

    assert rematerialize_orca_restart_input(
        stage,
        {
            "orca_input_updates": True,
            "resources": {"max_cores": 7, "max_memory_gb": 21},
        },
        allowed_root=tmp_path,
    )

    restarted_text = (tmp_path / "orca_stage.restart-001" / "input.inp").read_text()
    assert "%pal nprocs 7 end" in restarted_text
    assert "\n  nprocs 7\n" not in restarted_text
    assert "* xyzfile 0 1 .orca_auto_inputs/geometry.xyz" in restarted_text
    assert '"coords with space.xyz"' not in restarted_text
    safe_geometry = tmp_path / "orca_stage.restart-001" / ".orca_auto_inputs" / "geometry.xyz"
    assert safe_geometry.read_text() == xyz.read_text()


def test_rematerialize_orca_restart_input_rejects_reserved_geometry_collision(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    geometry = original / "coords with space.xyz"
    geometry.write_text("1\ngeometry\nH 0 0 0\n", encoding="utf-8")
    auxiliary = original / ".orca_auto_inputs" / "geometry.xyz"
    auxiliary.parent.mkdir()
    auxiliary.write_text("POINT-CHARGE-DATA\n", encoding="utf-8")
    (original / "input.inp").write_text(
        '%pointcharges ".orca_auto_inputs/geometry.xyz"\n* xyzfile 0 1 "coords with space.xyz"\n',
        encoding="utf-8",
    )
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    assert isinstance(payload, dict)
    payload["selected_input_xyz"] = str(geometry)

    with pytest.raises(ValueError, match="copy target collision"):
        rematerialize_orca_restart_input(
            stage,
            {"orca_input_updates": True},
            allowed_root=tmp_path,
        )

    assert not (tmp_path / "orca_stage.restart-001").exists()


@pytest.mark.parametrize(
    "auxiliary_relative_path",
    [
        Path(".orca_auto_inputs/geometry.xyz/charges.pc"),
        Path(".orca_auto_inputs"),
    ],
)
def test_rematerialize_orca_restart_input_rejects_reserved_geometry_path_tree_collision(
    tmp_path: Path,
    auxiliary_relative_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    geometry = original / "coords with space.xyz"
    geometry.write_text("1\ngeometry\nH 0 0 0\n", encoding="utf-8")
    auxiliary = original / auxiliary_relative_path
    auxiliary.parent.mkdir(parents=True, exist_ok=True)
    auxiliary.write_text("POINT-CHARGE-DATA\n", encoding="utf-8")
    (original / "input.inp").write_text(
        f'%pointcharges "{auxiliary_relative_path.as_posix()}"\n'
        '* xyzfile 0 1 "coords with space.xyz"\n',
        encoding="utf-8",
    )
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    assert isinstance(payload, dict)
    payload["selected_input_xyz"] = str(geometry)

    with pytest.raises(ValueError, match="copy target collision"):
        rematerialize_orca_restart_input(
            stage,
            {"orca_input_updates": True},
            allowed_root=tmp_path,
        )

    assert not (tmp_path / "orca_stage.restart-001").exists()


def test_rematerialize_orca_restart_input_parses_commented_xyzfile_fallback(
    tmp_path: Path,
) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    geometry = original / "input.xyz"
    geometry.write_text("1\ngeometry\nH 0 0 0\n", encoding="utf-8")
    (original / "input.inp").write_text(
        "! OLD\n* xyzfile 0 1 input.xyz # reactant geometry\n",
        encoding="utf-8",
    )
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    assert isinstance(payload, dict)
    payload["selected_input_xyz"] = ""

    assert rematerialize_orca_restart_input(
        stage,
        {"orca_input_updates": True},
        allowed_root=tmp_path,
    )

    restart_dir = tmp_path / "orca_stage.restart-001"
    assert (restart_dir / "input.xyz").read_text(encoding="utf-8") == geometry.read_text(
        encoding="utf-8"
    )
    assert "* xyzfile 0 1 input.xyz" in (restart_dir / "input.inp").read_text(encoding="utf-8")


def test_set_block_key_value_updates_hybrid_pal_start_line_without_duplicate() -> None:
    lines = [
        "! OLD",
        "%pal nprocs 1",
        "end",
        "* xyz 0 1",
        "H 0 0 0",
        "*",
    ]

    assert set_block_key_value(lines, "pal", "nprocs", "7")

    assert lines[1] == "%pal nprocs 7"
    assert "  nprocs 7" not in lines
    assert read_nprocs(lines) == 7


def test_resource_directives_parse_active_text_after_closed_comments() -> None:
    lines = [
        "# ordinary ! PAL999 %maxcore 999999",
        "# hidden # ! PAL7",
        "# hidden # %maxcore 2048",
    ]

    assert read_nprocs(lines) == 7
    assert read_maxcore(lines) == 2048


def test_rematerialize_orca_restart_input_preserves_inline_geometry(tmp_path: Path) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    inp = original / "input.inp"
    inp.write_text(
        "! OLD\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )
    stage = _restart_stage(original)
    task = stage["task"]
    assert isinstance(task, dict)
    payload = task["payload"]
    assert isinstance(payload, dict)
    payload["selected_input_xyz"] = ""

    assert rematerialize_orca_restart_input(
        stage,
        {
            "orca_input_updates": True,
            "orca_charge": -1,
            "orca_multiplicity": 2,
        },
        allowed_root=tmp_path,
    )

    restarted_text = (tmp_path / "orca_stage.restart-001" / "input.inp").read_text()
    assert "* xyz -1 2\nH 0 0 0\nH 0 0 0.74\n*" in restarted_text


def test_pending_generation_contract_keeps_inp_for_repeated_restart(tmp_path: Path) -> None:
    original = tmp_path / "orca_stage"
    original.mkdir()
    (original / "input.xyz").write_text("1\nsource\nH 0 0 0\n", encoding="utf-8")
    (original / "input.inp").write_text("! OLD\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")
    stage = _restart_stage(original)
    settings = {
        "orca_input_updates": True,
        "orca_route_line_present": True,
        "orca_route_line": "! NEW",
    }
    assert rematerialize_orca_restart_input(stage, settings, allowed_root=tmp_path)
    first = tmp_path / "orca_stage.restart-001"
    first_inp = first / "input.inp"
    first_xyz = first / "input.xyz"
    (tmp_path / "queue.json").write_text(
        json.dumps(
            [
                {
                    "queue_id": "q_new",
                    "task_id": "job_new",
                    "status": "pending",
                    "metadata": {
                        "reaction_dir": str(first),
                        "selected_inp": str(first_inp),
                        "selected_input_xyz": str(first_xyz),
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "job_locations.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "job_new",
                    "app_name": "orca_auto_orca",
                    "job_type": "orca_opt",
                    "status": "queued",
                    "original_run_dir": str(first),
                    "latest_known_path": str(first),
                    "selected_input_xyz": str(first_xyz),
                }
            ]
        ),
        encoding="utf-8",
    )
    contract = load_orca_artifact_contract(
        target=str(first),
        orca_allowed_root=tmp_path,
        queue_id="q_new",
        reaction_dir=str(first),
    )
    task = stage["task"]
    assert isinstance(task, dict)
    WorkflowTaskView(task).update_orca_contract_payload(contract, normalize_text)
    payload = task["payload"]
    assert isinstance(payload, dict)
    assert payload["selected_inp"] == str(first_inp)

    assert rematerialize_orca_restart_input(stage, settings, allowed_root=tmp_path)
    assert (tmp_path / "orca_stage.restart-002" / "input.inp").exists()
