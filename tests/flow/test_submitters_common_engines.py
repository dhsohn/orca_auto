from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto import cli_worker_specs
from orca_auto.flow import engine_runtime
from orca_auto.flow.submitters import (
    crest as crest_submitter,
)
from orca_auto.flow.submitters import (
    internal_engine_submission,
)
from orca_auto.flow.submitters import (
    xtb as xtb_submitter,
)


def test_queue_submission_status_treats_admission_wait_as_blocked() -> None:
    status, reason = internal_engine_submission.queue_submission_status(
        returncode=1,
        parsed_stdout={"status": "waiting_for_slot"},
        stdout="status: waiting_for_slot\n",
        stderr="Admission limit reached",
    )

    assert status == "blocked"
    assert reason == "waiting_for_slot"


def test_worker_module_command_without_repo_root_uses_module_execution() -> None:
    argv, cwd, env = cli_worker_specs.worker_module_command(
        config_path="/tmp/config.yaml",
        repo_root=None,
        module_name="orca_auto.orca.commands.queue",
        tail_argv=["--engine", "orca"],
    )

    assert argv == [
        sys.executable,
        "-m",
        "orca_auto.orca.commands.queue",
        "--config",
        "/tmp/config.yaml",
        "--engine",
        "orca",
    ]
    assert cwd is None
    assert env is None


def test_worker_module_command_with_repo_root_uses_module_execution_and_prepends_pythonpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setenv("PYTHONPATH", "/existing/site-packages")

    argv, cwd, env = cli_worker_specs.worker_module_command(
        config_path="/tmp/config.yaml",
        repo_root=str(repo_root),
        module_name="orca_auto.cli",
        tail_argv=["queue", "cancel", "job-1"],
    )

    assert argv == [
        sys.executable,
        "-m",
        "orca_auto.cli",
        "--config",
        "/tmp/config.yaml",
        "queue",
        "cancel",
        "job-1",
    ]
    assert cwd == str(repo_root.resolve())
    assert env is not None
    assert env["PYTHONPATH"] == f"{repo_root.resolve()}:/existing/site-packages"


def test_engine_runtime_paths_reads_top_level_runs_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    assert engine_runtime.engine_runtime_paths(str(config_path)) == {
        "workflow_root": runs_root.resolve(),
        "allowed_root": runs_root.resolve(),
        "admission_root": runs_root.resolve() / ".admission",
    }


