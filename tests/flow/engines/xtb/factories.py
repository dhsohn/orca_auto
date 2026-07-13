from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from orca_auto.core import engine_runner
from orca_auto.core.config import CommonResourceConfig, CommonRuntimeConfig, TelegramConfig
from orca_auto.core.config.engines import (
    WorkflowEngineAppConfig as AppConfig,
)
from orca_auto.core.config.engines import (
    WorkflowEnginePathsConfig as PathsConfig,
)
from orca_auto.core.queue.engine.input_snapshot import snapshot_input_file, snapshot_input_payload
from orca_auto.flow.engines.xtb import queue_runtime as queue_cmd
from orca_auto.flow.engines.xtb import runner as runner_mod
from orca_auto.flow.engines.xtb.job_locations import reaction_key_from_job_dir


def make_cfg(tmp_path: Path) -> SimpleNamespace:
    allowed_root = tmp_path / "allowed"
    organized_root = tmp_path / "organized"
    admission_root = tmp_path / "admission"
    allowed_root.mkdir()
    organized_root.mkdir()
    admission_root.mkdir()
    return SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(allowed_root),
            max_concurrent=2,
            admission_root=str(admission_root),
            admission_limit=2,
        ),
        resources=SimpleNamespace(max_cores_per_task=4, max_memory_gb_per_task=8),
        telegram=SimpleNamespace(bot_token="", chat_id=""),
        paths=SimpleNamespace(xtb_executable=""),
    )


def make_runner_cfg(tmp_path: Path, *, xtb_executable: str = "") -> AppConfig:
    return AppConfig(
        runtime=CommonRuntimeConfig(
            allowed_root=str(tmp_path / "allowed"),
        ),
        paths=PathsConfig(xtb_executable=xtb_executable),
        resources=CommonResourceConfig(max_cores_per_task=4, max_memory_gb_per_task=12),
        telegram=TelegramConfig(),
    )


def write_xyz(path: Path, *, comment: str = "example") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "3",
                comment,
                "O 0.000000 0.000000 0.000000",
                "H 0.000000 0.000000 0.970000",
                "H 0.000000 0.750000 -0.240000",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def write_multi_xyz(path: Path, comments: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index, comment in enumerate(comments):
        z_shift = 0.97 + (index * 0.01)
        y_shift = 0.75 - (index * 0.02)
        lines.extend(
            [
                "3",
                comment,
                "O 0.000000 0.000000 0.000000",
                f"H 0.000000 0.000000 {z_shift:.6f}",
                f"H 0.000000 {y_shift:.6f} -0.240000",
            ]
        )
    path.write_text("\n".join([*lines, ""]), encoding="utf-8")
    return path


class FakeCandidateProcess:
    def __init__(
        self,
        poll_values: list[int | None],
        *,
        terminate_raises: bool = False,
    ) -> None:
        self._poll_values = list(poll_values)
        self._terminal_returncode: int | None = None
        self.terminate_calls = 0
        self.terminate_raises = terminate_raises

    def poll(self) -> int | None:
        if self._terminal_returncode is not None:
            return self._terminal_returncode
        if self._poll_values:
            value = self._poll_values.pop(0)
            if value is not None:
                self._terminal_returncode = value
            return value
        return None

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_raises:
            raise RuntimeError("terminate failed")
        self._terminal_returncode = -15


class CandidateSpDeps:
    def __init__(self, result: object) -> None:
        self.result = result
        self.finalize_calls: list[tuple[object, str | None, str | None]] = []

    def finalize_xtb_job(
        self,
        running: object,
        *,
        forced_status: str | None = None,
        forced_reason: str | None = None,
    ) -> object:
        self.finalize_calls.append((running, forced_status, forced_reason))
        return self.result


def make_ranking_result(
    candidate_path: Path, *, status: str = "completed", reason: str = "completed"
) -> runner_mod.XtbRunResult:
    return runner_mod.XtbRunResult(
        status=status,
        reason=reason,
        command=("xtb", str(candidate_path)),
        exit_code=0 if status == "completed" else 1,
        started_at="2026-04-20T00:00:00Z",
        finished_at="2026-04-20T00:05:00Z",
        stdout_log=str((candidate_path.parent / "xtb.stdout.log").resolve()),
        stderr_log=str((candidate_path.parent / "xtb.stderr.log").resolve()),
        selected_input_xyz=str(candidate_path.resolve()),
        job_type="sp",
        reaction_key="rxn-1",
        input_summary={"input_xyz": str(candidate_path.resolve())},
        candidate_count=1,
        selected_candidate_paths=(str(candidate_path.resolve()),),
        candidate_details=(),
        analysis_summary={},
        manifest_path="",
        resource_request={"max_cores": 4, "max_memory_gb": 12},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 12},
    )


