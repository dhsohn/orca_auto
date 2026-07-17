from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from orca_auto.core.config.engines import (
    WorkflowEngineAppConfig as AppConfig,
)
from orca_auto.core.config.engines import (
    WorkflowEnginePathsConfig as PathsConfig,
)
from orca_auto.core.config.schema import CommonResourceConfig, CommonRuntimeConfig, TelegramConfig
from orca_auto.flow.engines.crest import runner as runner_mod
from orca_auto.flow.engines.crest.job_inputs import MANIFEST_FILE_NAME
from orca_auto.flow.engines.crest.runner import (
    CrestRunningJob,
    _build_command,
    _count_xyz_structures,
    finalize_crest_job,
    start_crest_job,
)


def _cfg(tmp_path: Path) -> AppConfig:
    xtb_executable = tmp_path / "bin" / "xtb"
    xtb_executable.parent.mkdir(parents=True, exist_ok=True)
    xtb_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    xtb_executable.chmod(0o700)
    return AppConfig(
        runtime=CommonRuntimeConfig(
            allowed_root=str(tmp_path / "runs"),
        ),
        paths=PathsConfig(
            crest_executable="/opt/crest",
            xtb_executable=str(xtb_executable),
        ),
        resources=CommonResourceConfig(max_cores_per_task=6, max_memory_gb_per_task=14),
        telegram=TelegramConfig(),
    )


