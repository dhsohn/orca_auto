from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from orca_auto.xtb_md.manifest import XtbMdManifestLimits, load_manifest, load_xyz_geometry


def _write_job(job_dir: Path, payload: dict[str, Any], xyz: str | None = None) -> None:
    job_dir.mkdir()
    (job_dir / "water.xyz").write_text(
        xyz or "3\nwater\nO 0 0 0\nH 0.75 0 0.5\nH -0.75 0 0.5\n",
        encoding="utf-8",
    )
    (job_dir / "xtb_md_job.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )


def test_load_manifest_builds_strict_derived_budget(
    manifest_job,
) -> None:
    _job_dir, manifest = manifest_job()

    assert manifest.input_xyz.name == "water.xyz"
    assert manifest.atom_symbols == ("O", "H", "H")
    assert manifest.expected_steps == 2
    assert manifest.dump_interval_steps == 1
    assert manifest.expected_frames == 2
    assert manifest.atom_steps == 6
    assert manifest.estimated_trajectory_bytes == 2 * (1024 + 3 * 128)
    assert manifest.walltime_seconds == 300
    assert manifest.resources.max_cores == 2
    assert manifest.public_dict()["input_xyz"] == "water.xyz"


@pytest.mark.parametrize("field", ["retry", "restart", "seed", "xcontrol", "omd"])
def test_manifest_rejects_unsupported_control_surfaces(
    tmp_path: Path,
    valid_manifest_payload: dict[str, Any],
    manifest_limits: XtbMdManifestLimits,
    field: str,
) -> None:
    payload = {**valid_manifest_payload, field: True}
    _write_job(tmp_path / field, payload)

    with pytest.raises(ValueError, match="Unknown xTB-MD manifest"):
        load_manifest(tmp_path / field, limits=manifest_limits)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", True, "must be an integer"),
        ("time_ps", "0.008", "must be a number"),
        ("temperature_k", math.inf, "must be finite"),
        ("walltime_seconds", 0, "between 1"),
        ("ensemble", "NVT", "exactly 'nvt' or 'nve'"),
        ("gfn", "2", "must be an integer"),
        ("shake", 3, "exactly 0, 1, or 2"),
    ],
)
def test_manifest_rejects_noncanonical_or_unsafe_scalars(
    tmp_path: Path,
    valid_manifest_payload: dict[str, Any],
    manifest_limits: XtbMdManifestLimits,
    field: str,
    value: Any,
    message: str,
) -> None:
    payload = {**valid_manifest_payload, field: value}
    _write_job(tmp_path / field, payload)

    with pytest.raises(ValueError, match=message):
        load_manifest(tmp_path / field, limits=manifest_limits)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"time_ps": 0.007}, "integer multiple"),
        ({"dump_fs": 6.0}, "integer multiple"),
        ({"time_ps": 8.0}, "step limit"),
        ({"walltime_seconds": 3601}, "server limit"),
        ({"resources": {"max_cores": 9}}, "server limit"),
        ({"resources": {"threads": 2}}, "Unknown xTB-MD manifest resources"),
        ({"temperature_k": 1e308}, "must be between"),
        ({"step_fs": 1e308, "dump_fs": 1e308, "time_ps": 1e305}, "must be between"),
        ({"scc_accuracy": 1e308}, "must be between"),
        ({"hydrogen_mass_amu": 2**31 - 1}, "server limit"),
        ({"dump_fs": 12.0}, "must not exceed"),
        ({"time_ps": 1e308}, "step limit"),
        ({"charge": -(2**31)}, "server magnitude limit"),
        ({"uhf": 101}, "server limit"),
        (
            {"time_ps": 0.0003, "step_fs": 0.1, "dump_fs": 0.1},
            "not represented",
        ),
    ],
)
def test_manifest_enforces_cross_field_and_server_limits(
    tmp_path: Path,
    valid_manifest_payload: dict[str, Any],
    manifest_limits: XtbMdManifestLimits,
    updates: dict[str, Any],
    message: str,
) -> None:
    payload = {**valid_manifest_payload, **updates}
    _write_job(tmp_path / f"job-{len(list(tmp_path.iterdir()))}", payload)

    with pytest.raises(ValueError, match=message):
        load_manifest(next(tmp_path.iterdir()), limits=manifest_limits)