def test_engine_runtime_paths_requires_runs_root(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("scheduler:\n  max_active_simulations: 4\n", encoding="utf-8")

    for engine in (None, "orca", "xtb", "crest"):
        with pytest.raises(ValueError, match="Missing runs_root"):
            engine_runtime.engine_runtime_paths(str(config_path), engine=engine)


def test_engine_runtime_paths_rejects_invalid_runs_root_before_resolving(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"

    config_path.write_text("runs_root: './runs'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="absolute Linux path"):
        engine_runtime.engine_runtime_paths(str(config_path), engine="orca")

    config_path.write_text("runs_root: '/mnt/c/runs'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Linux path"):
        engine_runtime.engine_runtime_paths(str(config_path), engine="xtb")


def test_engine_runtime_paths_ignores_legacy_root_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "workflow:",
                "  root: /tmp/wf",
                "orca:",
                "  runtime:",
                "    allowed_root: /tmp/runs",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Missing runs_root"):
        engine_runtime.engine_runtime_paths(str(config_path), engine="orca")


def test_engine_runtime_paths_all_engines_share_the_runs_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    expected = {
        "workflow_root": runs_root.resolve(),
        "allowed_root": runs_root.resolve(),
        "admission_root": runs_root.resolve() / ".admission",
    }
    for engine in ("orca", "xtb", "crest"):
        assert engine_runtime.engine_runtime_paths(str(config_path), engine=engine) == expected


def test_engine_runtime_paths_uses_scheduler_admission_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    admission_root = tmp_path / "admission"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {runs_root}",
                "scheduler:",
                "  max_active_simulations: 4",
                f"  admission_root: {admission_root}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    for engine in (None, "orca", "xtb", "crest"):
        paths = engine_runtime.engine_runtime_paths(str(config_path), engine=engine)
        assert paths["allowed_root"] == runs_root.resolve()
        assert paths["admission_root"] == admission_root.resolve()


def test_engine_runtime_paths_engine_scoped_scheduler_overrides_top_level(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    shared_admission = tmp_path / "shared-admission"
    orca_admission = tmp_path / "orca-admission"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {runs_root}",
                "scheduler:",
                f"  admission_root: {shared_admission}",
                "orca:",
                "  scheduler:",
                f"    admission_root: {orca_admission}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    orca_paths = engine_runtime.engine_runtime_paths(str(config_path), engine="orca")
    assert orca_paths["admission_root"] == orca_admission.resolve()
    # Internal engines keep using the top-level scheduler section.
    xtb_paths = engine_runtime.engine_runtime_paths(str(config_path), engine="xtb")
    assert xtb_paths["admission_root"] == shared_admission.resolve()


@pytest.mark.parametrize(
    ("module", "engine", "job_dir", "priority", "job_id", "queue_id", "extras"),
    [
        (
            xtb_submitter,
            "xtb",
            "/jobs/xtb-1",
            7,
            "xtb-job-1",
            "q-xtb-1",
            {"job_type": "path", "reaction_key": "rxn-1"},
        ),
        (
            crest_submitter,
            "crest",
            "/jobs/crest-1",
            3,
            "crest-job-1",
            "q-crest-1",
            {},
        ),
    ],
)
def test_submit_job_dir_uses_structured_engine_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    engine: str,
    job_dir: str,
    priority: int,
    job_id: str,
    queue_id: str,
    extras: dict[str, str],
) -> None:
    captured: dict[str, Any] = {}
    cfg = SimpleNamespace(name=f"{engine}-config")
    resolved_job_dir = tmp_path / engine / "organized-job"
    manifest = {"manifest": True}

    def fake_load_config(config_path: str) -> Any:
        captured["config_path"] = config_path
        return cfg

    def fake_resolve_job_dir(cfg_arg: Any, raw_job_dir: str) -> Path:
        captured["resolve"] = (cfg_arg, raw_job_dir)
        return resolved_job_dir

    def fake_load_manifest(job_dir_arg: Path) -> dict[str, Any]:
        captured["manifest_job_dir"] = job_dir_arg
        return manifest

    def fake_build_submission(
        cfg_arg: Any,
        job_dir_arg: Path,
        manifest_arg: dict[str, Any],
        args: Any,
    ) -> Any:
        captured["build"] = (cfg_arg, job_dir_arg, manifest_arg, args)
        metadata = {"job_dir": str(job_dir_arg), **extras}
        return SimpleNamespace(
            queue_root=tmp_path / engine / "queue",
            app_name=f"orca_auto_{engine}",
            task_id=job_id,
            task_kind=f"{engine}_job",
            engine=engine,
            priority=int(args.priority),
            metadata=metadata,
            context={},
        )

    def fake_enqueue(root: Path, **kwargs: Any) -> Any:
        captured["enqueue"] = (root, kwargs)
        return SimpleNamespace(
            queue_id=queue_id,
            task_id=kwargs["task_id"],
            priority=kwargs["priority"],
        )

    def fake_record_queued(cfg_arg: Any, submission: Any, entry: Any) -> None:
        captured["record"] = (cfg_arg, submission, entry)

    monkeypatch.setattr(module, "load_config", fake_load_config)
    monkeypatch.setattr(module, "resolve_job_dir", fake_resolve_job_dir)
    monkeypatch.setattr(module, "load_job_manifest", fake_load_manifest)
    monkeypatch.setattr(module, "build_submission", fake_build_submission)
    monkeypatch.setattr(module, "enqueue", fake_enqueue)
    monkeypatch.setattr(module, "record_queued", fake_record_queued)

    result = module.submit_job_dir(
        job_dir=job_dir,
        priority=priority,
        config_path="/tmp/config.yaml",
    )

    build_args = captured["build"][3]
    assert captured["config_path"] == "/tmp/config.yaml"
    assert captured["resolve"] == (cfg, job_dir)
    assert captured["manifest_job_dir"] == resolved_job_dir
    assert build_args.config == "/tmp/config.yaml"
    assert build_args.path == job_dir
    assert build_args.priority == priority
    assert captured["enqueue"][1]["metadata"] == {"job_dir": str(resolved_job_dir), **extras}
    assert captured["record"][0] is cfg
    assert result["status"] == "submitted"
    assert result["returncode"] == 0
    assert result["command_argv"] == [
        f"orca_auto.{engine}.submission.direct_enqueue",
        "config=/tmp/config.yaml",
        f"job_dir={job_dir}",
        f"priority={priority}",
    ]
    assert result["stdout"].startswith("status: queued\n")
    assert result["stderr"] == ""
    assert result["parsed_stdout"]["status"] == "queued"
    assert result["job_id"] == job_id
    assert result["queue_id"] == queue_id
    assert result["job_dir"] == str(resolved_job_dir)
    if module is xtb_submitter:
        assert result["job_type"] == extras["job_type"]
        assert result["reaction_key"] == extras["reaction_key"]
        assert result["parsed_stdout"]["job_type"] == extras["job_type"]
        assert result["parsed_stdout"]["reaction_key"] == extras["reaction_key"]


@pytest.mark.parametrize("module", [xtb_submitter, crest_submitter])
def test_submit_job_dir_reports_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
) -> None:
    def fake_load_config(_config_path: str) -> Any:
        raise RuntimeError("submission failed")

    monkeypatch.setattr(module, "load_config", fake_load_config)

    result = module.submit_job_dir(
        job_dir="/jobs/job-1",
        priority=4,
        config_path="/tmp/config.yaml",
    )

    assert result["status"] == "failed"
    assert result["returncode"] == 1
    assert result["stdout"] == ""
    assert result["stderr"] == "RuntimeError: submission failed\n"
    assert result["parsed_stdout"] == {}
    assert result["job_id"] == ""
    assert result["queue_id"] == ""


@pytest.mark.parametrize(
    ("module", "engine", "target", "displayed_status", "expected_status", "queue_id", "job_id"),
    [
        (
            xtb_submitter,
            "xtb",
            "xtb-job-1",
            "cancel_requested",
            "cancel_requested",
            "q-1",
            "xtb-job-1",
        ),
        (xtb_submitter, "xtb", "xtb-job-2", "cancelled", "cancelled", "q-2", "xtb-job-2"),
        (
            crest_submitter,
            "crest",
            "crest-job-1",
            "cancel_requested",
            "cancel_requested",
            "c-1",
            "crest-job-1",
        ),
        (crest_submitter, "crest", "crest-job-2", "cancelled", "cancelled", "c-2", "crest-job-2"),
    ],
)
def test_cancel_target_uses_structured_queue_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    engine: str,
    target: str,
    displayed_status: str,
    expected_status: str,
    queue_id: str,
    job_id: str,
) -> None:
    captured: dict[str, Any] = {}
    queue_root = tmp_path / "queue"
    cfg = SimpleNamespace(name=f"{engine}-queue-config")
    original_entry = SimpleNamespace(queue_id=queue_id, task_id=job_id)

    def fake_load_queue_config(config_path: str) -> Any:
        captured["config_path"] = config_path
        return cfg

    def fake_queue_entries_with_roots(cfg_arg: Any) -> list[tuple[Path, Any]]:
        captured["listed_cfg"] = cfg_arg
        return [(queue_root, original_entry)]

    def fake_request_cancel(root: Path, requested_queue_id: str) -> Any:
        captured["request_cancel"] = (root, requested_queue_id)
        return SimpleNamespace(
            queue_id=requested_queue_id,
            task_id=job_id,
            cancel_requested=displayed_status == "cancel_requested",
            status=SimpleNamespace(
                value="running" if displayed_status == "cancel_requested" else "cancelled"
            ),
        )

    def fake_display_status(entry: Any) -> str:
        captured["display_entry"] = entry
        return displayed_status

    monkeypatch.setattr(module, "load_queue_config", fake_load_queue_config)
    monkeypatch.setattr(module, "queue_entries_with_roots", fake_queue_entries_with_roots)
    monkeypatch.setattr(module, "request_cancel", fake_request_cancel)
    monkeypatch.setattr(module, "display_status", fake_display_status)

    result = module.cancel_target(
        target=target,
        config_path="/tmp/config.yaml",
    )

    assert captured["config_path"] == "/tmp/config.yaml"
    assert captured["listed_cfg"] is cfg
    assert captured["request_cancel"] == (queue_root, queue_id)
    assert result["status"] == expected_status
    assert result["returncode"] == 0
    assert result["command_argv"] == [
        f"orca_auto.{engine}.queue_runtime.direct_cancel",
        "config=/tmp/config.yaml",
        f"target={target}",
    ]
    assert result["stdout"] == f"status: {expected_status}\nqueue_id: {queue_id}\njob_id: {job_id}"
    assert result["stderr"] == ""
    assert result["parsed_stdout"]["status"] == expected_status
    assert result["queue_id"] == queue_id
    assert result["job_id"] == job_id


@pytest.mark.parametrize("module", [xtb_submitter, crest_submitter])
def test_cancel_target_reports_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
) -> None:
    def fake_load_queue_config(_config_path: str) -> Any:
        return object()

    def fake_queue_entries_with_roots(_cfg: Any) -> list[tuple[Path, Any]]:
        return [(tmp_path / "queue", SimpleNamespace(queue_id="q-1", task_id="job-1"))]

    def fake_request_cancel(_root: Path, _queue_id: str) -> Any:
        raise RuntimeError("cancel failed")

    monkeypatch.setattr(module, "load_queue_config", fake_load_queue_config)
    monkeypatch.setattr(module, "queue_entries_with_roots", fake_queue_entries_with_roots)
    monkeypatch.setattr(module, "request_cancel", fake_request_cancel)

    result = module.cancel_target(
        target="job-1",
        config_path="/tmp/config.yaml",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "cancel_command_failed"
    assert result["returncode"] == 1
    assert result["stdout"] == ""
    assert result["stderr"] == "RuntimeError: cancel failed\n"
