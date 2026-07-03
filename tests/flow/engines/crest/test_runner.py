from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from orca_auto.core.config.engines import (
    WorkflowEngineAppConfig as AppConfig,
)
from orca_auto.core.config.engines import (
    WorkflowEngineBehaviorConfig as BehaviorConfig,
)
from orca_auto.core.config.engines import (
    WorkflowEnginePathsConfig as PathsConfig,
)
from orca_auto.core.config.schema import CommonResourceConfig, CommonRuntimeConfig, TelegramConfig
from orca_auto.flow.engines.crest.job_inputs import MANIFEST_FILE_NAME
from orca_auto.flow.engines.crest.runner import (
    CrestRunningJob,
    _build_command,
    finalize_crest_job,
    start_crest_job,
)


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runtime=CommonRuntimeConfig(
            allowed_root=str(tmp_path / "runs"),
        ),
        paths=PathsConfig(crest_executable="/opt/crest"),
        behavior=BehaviorConfig(),
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
        "input.xyz",
        "--T",
        "6",
        "--nci",
        "--squick",
        "--dry",
        "--keepdir",
        "--noopt",
        "--noreftopo",
        "--notopo",
        "--nocbonds",
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
        "--scratch",
        str(job_dir / ".crest_scratch"),
    ]


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

    assert running.command[:4] == ("/opt/crest", "molecule.xyz", "--T", "11")
    assert running.mode == "standard"
    assert running.selected_input_xyz == str(selected_xyz.resolve())
    kwargs = popen_calls["kwargs"]
    assert popen_calls["args"][0][:4] == ["/opt/crest", "molecule.xyz", "--T", "11"]
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


def test_finalize_crest_job_collects_retained_outputs(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    stdout_path = job_dir / "crest.stdout.log"
    stderr_path = job_dir / "crest.stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
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