def make_entry(
    job_dir: Path,
    selected_input_xyz: Path,
    *,
    queue_id: str = "queue-1",
    job_id: str = "job-1",
    app_name: str = "orca_auto_xtb",
    job_type: str = "path_search",
    reaction_key: str = "reaction-1",
    input_summary: dict[str, object] | None = None,
    status: str = "running",
    cancel_requested: bool = False,
) -> SimpleNamespace:
    reaction_key = reaction_key or reaction_key_from_job_dir(job_dir)
    resource_request = {"max_cores": 4, "max_memory_gb": 8}
    selected_descriptor = snapshot_input_file(job_dir, selected_input_xyz, role="selected")
    selected_snapshot = Path(selected_descriptor["snapshot_path"])
    summary = {"candidate_count": 0, "candidate_paths": [], **dict(input_summary or {})}
    raw_candidate_paths = summary.get("candidate_paths", [])
    assert isinstance(raw_candidate_paths, list)
    summary["candidate_paths"] = [
        str(selected_snapshot)
        if Path(str(path)).expanduser().resolve() == selected_input_xyz.expanduser().resolve()
        else str(Path(str(path)).expanduser().resolve())
        for path in raw_candidate_paths
    ]
    input_snapshots = {"selected": selected_descriptor}
    secondary_snapshot = ""
    if job_type == "path_search":
        secondary_source = job_dir / "product_test.xyz"
        secondary_source.write_bytes(selected_snapshot.read_bytes())
        secondary_descriptor = snapshot_input_file(job_dir, secondary_source, role="secondary")
        input_snapshots["secondary"] = secondary_descriptor
        secondary_snapshot = str(secondary_descriptor["snapshot_path"])
        summary.update(
            {
                "reactant_xyz": str(selected_snapshot),
                "product_xyz": secondary_snapshot,
                "reactant_count": 1,
                "product_count": 1,
            }
        )
    elif job_type == "ranking":
        summary.update(
            {
                "candidate_count": 1,
                "candidate_paths": [str(selected_snapshot)],
                "candidates_dir": str(selected_snapshot.parent),
                "top_n": 3,
                "estimated_evaluations": 1,
                "max_ranking_evaluations": 100,
            }
        )
    else:
        summary["input_xyz"] = str(selected_snapshot)
    executable_path = job_dir / ".xtb_test_executable"
    executable_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable_path.chmod(0o700)
    executable_identity = engine_runner.executable_identity(executable_path)
    manifest = {
        "job_type": job_type,
        "resources": resource_request,
        "_orca_auto_xtb_executable": executable_identity["path"],
    }
    if job_type == "ranking":
        manifest["top_n"] = 3
    manifest_descriptor = snapshot_input_payload(
        job_dir,
        json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        role="manifest",
        suffix=".json",
        source_path=job_dir / "xtb_job.yaml",
    )
    execution_snapshot = {
        "version": 1,
        "manifest": manifest,
        "input_snapshots": {**input_snapshots, "manifest": manifest_descriptor},
        "selected_input_xyz": str(selected_snapshot),
        "secondary_input_xyz": secondary_snapshot,
        "job_type": job_type,
        "reaction_key": reaction_key,
        "input_summary": summary,
        "resource_request": resource_request,
        "manifest_path": manifest_descriptor["snapshot_path"],
        "executable_identities": {"xtb": executable_identity},
    }
    if selected_input_xyz != selected_snapshot:
        selected_input_xyz.unlink()
        selected_input_xyz.symlink_to(selected_snapshot)
    return SimpleNamespace(
        queue_id=queue_id,
        task_id=job_id,
        app_name=app_name,
        task_kind=f"xtb_{job_type}",
        engine="xtb",
        priority=10,
        enqueued_at="2026-04-19T23:59:00Z",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_snapshot),
            "job_type": job_type,
            "reaction_key": reaction_key,
            "input_summary": summary,
            "resource_request": resource_request,
            "execution_snapshot": execution_snapshot,
        },
        started_at="2026-04-20T00:00:00Z",
        status=SimpleNamespace(value=status),
        cancel_requested=cancel_requested,
        error="",
    )


