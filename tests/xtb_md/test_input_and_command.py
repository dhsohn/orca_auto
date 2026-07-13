from __future__ import annotations

import pytest

from orca_auto.xtb_md.command import build_xtb_md_command
from orca_auto.xtb_md.input_builder import build_md_input


def test_build_md_input_is_canonical_fresh_and_deterministic(manifest_job) -> None:
    _job_dir, manifest = manifest_job()

    assert build_md_input(manifest) == (
        "$samerand\n"
        "$md\n"
        "  temp=298.15\n"
        "  time=0.008\n"
        "  dump=4\n"
        "  step=4\n"
        "  velo=false\n"
        "  nvt=true\n"
        "  restart=false\n"
        "  hmass=4\n"
        "  shake=2\n"
        "  sccacc=2\n"
        "  forcewrrestart=true\n"
        "$end\n"
    )


def test_build_md_input_renders_nve_without_exposing_seed_or_resume(
    manifest_job,
    valid_manifest_payload,
) -> None:
    _job_dir, manifest = manifest_job({**valid_manifest_payload, "ensemble": "nve"})
    rendered = build_md_input(manifest)

    assert "nvt=false" in rendered
    assert "$samerand" in rendered
    assert "restart=false" in rendered
    assert "seed=" not in rendered


def test_build_command_has_one_shell_free_no_restart_path(manifest_job) -> None:
    job_dir, manifest = manifest_job()
    command = build_xtb_md_command(
        executable="/opt/xtb/bin/xtb",
        input_xyz=job_dir / "snapshot.xyz",
        md_input=job_dir / "md.inp",
        manifest=manifest,
        max_cores=2,
    )

    assert command == (
        "/opt/xtb/bin/xtb",
        str(job_dir / "snapshot.xyz"),
        "--input",
        str(job_dir / "md.inp"),
        "--md",
        "--gfn",
        "2",
        "--chrg",
        "0",
        "--uhf",
        "0",
        "--parallel",
        "2",
        "--norestart",
        "--strict",
    )
    assert "--omd" not in command


def test_build_command_adds_only_validated_solvent(manifest_job, valid_manifest_payload) -> None:
    job_dir, manifest = manifest_job(
        {**valid_manifest_payload, "solvent_model": "alpb", "solvent": "water"}
    )
    command = build_xtb_md_command(
        executable="/opt/xtb",
        input_xyz=job_dir / "snapshot.xyz",
        md_input=job_dir / "md.inp",
        manifest=manifest,
        max_cores=1,
    )

    assert command[-2:] == ("--alpb", "water")


@pytest.mark.parametrize(
    ("field", "value"),
    [("executable", "xtb"), ("input_xyz", "input.xyz"), ("md_input", "md.inp")],
)
def test_build_command_rejects_relative_paths(manifest_job, field: str, value: str) -> None:
    job_dir, manifest = manifest_job()
    arguments = {
        "executable": "/opt/xtb",
        "input_xyz": job_dir / "input.xyz",
        "md_input": job_dir / "md.inp",
        "manifest": manifest,
        "max_cores": 1,
    }
    arguments[field] = value

    with pytest.raises(ValueError, match="absolute path"):
        build_xtb_md_command(**arguments)


@pytest.mark.parametrize("cores", [0, True, 1.5])
def test_build_command_rejects_invalid_core_count(manifest_job, cores) -> None:
    job_dir, manifest = manifest_job()
    with pytest.raises(ValueError, match="positive integer"):
        build_xtb_md_command(
            executable="/opt/xtb",
            input_xyz=job_dir / "input.xyz",
            md_input=job_dir / "md.inp",
            manifest=manifest,
            max_cores=cores,
        )