def _write_xyz(path: Path, labels: tuple[str, ...]) -> None:
    lines: list[str] = []
    for label in labels:
        lines.extend(
            [
                "1",
                label,
                "H 0.0 0.0 0.0",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_build_command_includes_manifest_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    _write_xyz(selected_xyz, ("conf_a",))
    monkeypatch.setattr(
        "orca_auto.flow.engines.crest.runner._resolve_crest_executable",
        lambda _cfg: "/usr/bin/crest",
    )

    command = _build_command(
        cfg,
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        manifest={
            "mode": "nci",
            "speed": "squick",
            "dry_run": True,
            "keepdir": True,
            "no_preopt": True,
            "noreftopo": True,
            "notopo": True,
            "nocbonds": True,
            "gfn": "2//ff",
            "charge": "2",
            "uhf": 1,
            "solvent_model": "gbsa",
            "solvent": "water",
            "rthr": 0.3,
            "ewin": 8,
            "ethr": 0.1,
            "bthr": 0.03,
            "cluster": 3,
            "esort": True,
        },
    )

    assert command == [
        "/usr/bin/crest",
        str(selected_xyz.resolve()),
        "--T",
        "6",
        "-xnam",
        str(tmp_path / "bin" / "xtb"),
        "--nci",
        "--squick",
        "--dry",
        "--keepdir",
        "--noopt",
        "--noreftopo",
        "--notopo",
        "--nocbonds",
        "--legacy",
        "--gfn2//gfnff",
        "--chrg",
        "2",
        "--uhf",
        "1",
        "--gbsa",
        "water",
        "--rthr",
        "0.3",
        "--ewin",
        "8",
        "--ethr",
        "0.1",
        "--bthr",
        "0.03",
        "--cluster",
        "3",
        "--esort",
    ]
    assert "--scratch" not in command


@pytest.mark.parametrize(
    "solvent",
    ["water;touch", "water acetone", "$(id)", "`id`", "water\n--T 99"],
)
def test_build_command_rejects_solvent_shell_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    solvent: str,
) -> None:
    with pytest.raises(ValueError, match="solvent"):
        _sampling_command(
            monkeypatch,
            tmp_path,
            {"solvent_model": "gbsa", "solvent": solvent},
        )


def test_build_command_preserves_nested_input_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    selected_xyz = job_dir / "nested" / "input.xyz"
    selected_xyz.parent.mkdir(parents=True)
    _write_xyz(selected_xyz, ("conf_a",))
    monkeypatch.setattr(
        "orca_auto.flow.engines.crest.runner._resolve_crest_executable",
        lambda _cfg: "/usr/bin/crest",
    )

    command = _build_command(
        cfg,
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        manifest={},
    )

    assert command[1] == str(selected_xyz.resolve())


def test_build_command_rejects_input_outside_job_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = tmp_path / "outside.xyz"
    _write_xyz(selected_xyz, ("conf_a",))
    monkeypatch.setattr(
        "orca_auto.flow.engines.crest.runner._resolve_crest_executable",
        lambda _cfg: "/usr/bin/crest",
    )

    with pytest.raises(ValueError, match="inside the job directory"):
        _build_command(
            cfg,
            job_dir=job_dir,
            selected_xyz=selected_xyz,
            manifest={},
        )


def test_build_command_protects_dash_prefixed_input_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "-input.xyz"
    _write_xyz(selected_xyz, ("conf_a",))
    monkeypatch.setattr(
        "orca_auto.flow.engines.crest.runner._resolve_crest_executable",
        lambda _cfg: "/usr/bin/crest",
    )

    command = _build_command(
        cfg,
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        manifest={},
    )

    assert command[1] == str(selected_xyz.resolve())


def test_build_command_accepts_topology_aliases_without_duplicate_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    _write_xyz(selected_xyz, ("conf_a",))
    monkeypatch.setattr(
        "orca_auto.flow.engines.crest.runner._resolve_crest_executable",
        lambda _cfg: "/usr/bin/crest",
    )

    command = _build_command(
        cfg,
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        manifest={
            "noreftopo": True,
            "no_reftopo": True,
            "notopo": True,
            "no_topo": True,
            "nocbonds": True,
            "no_cbonds": True,
        },
    )

    assert command.count("--noreftopo") == 1
    assert command.count("--notopo") == 1
    assert command.count("--nocbonds") == 1


def test_start_crest_job_passes_expected_subprocess_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "molecule.xyz"
    _write_xyz(selected_xyz, ("conf_a",))
    stale_output = job_dir / "crest_conformers.xyz"
    _write_xyz(stale_output, ("stale",))
    (job_dir / MANIFEST_FILE_NAME).write_text(
        "mode: standard\nresources:\n  max_cores: 11\n  max_memory_gb: 22\n",
        encoding="utf-8",
    )
    popen_calls: dict[str, Any] = {}

    class _FakeProcess:
        def poll(self) -> int | None:
            return 0

    def fake_popen(*args: Any, **kwargs: Any) -> _FakeProcess:
        popen_calls["args"] = args
        popen_calls["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(
        "orca_auto.flow.engines.crest.runner._resolve_crest_executable", lambda _cfg: "/opt/crest"
    )
    monkeypatch.setattr("orca_auto.flow.engines.crest.runner.subprocess.Popen", fake_popen)

    running = start_crest_job(cfg, job_dir=job_dir, selected_xyz=selected_xyz)

    assert not stale_output.exists()
    assert running.command[:4] == (
        "/opt/crest",
        str(selected_xyz.resolve()),
        "--T",
        "11",
    )
    assert running.mode == "standard"
    assert running.selected_input_xyz == str(selected_xyz.resolve())
    kwargs = popen_calls["kwargs"]
    assert popen_calls["args"][0][:4] == [
        "/opt/crest",
        str(selected_xyz.resolve()),
        "--T",
        "11",
    ]
    assert "--scratch" not in popen_calls["args"][0]
    assert kwargs["cwd"] == job_dir
    assert kwargs["text"] is True
    assert kwargs["stdin"] is not None
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["OMP_NUM_THREADS"] == "11"
    assert kwargs["env"]["OPENBLAS_NUM_THREADS"] == "11"
    assert kwargs["env"]["MKL_NUM_THREADS"] == "11"
    assert kwargs["env"]["NUMEXPR_NUM_THREADS"] == "11"
    assert running.resource_request == {"max_cores": 11, "max_memory_gb": 22}
    assert running.resource_actual == {
        "assigned_cores": 11,
        "memory_limit_gb": 22,
        "omp_num_threads": 11,
        "openblas_num_threads": 11,
        "mkl_num_threads": 11,
        "numexpr_num_threads": 11,
    }
    assert Path(running.stdout_log).name == "crest.stdout.log"
    assert Path(running.stderr_log).name == "crest.stderr.log"

    running.stdout_handle.close()
    running.stderr_handle.close()


def test_stale_crest_output_fsync_failure_blocks_launch_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected = job_dir / "input.xyz"
    _write_xyz(selected, ("input",))
    stale = job_dir / "crest_conformers.xyz"
    _write_xyz(stale, ("stale",))
    monkeypatch.setattr(
        runner_mod,
        "fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("stale unlink fsync failed")),
    )

    with pytest.raises(OSError, match="stale unlink fsync failed"):
        runner_mod._clear_stale_crest_outputs(job_dir, selected_input_xyz=selected)

    assert not stale.exists()


def test_finalize_crest_job_collects_retained_outputs(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    stdout_path = job_dir / "crest.stdout.log"
    stderr_path = job_dir / "crest.stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    _write_xyz(job_dir / "input.xyz", ("input",))
    _write_xyz(job_dir / "crest_conformers.xyz", ("conf_a", "conf_b"))

    process = MagicMock()
    process.poll.return_value = 0
    running = CrestRunningJob(
        process=process,
        command=("crest", "input.xyz"),
        started_at="2026-04-19T00:00:00+00:00",
        stdout_log=str(stdout_path.resolve()),
        stderr_log=str(stderr_path.resolve()),
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
        selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        mode="standard",
        manifest_path=str((job_dir / MANIFEST_FILE_NAME).resolve()),
        resource_request={"max_cores": 4, "max_memory_gb": 8},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 8},
        job_dir=str(job_dir.resolve()),
    )

    result = finalize_crest_job(running)

    assert result.status == "completed"
    assert result.reason == "completed"
    assert result.exit_code == 0
    assert result.retained_conformer_count == 2
    assert result.retained_conformer_paths == (str((job_dir / "crest_conformers.xyz").resolve()),)
    assert result.resource_request == {"max_cores": 4, "max_memory_gb": 8}
    assert result.resource_actual == {"assigned_cores": 4, "memory_limit_gb": 8}


@pytest.mark.parametrize(
    "retained_payload",
    [None, "10\ntruncated\nH 0 0 0\n", "1\nnonfinite\nH nan 0 0\n"],
)
def test_finalize_crest_job_rejects_missing_or_invalid_retained_ensemble(
    tmp_path: Path, retained_payload: str | None
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    _write_xyz(job_dir / "input.xyz", ("input",))
    if retained_payload is not None:
        retained = job_dir / "crest_conformers.xyz"
        retained.write_text(retained_payload, encoding="utf-8")
        assert _count_xyz_structures(retained) == 0
    stdout_path = job_dir / "crest.stdout.log"
    stderr_path = job_dir / "crest.stderr.log"
    process = MagicMock()
    process.poll.return_value = 0
    running = CrestRunningJob(
        process=process,
        command=("crest", "input.xyz"),
        started_at="2026-04-19T00:00:00+00:00",
        stdout_log=str(stdout_path.resolve()),
        stderr_log=str(stderr_path.resolve()),
        stdout_handle=stdout_path.open("w", encoding="utf-8"),
        stderr_handle=stderr_path.open("w", encoding="utf-8"),
        selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        mode="standard",
        manifest_path="",
        resource_request={"max_cores": 4, "max_memory_gb": 8},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 8},
        job_dir=str(job_dir.resolve()),
    )

    result = finalize_crest_job(running)

    assert result.status == "failed"
    assert result.reason == "crest_no_valid_retained_ensemble"
    assert result.retained_conformer_count == 0
    assert result.retained_conformer_paths == ()


def test_finalize_crest_job_preserves_all_valid_retained_ensembles(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    _write_xyz(job_dir / "input.xyz", ("input",))
    _write_xyz(job_dir / "crest_conformers.xyz", ("first", "second"))
    _write_xyz(job_dir / "crest_best.xyz", ("first",))
    stdout_path = job_dir / "crest.stdout.log"
    stderr_path = job_dir / "crest.stderr.log"
    process = MagicMock()
    process.poll.return_value = 0
    running = CrestRunningJob(
        process=process,
        command=("crest", "input.xyz"),
        started_at="2026-04-19T00:00:00+00:00",
        stdout_log=str(stdout_path.resolve()),
        stderr_log=str(stderr_path.resolve()),
        stdout_handle=stdout_path.open("w", encoding="utf-8"),
        stderr_handle=stderr_path.open("w", encoding="utf-8"),
        selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        mode="standard",
        manifest_path="",
        resource_request={"max_cores": 4, "max_memory_gb": 8},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 8},
        job_dir=str(job_dir.resolve()),
    )

    result = finalize_crest_job(running)

    assert result.status == "completed"
    assert result.retained_conformer_count == 2
    assert result.retained_conformer_paths == (
        str((job_dir / "crest_conformers.xyz").resolve()),
        str((job_dir / "crest_best.xyz").resolve()),
    )


def test_finalize_crest_job_preserves_distinct_rotamer_geometry(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    _write_xyz(job_dir / "input.xyz", ("input",))
    _write_xyz(job_dir / "crest_conformers.xyz", ("conformer",))
    rotamer = job_dir / "crest_rotamers.xyz"
    rotamer.write_text("1\nrotamer\nH 1.0 0.0 0.0\n", encoding="utf-8")
    stdout_path = job_dir / "crest.stdout.log"
    stderr_path = job_dir / "crest.stderr.log"
    process = MagicMock()
    process.poll.return_value = 0
    running = CrestRunningJob(
        process=process,
        command=("crest", "input.xyz"),
        started_at="2026-04-19T00:00:00+00:00",
        stdout_log=str(stdout_path.resolve()),
        stderr_log=str(stderr_path.resolve()),
        stdout_handle=stdout_path.open("w", encoding="utf-8"),
        stderr_handle=stderr_path.open("w", encoding="utf-8"),
        selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        mode="standard",
        manifest_path="",
        resource_request={"max_cores": 4, "max_memory_gb": 8},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 8},
        job_dir=str(job_dir.resolve()),
    )

    result = finalize_crest_job(running)

    assert result.status == "completed"
    assert result.retained_conformer_count == 2
    assert result.retained_conformer_paths == (
        str((job_dir / "crest_conformers.xyz").resolve()),
        str(rotamer.resolve()),
    )


def test_finalize_crest_job_rejects_retained_output_for_different_atoms(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    _write_xyz(job_dir / "input.xyz", ("input",))
    (job_dir / "crest_conformers.xyz").write_text(
        "1\nstale other molecule\nCl 0 0 0\n", encoding="utf-8"
    )
    stdout_path = job_dir / "crest.stdout.log"
    stderr_path = job_dir / "crest.stderr.log"
    process = MagicMock()
    process.poll.return_value = 0
    running = CrestRunningJob(
        process=process,
        command=("crest", "input.xyz"),
        started_at="2026-04-19T00:00:00+00:00",
        stdout_log=str(stdout_path.resolve()),
        stderr_log=str(stderr_path.resolve()),
        stdout_handle=stdout_path.open("w", encoding="utf-8"),
        stderr_handle=stderr_path.open("w", encoding="utf-8"),
        selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        mode="standard",
        manifest_path="",
        resource_request={"max_cores": 4, "max_memory_gb": 8},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 8},
        job_dir=str(job_dir.resolve()),
    )

    result = finalize_crest_job(running)

    assert result.status == "failed"
    assert result.reason == "crest_no_valid_retained_ensemble"
    assert result.retained_conformer_paths == ()


def _sampling_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, manifest: dict[str, Any]
) -> list[str]:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir(exist_ok=True)  # helper may run twice within one test
    selected_xyz = job_dir / "input.xyz"
    _write_xyz(selected_xyz, ("conf_a",))
    monkeypatch.setattr(
        "orca_auto.flow.engines.crest.runner._resolve_crest_executable",
        lambda _cfg: "/usr/bin/crest",
    )
    return _build_command(cfg, job_dir=job_dir, selected_xyz=selected_xyz, manifest=manifest)