@pytest.mark.parametrize(
    ("xyz", "updates", "message"),
    [
        ("3\nwater\nO nan 0 0\nH 0 0 0\nH 0 0 0\n", {}, "non-finite"),
        ("3\nwater\nO 1e308 0 0\nH 0 0 0\nH 0 0 0\n", {}, "magnitude limit"),
        ("3\nwater\nO 1_0 0 0\nH 0 0 0\nH 0 0 0\n", {}, "invalid coordinate"),
        ("3\nwater\nO 0 0 0\nH 0 0 0\nXx 0 0 0\n", {}, "unsupported element"),
        ("2\nhydrogen\nH 0 0 0\nH 0 0 1\n1\nextra\nH 0 0 0\n", {}, "exactly one"),
        ("3\nwater\nO 0 0 0\nH 0 0 0\nH 0 0 0\n", {"charge": 1}, "parity"),
    ],
)
def test_manifest_rejects_invalid_geometry_or_electronic_state(
    tmp_path: Path,
    valid_manifest_payload: dict[str, Any],
    manifest_limits: XtbMdManifestLimits,
    xyz: str,
    updates: dict[str, Any],
    message: str,
) -> None:
    payload = {**valid_manifest_payload, **updates}
    job_dir = tmp_path / f"geometry-{len(list(tmp_path.iterdir()))}"
    _write_job(job_dir, payload, xyz)

    with pytest.raises(ValueError, match=message):
        load_manifest(job_dir, limits=manifest_limits)


def test_manifest_rejects_path_escape_and_input_symlink(
    tmp_path: Path,
    valid_manifest_payload: dict[str, Any],
    manifest_limits: XtbMdManifestLimits,
) -> None:
    escaped = {**valid_manifest_payload, "input_xyz": "../water.xyz"}
    escape_dir = tmp_path / "escape"
    _write_job(escape_dir, escaped)
    with pytest.raises(ValueError, match="one file name"):
        load_manifest(escape_dir, limits=manifest_limits)

    symlink_dir = tmp_path / "symlink"
    _write_job(symlink_dir, valid_manifest_payload)
    (symlink_dir / "water.xyz").unlink()
    outside = tmp_path / "outside.xyz"
    outside.write_text("1\nh\nH 0 0 0\n", encoding="utf-8")
    (symlink_dir / "water.xyz").symlink_to(outside)
    with pytest.raises(ValueError, match="must not be a symlink"):
        load_manifest(symlink_dir, limits=manifest_limits)


def test_public_geometry_validator_rechecks_immutable_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.xyz"
    snapshot.write_text("2\nhydrogen\nH 0 0 0\nH 0 0 0.7\n", encoding="utf-8")

    assert load_xyz_geometry(snapshot, max_atoms=2) == ("H", "H")
    with pytest.raises(ValueError, match="server atom limit"):
        load_xyz_geometry(snapshot, max_atoms=1)


def test_manifest_rejects_duplicate_yaml_mapping_keys(
    tmp_path: Path,
    manifest_limits: XtbMdManifestLimits,
) -> None:
    job_dir = tmp_path / "duplicate"
    job_dir.mkdir()
    (job_dir / "water.xyz").write_text(
        "3\nwater\nO 0 0 0\nH 0.75 0 0.5\nH -0.75 0 0.5\n",
        encoding="utf-8",
    )
    (job_dir / "xtb_md_job.yaml").write_text(
        """schema_version: 1
input_xyz: water.xyz
gfn: 2
gfn: 1
ensemble: nvt
temperature_k: 298.15
time_ps: 0.008
walltime_seconds: 300
step_fs: 4.0
dump_fs: 4.0
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate mapping key"):
        load_manifest(job_dir, limits=manifest_limits)


def test_manifest_rejects_estimated_trajectory_over_server_byte_limit(
    tmp_path: Path,
    valid_manifest_payload: dict[str, Any],
    manifest_limits: XtbMdManifestLimits,
) -> None:
    job_dir = tmp_path / "trajectory-budget"
    _write_job(job_dir, valid_manifest_payload)

    with pytest.raises(ValueError, match="estimated trajectory.*server byte limit"):
        load_manifest(
            job_dir,
            limits=replace(manifest_limits, max_trajectory_bytes=2_000),
        )