def make_result(
    selected_input_xyz: Path,
    *,
    status: str,
    reason: str,
    job_type: str = "path_search",
    reaction_key: str = "reaction-1",
    candidate_paths: tuple[str, ...] = (),
) -> queue_cmd.XtbRunResult:
    resource_request = {"max_cores": 4, "max_memory_gb": 8}
    resource_actual = {"assigned_cores": 4, "memory_limit_gb": 8}
    resolved_selected = selected_input_xyz.resolve()
    normalized_candidate_paths = [
        str(Path(path).expanduser().resolve()) for path in candidate_paths
    ]
    if job_type == "path_search":
        input_summary = {
            "candidate_count": len(normalized_candidate_paths),
            "candidate_paths": normalized_candidate_paths,
            "reactant_xyz": str(resolved_selected),
            "product_xyz": str(resolved_selected),
            "reactant_count": 1,
            "product_count": 1,
        }
    elif job_type == "ranking":
        input_summary = {
            "candidate_count": 1,
            "candidate_paths": [str(resolved_selected)],
            "candidates_dir": str(resolved_selected.parent),
            "top_n": 3,
            "estimated_evaluations": 1,
            "max_ranking_evaluations": 100,
        }
    else:
        input_summary = {"input_xyz": str(resolved_selected)}
    stdout_log = selected_input_xyz.parent / "xtb.stdout.log"
    stderr_log = selected_input_xyz.parent / "xtb.stderr.log"
    if status == "completed":
        stdout_log.write_text("completed\n", encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")
    return queue_cmd.XtbRunResult(
        status=status,
        reason=reason,
        command=("xtb", str(selected_input_xyz)),
        exit_code=0 if status == "completed" else 1,
        started_at="2026-04-20T00:00:00Z",
        finished_at="2026-04-20T00:05:00Z",
        stdout_log=str(stdout_log.resolve()),
        stderr_log=str(stderr_log.resolve()),
        selected_input_xyz=str(resolved_selected),
        job_type=job_type,
        reaction_key=reaction_key,
        input_summary=input_summary,
        candidate_count=len(candidate_paths),
        selected_candidate_paths=tuple(normalized_candidate_paths),
        candidate_details=tuple(
            {"path": str(Path(path).expanduser().resolve())} for path in candidate_paths
        ),
        analysis_summary={
            "candidate_paths": [str(Path(path).expanduser().resolve()) for path in candidate_paths],
            **(
                {"best_candidate_path": normalized_candidate_paths[0]}
                if job_type == "ranking" and normalized_candidate_paths
                else {}
            ),
        },
        manifest_path="",
        resource_request=resource_request,
        resource_actual=resource_actual,
    )


def fake_reserve_slot(
    calls: list[tuple[str, int, str, str]],
    root: str,
    limit: int,
    source: str,
    app_name: str,
) -> str:
    calls.append((root, limit, source, app_name))
    return "slot-1"


def record_finished_call(
    finished_calls: list[dict[str, object]], kwargs: dict[str, object]
) -> bool:
    finished_calls.append(kwargs)
    return True


__all__ = [
    "CandidateSpDeps",
    "FakeCandidateProcess",
    "fake_reserve_slot",
    "make_cfg",
    "make_entry",
    "make_ranking_result",
    "make_result",
    "make_runner_cfg",
    "record_finished_call",
    "write_multi_xyz",
    "write_xyz",
]