def test_build_command_emits_verified_sampling_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = _sampling_command(
        monkeypatch,
        tmp_path,
        {
            "mdlen": 1.25,
            "wscal": 1.0,
            "tstep": 2.5,
            "mddump": 100,
            "shake": 2,
            "norotmd": True,
            "nocross": True,
        },
    )
    # Flags verified against CREST 3.0.2 `--help conf`; reals carry no exponent and
    # an int-valued real (wscal 1.0) normalizes to "1".
    assert command[command.index("--mdlen") + 1] == "1.25"
    assert command[command.index("--wscal") + 1] == "1"
    assert command[command.index("--tstep") + 1] == "2.5"
    assert command[command.index("--mddump") + 1] == "100"
    assert command[command.index("--shake") + 1] == "2"
    assert "--norotmd" in command
    assert "--nocross" in command
    assert "--cross" not in command


def test_build_command_len_is_an_alias_for_mdlen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = _sampling_command(monkeypatch, tmp_path, {"len": 2.0})
    assert command[command.index("--mdlen") + 1] == "2"
    assert command.count("--mdlen") == 1

    with pytest.raises(ValueError, match="must match"):
        _sampling_command(monkeypatch, tmp_path, {"mdlen": 2.0, "len": 3.0})


def test_build_command_shake_zero_is_emitted_not_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 0 disables SHAKE — a meaningful value, not "absent".
    command = _sampling_command(monkeypatch, tmp_path, {"shake": 0})
    assert command[command.index("--shake") + 1] == "0"


def test_build_command_false_bools_and_unknown_keys_omit_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = _sampling_command(
        monkeypatch,
        tmp_path,
        {
            "norotmd": False,
            "nocross": False,
            "cross": False,
            "mdlenn": 1.0,
            "shakee": 2,
            "no_cross": True,
        },
    )
    assert "--norotmd" not in command
    assert "--nocross" not in command
    assert "--cross" not in command
    assert "--mdlen" not in command  # a typo'd key is silently ignored, never shipped
    assert "--shake" not in command


def test_build_command_cross_and_nocross_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        _sampling_command(monkeypatch, tmp_path, {"cross": True, "nocross": True})


def test_build_command_cross_true_keeps_crest_default_without_redundant_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = _sampling_command(monkeypatch, tmp_path, {"cross": True})

    assert "--cross" not in command
    assert "--nocross" not in command


@pytest.mark.parametrize(
    "manifest",
    [
        {"mdlen": 0},
        {"mdlen": -1.0},
        {"wscal": "fast"},
        {"mdlen": True},
        {"tstep": 0},
        {"tstep": "fast"},
        {"mddump": 1.5},
        {"shake": 3},
        {"shake": -1},
    ],
)
def test_build_command_rejects_malformed_sampling_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, manifest: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        _sampling_command(monkeypatch, tmp_path, manifest)


@pytest.mark.parametrize("key", ["cross", "nocross", "norotmd"])
@pytest.mark.parametrize("value", ["treu", 2, [], {}])
def test_build_command_rejects_malformed_sampling_booleans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    key: str,
    value: Any,
) -> None:
    with pytest.raises(ValueError, match=key):
        _sampling_command(monkeypatch, tmp_path, {key: value})


def test_build_command_accepts_canonical_boolean_strings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = _sampling_command(
        monkeypatch,
        tmp_path,
        {"norotmd": "yes", "nocross": "true", "cross": "false"},
    )

    assert "--norotmd" in command
    assert "--nocross" in command
    assert "--cross" not in command


@pytest.mark.parametrize("manifest", [{"mdlen": 4e-7}, {"wscal": 4e-7}])
def test_build_command_never_rounds_positive_sampling_values_to_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        _sampling_command(monkeypatch, tmp_path, manifest)


def test_build_command_enforces_crest_native_numeric_bounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    max_int = (1 << 31) - 1
    command = _sampling_command(monkeypatch, tmp_path, {"mdlen": 0.001, "mddump": max_int})
    assert command[command.index("--mddump") + 1] == str(max_int)

    with pytest.raises(ValueError, match="mddump"):
        _sampling_command(monkeypatch, tmp_path, {"mdlen": 0.001, "mddump": max_int + 1})
    with pytest.raises(ValueError, match="MD steps"):
        _sampling_command(monkeypatch, tmp_path, {"mdlen": 10_000_000, "tstep": 0.001})
    with pytest.raises(ValueError, match="MD steps"):
        _sampling_command(monkeypatch, tmp_path, {"mdlen": 1e308, "tstep": 0.001})


@pytest.mark.parametrize("tstep", [0.0009, 2500.1, float("nan"), float("inf")])
def test_build_command_rejects_native_unsafe_tstep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tstep: float
) -> None:
    with pytest.raises(ValueError, match="tstep"):
        _sampling_command(monkeypatch, tmp_path, {"tstep": tstep})


def test_sampling_budget_uses_method_specific_default_timestep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="admitted work budget"):
        _sampling_command(monkeypatch, tmp_path, {"gfn": "ff", "mdlen": 120})

    command = _sampling_command(monkeypatch, tmp_path, {"gfn": 2, "mdlen": 120})
    assert command[command.index("--mdlen") + 1] == "120"

    with pytest.raises(ValueError, match="admitted work budget"):
        _sampling_command(monkeypatch, tmp_path, {"gfn": "2//ff", "mdlen": 150})


def test_sampling_budget_requires_explicit_high_cost_acknowledgement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = {"gfn": "ff", "mdlen": 120, "max_md_steps": 12_000_000}
    with pytest.raises(ValueError, match="allow_high_cost_md"):
        _sampling_command(monkeypatch, tmp_path, manifest)

    command = _sampling_command(
        monkeypatch,
        tmp_path,
        {**manifest, "allow_high_cost_md": True},
    )
    assert command[command.index("--mdlen") + 1] == "120"


def test_sampling_budget_caps_atom_count_times_aggregate_md_steps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "atom-work-budget"
    job_dir.mkdir()
    atom_count = 6_000
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text(
        f"{atom_count}\nlarge molecule\n" + "H 0 0 0\n" * atom_count,
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orca_auto.flow.engines.crest.runner._resolve_crest_executable",
        lambda _cfg: "/usr/bin/crest",
    )

    with pytest.raises(ValueError, match="server work-unit ceiling"):
        _build_command(
            cfg,
            job_dir=job_dir,
            selected_xyz=selected_xyz,
            manifest={"gfn": 2, "mdlen": 300},
        )


def test_automatic_mdlen_respects_explicit_max_md_steps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="automatic mdlen worst case"):
        _sampling_command(monkeypatch, tmp_path, {"max_md_steps": 1})


def test_local_default_uses_server_automatic_md_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    command = _sampling_command(monkeypatch, tmp_path, {})

    assert "--mdlen" not in command


def test_start_crest_job_fails_closed_on_malformed_sampling_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A bad value must raise before launch so the execution layer records a failed
    # job, never forwarding a bad token to CREST.
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "molecule.xyz"
    _write_xyz(selected_xyz, ("conf_a",))
    (job_dir / MANIFEST_FILE_NAME).write_text("mode: standard\nmdlen: fast\n", encoding="utf-8")
    monkeypatch.setattr(
        "orca_auto.flow.engines.crest.runner._resolve_crest_executable", lambda _cfg: "/opt/crest"
    )

    with pytest.raises(ValueError, match="mdlen"):
        start_crest_job(cfg, job_dir=job_dir, selected_xyz=selected_xyz)


def test_finalize_crest_job_can_force_cancelled_result(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    stdout_path = job_dir / "crest.stdout.log"
    stderr_path = job_dir / "crest.stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")

    process = MagicMock()
    process.poll.return_value = -15
    running = CrestRunningJob(
        process=process,
        command=("crest", "input.xyz"),
        started_at="2026-04-19T00:00:00+00:00",
        stdout_log=str(stdout_path.resolve()),
        stderr_log=str(stderr_path.resolve()),
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
        selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        mode="nci",
        manifest_path="",
        resource_request={},
        resource_actual={},
        job_dir=str(job_dir.resolve()),
    )

    result = finalize_crest_job(
        running,
        forced_status="cancelled",
        forced_reason="cancel_requested",
    )

    assert result.status == "cancelled"
    assert result.reason == "cancel_requested"
    assert result.exit_code == -15
    assert result.mode == "nci"
