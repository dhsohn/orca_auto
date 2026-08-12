from __future__ import annotations

import json
import math
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from orca_auto.core import engine_runner, engine_scratch
from orca_auto.core.config import ScratchConfig
from orca_auto.core.config.engines import (
    WorkflowEngineAppConfig as AppConfig,
)
from orca_auto.flow.engines.xtb import ranking_inputs, runner_artifacts
from orca_auto.flow.engines.xtb import runner as runner_mod
from orca_auto.flow.engines.xtb.ranking_selection import (
    rank_usable_candidates,
    usable_ranking_candidates,
)
from tests.execution_snapshot_helpers import stage_execution_snapshot
from tests.flow.engines.xtb.factories import (
    FakeCandidateProcess,
    make_ranking_result,
)
from tests.flow.engines.xtb.factories import (
    make_runner_cfg as _cfg,
)
from tests.flow.engines.xtb.factories import (
    write_multi_xyz as _write_multi_xyz,
)
from tests.flow.engines.xtb.factories import (
    write_xyz as _write_xyz,
)


def test_resolve_xtb_executable_uses_configured_and_path_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "xtb"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert runner_mod._resolve_xtb_executable(
        _cfg(tmp_path, xtb_executable=str(executable))
    ) == str(executable.resolve())

    with pytest.raises(ValueError, match="Configured xTB executable not found"):
        runner_mod._resolve_xtb_executable(
            _cfg(tmp_path, xtb_executable=str(tmp_path / "missing-xtb"))
        )

    not_executable = tmp_path / "bin" / "xtb-not-executable"
    not_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    not_executable.chmod(0o644)
    with pytest.raises(ValueError, match="Configured xTB executable is not executable"):
        runner_mod._resolve_xtb_executable(_cfg(tmp_path, xtb_executable=str(not_executable)))

    monkeypatch.setattr(
        engine_runner.shutil,
        "which",
        lambda name: "/usr/bin/xtb" if name == "xtb" else None,
    )
    assert runner_mod._resolve_xtb_executable(_cfg(tmp_path)) == "/usr/bin/xtb"

    monkeypatch.setattr(engine_runner.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="xTB executable not configured and not found on PATH"):
        runner_mod._resolve_xtb_executable(_cfg(tmp_path))


def test_build_command_handles_job_types_and_manifest_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(tmp_path)
    selected_xyz = _write_xyz(tmp_path / "job" / "reactant.xyz")
    product_xyz = _write_xyz(tmp_path / "job" / "product.xyz")

    monkeypatch.setattr(runner_mod, "_resolve_xtb_executable", lambda cfg_obj: "/usr/bin/xtb")

    path_search = runner_mod._build_command(
        cfg,
        manifest={
            "gfn": "1",
            "charge": "2",
            "uhf": 1,
            "solvent_model": "gbsa",
            "solvent": "water",
            "xcontrol": "input.inp",
            "dry_run": "true",
            "resources": {"max_cores": 10, "max_memory_gb": 28},
        },
        selected_input_xyz=selected_xyz,
        secondary_input_xyz=product_xyz,
        job_type="path_search",
    )
    assert path_search == [
        "/usr/bin/xtb",
        str(selected_xyz),
        "--parallel",
        "10",
        "--json",
        "--norestart",
        "--gfn",
        "1",
        "--chrg",
        "2",
        "--uhf",
        "1",
        "--gbsa",
        "water",
        "--input",
        "input.inp",
        "--path",
        str(product_xyz),
        "--define",
    ]

    opt_command = runner_mod._build_command(
        cfg,
        manifest={"opt_level": "tight"},
        selected_input_xyz=selected_xyz,
        secondary_input_xyz=None,
        job_type="opt",
    )
    assert opt_command[-2:] == ["--opt", "tight"]

    sp_command = runner_mod._build_command(
        cfg,
        manifest={},
        selected_input_xyz=selected_xyz,
        secondary_input_xyz=None,
        job_type="sp",
    )
    assert sp_command[-1] == "--sp"

    with pytest.raises(ValueError, match="path_search requires a product/reference structure"):
        runner_mod._build_command(
            cfg,
            manifest={},
            selected_input_xyz=selected_xyz,
            secondary_input_xyz=None,
            job_type="path_search",
        )

    with pytest.raises(ValueError, match="Unsupported xtb job_type: weird"):
        runner_mod._build_command(
            cfg,
            manifest={},
            selected_input_xyz=selected_xyz,
            secondary_input_xyz=None,
            job_type="weird",
        )


def test_extract_sp_energy_prefers_xtbout_then_string_then_comment(tmp_path: Path) -> None:
    job_dir = tmp_path / "sp-job"
    candidate_xyz = _write_xyz(job_dir / "candidate.xyz", comment="energy = -17.25")

    (job_dir / "xtbout.json").write_text(json.dumps({"total energy": -11.5}), encoding="utf-8")
    assert runner_mod._extract_sp_energy(job_dir, candidate_xyz) == (
        -11.5,
        "xtbout.json:total energy",
    )

    (job_dir / "xtbout.json").write_text(json.dumps({"total energy": "-12.75"}), encoding="utf-8")
    assert runner_mod._extract_sp_energy(job_dir, candidate_xyz) == (
        -12.75,
        "xtbout.json:total energy",
    )

    (job_dir / "xtbout.json").write_text(
        json.dumps({"electronic energy": "-1.25E+02"}),
        encoding="utf-8",
    )
    assert runner_mod._extract_sp_energy(job_dir, candidate_xyz) == (
        -125.0,
        "xtbout.json:electronic energy",
    )

    (job_dir / "xtbout.json").unlink()
    assert runner_mod._extract_sp_energy(job_dir, candidate_xyz) == (None, "")

    candidate_xyz.write_text("3\nno energy here\nH 0 0 0\nH 0 0 1\nH 0 1 0\n", encoding="utf-8")
    assert runner_mod._extract_sp_energy(job_dir, candidate_xyz) == (None, "")


def test_extract_sp_energy_parses_exponents_and_rejects_json_boolean(tmp_path: Path) -> None:
    job_dir = tmp_path / "sp"
    job_dir.mkdir()
    candidate_xyz = _write_xyz(job_dir / "candidate.xyz", comment="energy = -1.0E+02")

    (job_dir / "xtbout.json").write_text(json.dumps({"total energy": True}), encoding="utf-8")

    assert runner_mod._extract_sp_energy(job_dir, candidate_xyz) == (None, "")
    count, paths, _details, summary = runner_mod._collect_sp_candidates(job_dir)
    assert count == 0
    assert paths == ()
    assert "no finite energy" in summary["result_validation_error"]


def test_path_summary_parses_scientific_notation(tmp_path: Path) -> None:
    job_dir = tmp_path / "path"
    job_dir.mkdir()
    stdout = job_dir / "xtb.stdout.log"
    stdout.write_text(
        "forward barrier (kcal) : 1.2E+02\n"
        "backward barrier (kcal) : 3.5e+01\n"
        "reaction energy (kcal) : -4.5E-01\n",
        encoding="utf-8",
    )

    summary = runner_artifacts._parse_path_search_stdout(job_dir, str(stdout))

    assert summary["forward_barrier_kcal"] == 120.0
    assert summary["backward_barrier_kcal"] == 35.0
    assert summary["reaction_energy_kcal"] == -0.45


def test_path_summary_drops_overflowed_scientific_notation(tmp_path: Path) -> None:
    job_dir = tmp_path / "path-overflow"
    job_dir.mkdir()
    stdout = job_dir / "xtb.stdout.log"
    stdout.write_text(
        "forward barrier (kcal) : 1E999\n"
        "run 1 barrier: 1E999 dE: -1E999 product-end path RMSD: 1E999\n",
        encoding="utf-8",
    )

    summary = runner_artifacts._parse_path_search_stdout(job_dir, str(stdout))

    assert "forward_barrier_kcal" not in summary
    assert "path_trials" not in summary


@pytest.mark.parametrize("energy", [math.nan, math.inf, -math.inf, "NaN", "Infinity"])
def test_nonfinite_sp_energy_is_never_rankable(tmp_path: Path, energy: object) -> None:
    job_dir = tmp_path / "sp-job"
    candidate_xyz = _write_xyz(job_dir / "candidate.xyz")
    (job_dir / "xtbout.json").write_text(json.dumps({"total energy": energy}), encoding="utf-8")

    assert runner_mod._extract_sp_energy(job_dir, candidate_xyz) == (None, "")

    candidates = [
        {
            "candidate_path": "nonfinite.xyz",
            "candidate_run_dir_path": "nonfinite",
            "energy_source": "test",
            "status": "completed",
            "reason": "completed",
            "exit_code": 0,
            "total_energy": energy,
        },
        {
            "candidate_path": "finite.xyz",
            "candidate_run_dir_path": "finite",
            "energy_source": "test",
            "status": "completed",
            "reason": "completed",
            "exit_code": 0,
            "total_energy": -10.0,
        },
    ]
    usable = usable_ranking_candidates(candidates)
    ranked, details, selected = rank_usable_candidates(usable, top_n=1)

    assert [item["candidate_path"] for item in ranked] == ["finite.xyz"]
    assert details[0]["score"] == 10.0
    assert selected == ["finite.xyz"]


def test_runner_helper_functions_cover_invalid_and_fallback_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert engine_runner.manifest_int({"charge": "   "}, "charge") is None
    with pytest.raises(ValueError, match="must be an integer-compatible value"):
        engine_runner.manifest_int({"charge": object()}, "charge")
    for invalid_integer in (True, 1.5, float("nan")):
        with pytest.raises(ValueError, match="must be an integer"):
            engine_runner.manifest_int({"charge": invalid_integer}, "charge")

    assert runner_artifacts._safe_float("not-a-number") is None
    assert runner_artifacts._safe_float(True) is None
    with pytest.raises(ValueError, match="top_n must be a positive integer"):
        ranking_inputs.ranking_top_n({"top_n": "bad"})

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    assert runner_artifacts._resolve_existing_path(job_dir, "missing.xyz") == ""

    original_resolve = runner_mod.Path.resolve

    def fake_resolve(self: Path) -> Path:
        if self.name == "boom.xyz":
            raise OSError("boom")
        return original_resolve(self)

    monkeypatch.setattr(runner_artifacts.Path, "resolve", fake_resolve, raising=False)
    assert runner_artifacts._resolve_existing_path(job_dir, "boom.xyz") == ""

    invalid_json_dir = tmp_path / "invalid-json"
    invalid_json_dir.mkdir()
    (invalid_json_dir / "xtbout.json").write_text("{not-json", encoding="utf-8")
    assert runner_artifacts._load_xtbout_json(invalid_json_dir) == {}


def test_run_xtb_ranking_job_returns_failed_result_when_no_usable_energy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "ranking-job"
    candidate_a = _write_xyz(job_dir / "candidates" / "a.xyz")
    candidate_b = _write_xyz(job_dir / "candidates" / "b.xyz")

    monkeypatch.setattr(runner_mod, "load_job_manifest", lambda path: {"top_n": 2})
    monkeypatch.setattr(
        runner_mod,
        "resolve_job_inputs",
        lambda job_dir_path, manifest: {
            "job_type": "ranking",
            "reaction_key": "rxn-1",
            "input_summary": {
                "candidate_paths": [str(candidate_a.resolve()), str(candidate_b.resolve())],
                "top_n": 2,
            },
        },
    )
    monkeypatch.setattr(
        runner_mod,
        "_run_candidate_sp_job",
        lambda cfg_obj, *, candidate_xyz, candidate_run_dir, manifest: make_ranking_result(
            candidate_xyz,
            status="failed",
            reason="xtb_exit_code_1",
        ),
    )
    monkeypatch.setattr(
        runner_mod, "_extract_sp_energy", lambda job_dir_path, candidate_xyz: (None, "")
    )
    monkeypatch.setattr(runner_mod, "now_utc_iso", lambda: "2026-04-20T00:00:00Z")

    result = runner_mod.run_xtb_ranking_job(cfg, job_dir=job_dir)

    assert result.status == "failed"
    assert result.reason == "ranking_no_usable_energy"
    assert result.candidate_count == 2
    assert result.selected_candidate_paths == ()
    assert result.analysis_summary["failure_reason"] == "ranking_no_usable_energy"
    assert result.analysis_summary["top_n"] == 2
    assert all(item["selected"] is False for item in result.candidate_details)
    assert (
        Path(result.stdout_log).read_text(encoding="utf-8")
        == "ranking failed: ranking_no_usable_energy\n"
    )
    assert (
        Path(result.stderr_log).read_text(encoding="utf-8")
        == "no candidate produced a usable xTB energy\n"
    )


def test_run_candidate_sp_job_writes_scaffold_and_finalizes_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(tmp_path)
    candidate_xyz = _write_xyz(tmp_path / "input-candidates" / "rank 1.xyz")
    candidate_run_dir = tmp_path / "ranking-job" / ".ranking_runs" / "01_rank_1"
    started: list[tuple[Path, Path]] = []
    finalized: list[object] = []

    sentinel_running = SimpleNamespace(process=FakeCandidateProcess([0]))
    expected_result = make_ranking_result(candidate_xyz)

    def fake_start_xtb_job(
        cfg_obj: AppConfig, *, job_dir: Path, selected_input_xyz: Path
    ) -> object:
        started.append((job_dir, selected_input_xyz))
        return sentinel_running

    def fake_finalize_xtb_job(running: object) -> runner_mod.XtbRunResult:
        finalized.append(running)
        return expected_result

    monkeypatch.setattr(runner_mod, "start_xtb_job", fake_start_xtb_job)
    monkeypatch.setattr(runner_mod, "finalize_xtb_job", fake_finalize_xtb_job)

    result = runner_mod._run_candidate_sp_job(
        cfg,
        candidate_xyz=candidate_xyz,
        candidate_run_dir=candidate_run_dir,
        manifest={"job_type": "ranking", "top_n": 2},
    )

    manifest = yaml.safe_load((candidate_run_dir / "xtb_job.yaml").read_text(encoding="utf-8"))
    assert result.status == expected_result.status
    assert result.reason == expected_result.reason
    assert len(result.analysis_summary["executed_input_sha256"]) == 64
    assert result.analysis_summary["executed_input_size_bytes"] == candidate_xyz.stat().st_size
    assert started == [(candidate_run_dir, candidate_run_dir / "input.xyz")]
    assert finalized == [sentinel_running]
    assert (candidate_run_dir / "input.xyz").read_text(encoding="utf-8") == candidate_xyz.read_text(
        encoding="utf-8"
    )
    assert manifest["job_type"] == "sp"
    assert manifest["input_xyz"] == "input.xyz"


def test_run_candidate_sp_job_cancels_process_and_clears_running_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(tmp_path)
    candidate_xyz = _write_xyz(tmp_path / "input-candidates" / "rank 2.xyz")
    candidate_run_dir = tmp_path / "ranking-job" / ".ranking_runs" / "02_rank_2"
    process = FakeCandidateProcess([None])
    running = SimpleNamespace(process=process)
    expected_result = make_ranking_result(
        candidate_xyz,
        status="cancelled",
        reason="cancel_requested",
    )
    running_callbacks: list[object | None] = []
    stopped_processes: list[object] = []

    def fake_finalize_xtb_job(
        finalized_running: object,
        *,
        forced_status: str | None = None,
        forced_reason: str | None = None,
    ) -> runner_mod.XtbRunResult:
        assert finalized_running is running
        assert forced_status == "cancelled"
        assert forced_reason == "cancel_requested"
        return expected_result

    monkeypatch.setattr(
        runner_mod,
        "start_xtb_job",
        lambda cfg_obj, *, job_dir, selected_input_xyz: running,
    )
    monkeypatch.setattr(runner_mod, "finalize_xtb_job", fake_finalize_xtb_job)

    def terminate_process(actual_process: Any) -> bool:
        assert isinstance(actual_process, FakeCandidateProcess)
        stopped_processes.append(actual_process)
        actual_process.terminate()
        return True

    result = runner_mod._run_candidate_sp_job(
        cfg,
        candidate_xyz=candidate_xyz,
        candidate_run_dir=candidate_run_dir,
        manifest={"job_type": "ranking"},
        should_cancel=lambda: True,
        on_running_job=running_callbacks.append,
        terminate_process=terminate_process,
    )

    manifest = yaml.safe_load((candidate_run_dir / "xtb_job.yaml").read_text(encoding="utf-8"))
    assert result.status == expected_result.status
    assert result.reason == expected_result.reason
    assert len(result.analysis_summary["executed_input_sha256"]) == 64
    assert result.analysis_summary["executed_input_size_bytes"] == candidate_xyz.stat().st_size
    assert manifest["job_type"] == "sp"
    assert manifest["input_xyz"] == "input.xyz"
    assert running_callbacks == [running, None]
    assert stopped_processes == [process]
    assert process.terminate_calls == 1


def test_run_candidate_sp_job_rejects_bound_input_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(tmp_path)
    candidate_xyz = _write_xyz(tmp_path / "input-candidates" / "candidate.xyz")
    candidate_run_dir = tmp_path / "ranking-job" / ".ranking_runs" / "01_candidate"
    running = SimpleNamespace(process=FakeCandidateProcess([0]))

    monkeypatch.setattr(
        runner_mod,
        "start_xtb_job",
        lambda cfg_obj, *, job_dir, selected_input_xyz: running,
    )

    def mutate_then_finalize(actual_running: object) -> runner_mod.XtbRunResult:
        assert actual_running is running
        (candidate_run_dir / "input.xyz").write_text(
            "1\nmutated\nHe 0 0 0\n",
            encoding="utf-8",
        )
        return make_ranking_result(candidate_xyz)

    monkeypatch.setattr(runner_mod, "finalize_xtb_job", mutate_then_finalize)

    result = runner_mod._run_candidate_sp_job(
        cfg,
        candidate_xyz=candidate_xyz,
        candidate_run_dir=candidate_run_dir,
        manifest={"job_type": "ranking"},
    )

    assert result.status == "failed"
    assert result.reason == "candidate_input_changed_during_execution"
    assert (
        result.analysis_summary["expected_input_sha256"]
        != result.analysis_summary["executed_input_sha256"]
    )


def test_run_xtb_ranking_job_requires_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "ranking-job"
    job_dir.mkdir()

    monkeypatch.setattr(runner_mod, "load_job_manifest", lambda path: {"top_n": 2})
    monkeypatch.setattr(
        runner_mod,
        "resolve_job_inputs",
        lambda job_dir_path, manifest: {
            "job_type": "ranking",
            "reaction_key": "rxn-1",
            "input_summary": {"candidate_paths": []},
        },
    )

    with pytest.raises(ValueError, match="No ranking candidates available"):
        runner_mod.run_xtb_ranking_job(cfg, job_dir=job_dir)


def test_run_xtb_ranking_job_selects_lowest_energy_candidates_and_logs_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "ranking-job"
    candidate_a = _write_xyz(job_dir / "candidates" / "alpha.xyz")
    candidate_b = _write_xyz(job_dir / "candidates" / "beta.xyz")
    candidate_c = _write_xyz(job_dir / "candidates" / "gamma.xyz")
    energy_by_candidate = {
        candidate_a.resolve(): (-10.0, "xtbout.json:total energy"),
        candidate_b.resolve(): (-12.5, "xtbout.json:total energy"),
        candidate_c.resolve(): (None, ""),
    }

    monkeypatch.setattr(runner_mod, "load_job_manifest", lambda path: {"top_n": 2})
    monkeypatch.setattr(
        runner_mod,
        "resolve_job_inputs",
        lambda job_dir_path, manifest: {
            "job_type": "ranking",
            "reaction_key": "rxn-1",
            "input_summary": {
                "candidate_paths": [
                    str(candidate_a.resolve()),
                    str(candidate_b.resolve()),
                    str(candidate_c.resolve()),
                ],
                "top_n": 2,
            },
        },
    )

    def fake_run_candidate_sp_job(
        cfg_obj: AppConfig,
        *,
        candidate_xyz: Path,
        candidate_run_dir: Path,
        manifest: dict[str, Any],
    ) -> runner_mod.XtbRunResult:
        status = "completed" if candidate_xyz.resolve() != candidate_c.resolve() else "failed"
        reason = "completed" if status == "completed" else "xtb_exit_code_1"
        return make_ranking_result(candidate_xyz, status=status, reason=reason)

    monkeypatch.setattr(runner_mod, "_run_candidate_sp_job", fake_run_candidate_sp_job)
    monkeypatch.setattr(
        runner_mod,
        "_extract_sp_energy",
        lambda job_dir_path, candidate_xyz: energy_by_candidate[candidate_xyz.resolve()],
    )
    monkeypatch.setattr(runner_mod, "now_utc_iso", lambda: "2026-04-20T00:00:00Z")

    result = runner_mod.run_xtb_ranking_job(cfg, job_dir=job_dir)

    assert result.status == "completed"
    assert result.reason == "completed"
    assert result.selected_input_xyz == str(candidate_b.resolve())
    assert result.selected_candidate_paths == (
        str(candidate_b.resolve()),
        str(candidate_a.resolve()),
    )
    assert result.command == ("xtb", str(candidate_b))
    assert result.analysis_summary["best_candidate_path"] == str(candidate_b.resolve())
    assert result.analysis_summary["best_total_energy"] == -12.5
    assert result.analysis_summary["failed_candidate_count"] == 1
    assert result.analysis_summary["selected_candidate_paths"] == [
        str(candidate_b.resolve()),
        str(candidate_a.resolve()),
    ]
    assert result.analysis_summary["command_summary"] == [
        ["xtb", str(candidate_a)],
        ["xtb", str(candidate_b)],
        ["xtb", str(candidate_c)],
    ]
    assert [item["path"] for item in result.candidate_details] == [
        str(candidate_b.resolve()),
        str(candidate_a.resolve()),
    ]
    assert [item["rank"] for item in result.candidate_details] == [1, 2]
    assert [item["selected"] for item in result.candidate_details] == [True, True]
    assert result.candidate_details[0] == {
        "rank": 1,
        "kind": "ranking_candidate",
        "path": str(candidate_b.resolve()),
        "candidate_run_dir_path": str((job_dir / ".ranking_runs" / "02_beta").resolve()),
        "energy_source": "xtbout.json:total energy",
        "total_energy": -12.5,
        "score": 12.5,
        "status": "completed",
        "reason": "completed",
        "exit_code": 0,
        "selected": True,
    }
    stdout_text = Path(result.stdout_log).read_text(encoding="utf-8")
    assert "ranking completed: evaluated=3 selected=2" in stdout_text
    assert "failed_candidates: 1" in stdout_text


def test_run_xtb_ranking_job_returns_cancelled_when_cancel_requested_mid_ranking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "ranking-job"
    candidate_a = _write_xyz(job_dir / "candidates" / "alpha.xyz")
    candidate_b = _write_xyz(job_dir / "candidates" / "beta.xyz")
    cancel_checks = 0
    evaluated: list[Path] = []

    monkeypatch.setattr(runner_mod, "load_job_manifest", lambda path: {"top_n": 2})
    monkeypatch.setattr(
        runner_mod,
        "resolve_job_inputs",
        lambda job_dir_path, manifest: {
            "job_type": "ranking",
            "reaction_key": "rxn-1",
            "input_summary": {
                "candidate_paths": [
                    str(candidate_a.resolve()),
                    str(candidate_b.resolve()),
                ],
                "top_n": 2,
            },
        },
    )

    def should_cancel() -> bool:
        nonlocal cancel_checks
        cancel_checks += 1
        return cancel_checks >= 2

    def fake_run_candidate_sp_job(
        cfg_obj: AppConfig,
        *,
        candidate_xyz: Path,
        candidate_run_dir: Path,
        manifest: dict[str, Any],
        should_cancel: Any = None,
        on_running_job: Any = None,
        terminate_process: Any = None,
    ) -> runner_mod.XtbRunResult:
        evaluated.append(candidate_xyz)
        return make_ranking_result(candidate_xyz)

    monkeypatch.setattr(runner_mod, "_run_candidate_sp_job", fake_run_candidate_sp_job)
    monkeypatch.setattr(
        runner_mod,
        "_extract_sp_energy",
        lambda job_dir_path, candidate_xyz: (-10.0, "xtbout.json:total energy"),
    )
    monkeypatch.setattr(runner_mod, "now_utc_iso", lambda: "2026-04-20T00:00:00Z")

    result = runner_mod.run_xtb_ranking_job(cfg, job_dir=job_dir, should_cancel=should_cancel)

    assert result.status == "cancelled"
    assert result.reason == "cancel_requested"
    assert evaluated == [candidate_a]
    assert result.command == ("xtb", str(candidate_a))
    assert result.selected_candidate_paths == ()
    assert result.candidate_count == 2
    assert result.analysis_summary["evaluated_candidate_count"] == 1
    assert result.analysis_summary["candidate_paths"] == [str(candidate_a.resolve())]
    assert result.candidate_details == (
        {
            "rank": 1,
            "kind": "ranking_candidate",
            "path": str(candidate_a.resolve()),
            "candidate_run_dir_path": str((job_dir / ".ranking_runs" / "01_alpha").resolve()),
            "energy_source": "xtbout.json:total energy",
            "status": "completed",
            "reason": "completed",
            "exit_code": 0,
            "selected": False,
        },
    )
    assert (
        Path(result.stdout_log).read_text(encoding="utf-8")
        == "ranking cancelled: cancel_requested\n"
    )


def test_parse_path_search_stdout_and_candidate_collection_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "path-job"
    job_dir.mkdir()
    stdout_log = job_dir / "missing.stdout.log"
    assert runner_artifacts._parse_path_search_stdout(job_dir, str(stdout_log)) == {}

    fallback_stdout = job_dir / "xtb.stdout.log"
    fallback_stdout.write_text("path output without ranked selections\n", encoding="utf-8")
    path_file = _write_xyz(job_dir / "xtbpath.xyz")
    count, selected_paths, details, summary = runner_mod._collect_path_search_candidates(
        job_dir, str(fallback_stdout)
    )
    assert count == 0
    assert details == ()
    assert selected_paths == (str(path_file.resolve()),)
    assert summary["selected_candidate_paths"] == [str(path_file.resolve())]

    _write_xyz(job_dir / "xtbpath_1.xyz")
    ignored_count, ignored_paths, ignored_details, _ = runner_mod._collect_path_search_candidates(
        job_dir, str(fallback_stdout)
    )
    assert ignored_count == 0
    assert ignored_paths == (str(path_file.resolve()),)
    assert ignored_details == ()


def test_collect_path_search_candidates_parses_stdout_and_keeps_only_ts_and_selected_path(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "path-job"
    job_dir.mkdir()
    ts_guess = _write_xyz(job_dir / "ts_guess.xyz")
    path_file = _write_xyz(job_dir / "xtbpath.xyz")
    selected_path = _write_multi_xyz(
        job_dir / "xtbpath_0.xyz",
        comments=[
            "energy: -10.0",
            "energy: -8.5",
            "energy: -6.0",
            "energy: -3.5",
            "energy: -2.0",
            "energy: -1.0",
            "energy: -4.0",
        ],
    )
    _write_xyz(job_dir / "xtbpath_1.xyz")
    _write_xyz(job_dir / "xtbpath_2.xyz")
    stdout_log = job_dir / "xtb.stdout.log"
    stdout_log.write_text(
        "\n".join(
            [
                "forward barrier (kcal) : 15.5",
                "backward barrier (kcal) : 10.0",
                "reaction energy (kcal) : -3.2",
                f"estimated TS on file {ts_guess.name}",
                "path 0 taken with 12 points",
                "run 1 barrier: 22.0 dE: -0.5 product-end path RMSD: 0.10",
                "run 2 barrier: 18.0 dE: 1.0 product-end path RMSD: 0.20",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    candidate_count, selected_paths, candidate_details, summary = (
        runner_mod._collect_path_search_candidates(
            job_dir,
            str(stdout_log),
        )
    )

    assert candidate_count == 2
    assert selected_paths == (
        str(ts_guess.resolve()),
        str(selected_path.resolve()),
    )
    assert candidate_details[0]["kind"] == "ts_guess"
    assert candidate_details[1]["kind"] == "selected_path"
    assert summary["forward_barrier_kcal"] == 15.5
    assert summary["backward_barrier_kcal"] == 10.0
    assert summary["reaction_energy_kcal"] == -3.2
    assert summary["ts_guess_path"] == str(ts_guess.resolve())
    assert summary["path_file"] == str(path_file.resolve())
    assert summary["selected_path_file"] == str(selected_path.resolve())
    assert summary["selected_path_index"] == 0
    assert summary["selected_path_point_count"] == 12
    assert summary["path_trials"] == [
        {"trial_index": 1, "barrier_kcal": 22.0, "delta_e_kcal": -0.5, "product_end_rmsd": 0.1},
        {"trial_index": 2, "barrier_kcal": 18.0, "delta_e_kcal": 1.0, "product_end_rmsd": 0.2},
    ]
    assert summary["selected_candidate_paths"] == list(selected_paths)


def test_collect_opt_and_sp_candidates_return_expected_metadata(tmp_path: Path) -> None:
    opt_job_dir = tmp_path / "opt-job"
    sp_job_dir = tmp_path / "sp-job"
    optimized_geometry = _write_xyz(opt_job_dir / "xtbopt.xyz")
    (opt_job_dir / "xtbopt.log").write_text("optimization complete\n", encoding="utf-8")
    (opt_job_dir / ".xtboptok").write_text("", encoding="utf-8")

    opt_count, opt_paths, opt_details, opt_summary = runner_mod._collect_opt_candidates(opt_job_dir)
    assert opt_count == 1
    assert opt_paths == (str(optimized_geometry.resolve()),)
    assert opt_details[0]["kind"] == "optimized_geometry"
    assert opt_summary["optimization_ok"] is True

    result_json = sp_job_dir / "xtbout.json"
    result_json.parent.mkdir(parents=True, exist_ok=True)
    result_json.write_text(
        json.dumps({"total energy": -42.0, "electronic energy": -41.5}),
        encoding="utf-8",
    )
    (sp_job_dir / "charges").write_text("charges\n", encoding="utf-8")
    (sp_job_dir / "wbo").write_text("wbo\n", encoding="utf-8")
    (sp_job_dir / "xtbtopo.mol").write_text("topology\n", encoding="utf-8")

    sp_count, sp_paths, sp_details, sp_summary = runner_mod._collect_sp_candidates(sp_job_dir)
    assert sp_count == 1
    assert sp_paths == (str(result_json.resolve()),)
    assert sp_details[0]["kind"] == "single_point_result"
    assert sp_details[0]["total_energy"] == -42.0
    assert sp_details[0]["score"] == 42.0
    assert sp_summary["canonical_result_path"] == str(result_json.resolve())
    assert sp_summary["total_energy"] == -42.0
    assert sp_summary["electronic_energy"] == -41.5

    empty_opt_dir = tmp_path / "empty-opt"
    empty_sp_dir = tmp_path / "empty-sp"
    empty_opt_dir.mkdir()
    empty_sp_dir.mkdir()

    assert runner_mod._collect_opt_candidates(empty_opt_dir) == (
        0,
        (),
        (),
        {
            "canonical_result_path": "",
            "optimization_log_path": "",
            "optimization_ok": False,
        },
    )
    assert runner_mod._collect_sp_candidates(empty_sp_dir) == (
        0,
        (),
        (),
        {
            "canonical_result_path": "",
            "charges_path": "",
            "wbo_path": "",
            "topology_path": "",
        },
    )


def test_collect_opt_and_sp_candidates_reject_nonfinite_results(tmp_path: Path) -> None:
    opt_job_dir = tmp_path / "invalid-opt"
    invalid_geometry = opt_job_dir / "xtbopt.xyz"
    invalid_geometry.parent.mkdir(parents=True)
    invalid_geometry.write_text("1\ninvalid\nH nan 0 0\n", encoding="utf-8")
    (opt_job_dir / ".xtboptok").write_text("", encoding="utf-8")

    opt_count, opt_paths, opt_details, opt_summary = runner_mod._collect_opt_candidates(opt_job_dir)
    assert (opt_count, opt_paths, opt_details) == (0, (), ())
    assert opt_summary["canonical_result_path"] == ""
    assert "exactly one valid finite XYZ frame" in opt_summary["result_validation_error"]

    sp_job_dir = tmp_path / "invalid-sp"
    sp_job_dir.mkdir()
    (sp_job_dir / "xtbout.json").write_text(
        '{"total energy": NaN, "electronic energy": "inf"}',
        encoding="utf-8",
    )

    sp_count, sp_paths, sp_details, sp_summary = runner_mod._collect_sp_candidates(sp_job_dir)
    assert (sp_count, sp_paths, sp_details) == (0, (), ())
    assert sp_summary["canonical_result_path"] == ""
    assert "no finite energy" in sp_summary["result_validation_error"]


def test_collect_opt_candidate_rejects_atom_identity_mismatch(tmp_path: Path) -> None:
    job_dir = tmp_path / "opt-mismatch"
    selected_input = _write_xyz(job_dir / "input.xyz")
    (job_dir / "xtbopt.xyz").write_text("1\nstale\nCl 0 0 0\n", encoding="utf-8")
    (job_dir / ".xtboptok").write_text("", encoding="utf-8")

    count, paths, details, summary = runner_mod._collect_opt_candidates(
        job_dir, selected_input_xyz=selected_input
    )

    assert (count, paths, details) == (0, (), ())
    assert "element order" in summary["result_validation_error"]


def test_start_xtb_job_passes_expected_subprocess_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    selected_xyz = _write_xyz(job_dir / "input.xyz")
    secondary_xyz = _write_xyz(job_dir / "product.xyz")
    stale_ts = _write_xyz(job_dir / "xtbpath_ts.xyz")
    popen_calls: dict[str, Any] = {}
    build_command_calls: list[dict[str, Any]] = []

    class _FakeProcess:
        def poll(self) -> int | None:
            return 0

    def fake_popen(*args: Any, **kwargs: Any) -> _FakeProcess:
        popen_calls["args"] = args
        popen_calls["kwargs"] = kwargs
        return _FakeProcess()

    def fake_build_command(
        cfg_obj: AppConfig,
        *,
        manifest: dict[str, Any],
        selected_input_xyz: Path,
        secondary_input_xyz: Path | None,
        job_type: str,
        resource_request: dict[str, int] | None = None,
    ) -> list[str]:
        build_command_calls.append(
            {
                "manifest": manifest,
                "selected_input_xyz": selected_input_xyz,
                "secondary_input_xyz": secondary_input_xyz,
                "job_type": job_type,
            }
        )
        return ["xtb", str(selected_input_xyz), "--sp"]

    monkeypatch.setattr(
        runner_mod,
        "load_job_manifest",
        lambda path: {
            "job_type": "path_search",
            "resources": {"max_cores": 9, "max_memory_gb": 18},
        },
    )
    monkeypatch.setattr(
        runner_mod,
        "resolve_job_inputs",
        lambda job_dir_path, manifest: {
            "job_type": "path_search",
            "reaction_key": "rxn-1",
            "secondary_input_xyz": str(secondary_xyz.resolve()),
            "input_summary": {"product_xyz": str(secondary_xyz.resolve())},
        },
    )
    monkeypatch.setattr(runner_mod, "_build_command", fake_build_command)
    monkeypatch.setattr(runner_mod, "now_utc_iso", lambda: "2026-04-20T00:00:00Z")
    monkeypatch.setattr(runner_mod.subprocess, "Popen", fake_popen)

    running = runner_mod.start_xtb_job(cfg, job_dir=job_dir, selected_input_xyz=selected_xyz)

    assert not stale_ts.exists()
    assert build_command_calls == [
        {
            "manifest": {
                "job_type": "path_search",
                "resources": {"max_cores": 9, "max_memory_gb": 18},
            },
            "selected_input_xyz": selected_xyz,
            "secondary_input_xyz": secondary_xyz.resolve(),
            "job_type": "path_search",
        }
    ]
    assert running.command == ("xtb", str(selected_xyz), "--sp")
    assert running.started_at == "2026-04-20T00:00:00Z"
    assert running.selected_input_xyz == str(selected_xyz.resolve())
    assert running.job_type == "path_search"
    assert running.reaction_key == "rxn-1"
    assert running.input_summary == {"product_xyz": str(secondary_xyz.resolve())}
    kwargs = popen_calls["kwargs"]
    assert kwargs["cwd"] == job_dir
    assert kwargs["text"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["OMP_NUM_THREADS"] == "9"
    assert kwargs["env"]["OPENBLAS_NUM_THREADS"] == "9"
    assert kwargs["env"]["MKL_NUM_THREADS"] == "9"
    assert kwargs["env"]["NUMEXPR_NUM_THREADS"] == "9"
    assert callable(kwargs["preexec_fn"])
    running.stdout_handle.close()
    running.stderr_handle.close()


def test_xtb_ram_scratch_publishes_only_canonical_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shm = tmp_path / "shm"
    shm.mkdir()
    scratch_root = shm / "orca_auto"
    cfg = replace(
        _cfg(tmp_path),
        scratch=ScratchConfig(root=str(scratch_root), min_free_gb=1),
    )
    job_dir = tmp_path / "job"
    selected_xyz = _write_xyz(job_dir / "input.xyz")
    (job_dir / "xtb_job.yaml").write_text("job_type: opt\n", encoding="utf-8")
    runtime_identity = runner_mod._engine_runner.engine_runtime_identity(job_dir)
    selected_xyz, execution_snapshot = stage_execution_snapshot(
        job_dir,
        selected_xyz,
        engine="xtb",
        manifest={"job_type": "opt", "_orca_auto_runtime_identity": runtime_identity},
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        identity={
            "job_type": "opt",
            "reaction_key": "rxn-1",
            "secondary_input_xyz": "",
            "input_summary": {"input_xyz": str(selected_xyz)},
            "runtime_identity": runtime_identity,
        },
    )
    (job_dir / "xtb_job.yaml").unlink()
    popen_cwd: list[Path] = []
    popen_home: list[Path] = []

    class _FakeProcess:
        def poll(self) -> int:
            return 0

    def fake_popen(*args: Any, **kwargs: Any) -> _FakeProcess:
        del args
        cwd = Path(kwargs["cwd"])
        popen_cwd.append(cwd)
        home = Path(kwargs["env"]["HOME"])
        config_home = Path(kwargs["env"]["XDG_CONFIG_HOME"])
        popen_home.append(home)
        home.joinpath("engine-cache").write_text("tmpfs\n", encoding="utf-8")
        config_home.joinpath("engine.conf").write_text("tmpfs\n", encoding="utf-8")
        (cwd / "xtbopt.xyz").write_bytes(selected_xyz.read_bytes())
        (cwd / ".xtboptok").write_text("", encoding="utf-8")
        (cwd / "large-intermediate.tmp").write_bytes(b"temporary")
        return _FakeProcess()

    monkeypatch.setattr(engine_scratch, "_SCRATCH_ROOT_PARENT", shm)
    monkeypatch.setattr(engine_scratch, "_linux_available_memory_bytes", lambda: 2**63)
    monkeypatch.setattr(
        runner_mod,
        "load_job_manifest",
        lambda _path: {"job_type": "opt", "resources": {"max_cores": 1, "max_memory_gb": 1}},
    )
    monkeypatch.setattr(
        runner_mod,
        "resolve_job_inputs",
        lambda _job_dir, _manifest: {
            "job_type": "opt",
            "reaction_key": "rxn-1",
            "secondary_input_xyz": None,
            "input_summary": {"input_xyz": str(selected_xyz)},
        },
    )
    monkeypatch.setattr(runner_mod, "_build_command", lambda *args, **kwargs: ["xtb"])
    monkeypatch.setattr(runner_mod.subprocess, "Popen", fake_popen)

    running = runner_mod.start_xtb_job(
        cfg,
        job_dir=job_dir,
        selected_input_xyz=selected_xyz,
        execution_snapshot=execution_snapshot,
    )
    result = runner_mod.finalize_xtb_job(running)

    assert len(popen_cwd) == 1
    assert popen_cwd[0].is_relative_to(scratch_root)
    assert popen_home[0].parent == popen_cwd[0]
    assert not (job_dir / "xtb_job.yaml").exists()
    assert (job_dir / "xtbopt.xyz").is_file()
    assert (job_dir / ".xtboptok").is_file()
    assert (job_dir / "xtb.stdout.log").is_file()
    assert not (job_dir / "large-intermediate.tmp").exists()
    assert not (job_dir / ".orca_auto_runtime_home" / "engine-cache").exists()
    assert not (job_dir / ".orca_auto_runtime_home" / ".config" / "engine.conf").exists()
    assert result.status == "completed"
    assert result.scratch_provenance["used"] is True
    assert "large-intermediate.tmp" in result.scratch_provenance["omitted_transient_files"]
    assert not any(path.name.startswith("attempt-") for path in scratch_root.iterdir())


def test_stale_xtb_output_fsync_failure_blocks_launch_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    selected = _write_xyz(job_dir / "input.xyz")
    stale = _write_xyz(job_dir / "xtbpath_ts.xyz")
    monkeypatch.setattr(
        runner_mod,
        "fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("stale unlink fsync failed")),
    )

    with pytest.raises(OSError, match="stale unlink fsync failed"):
        runner_mod._clear_stale_xtb_outputs(
            job_dir,
            job_type="path_search",
            selected_input_xyz=selected,
            secondary_input_xyz=None,
        )

    assert not stale.exists()


def test_finalize_xtb_job_defaults_for_completed_path_search_and_unknown_job_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_job_dir = tmp_path / "path-job"
    path_job_dir.mkdir()
    stdout_path = path_job_dir / "xtb.stdout.log"
    stderr_path = path_job_dir / "xtb.stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")

    class _CompletedProcess:
        def poll(self) -> int:
            return 0

    monkeypatch.setattr(
        runner_mod,
        "_collect_path_search_candidates",
        lambda job_dir, stdout_log, input_summary=None, manifest=None: (
            2,
            (str(path_job_dir / "a.xyz"), str(path_job_dir / "b.xyz")),
            ({"kind": "ts_guess"}, {"kind": "selected_path"}),
            {"path_file": "a.xyz"},
        ),
    )
    _write_xyz(path_job_dir / "a.xyz")
    _write_xyz(path_job_dir / "b.xyz")
    monkeypatch.setattr(runner_mod, "now_utc_iso", lambda: "2026-04-20T00:10:00Z")

    path_running = runner_mod.XtbRunningJob(
        process=cast(Any, _CompletedProcess()),
        command=("xtb", "input.xyz", "--path", "product.xyz"),
        started_at="2026-04-20T00:00:00Z",
        stdout_log=str(stdout_path.resolve()),
        stderr_log=str(stderr_path.resolve()),
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
        selected_input_xyz=str((path_job_dir / "input.xyz").resolve()),
        job_type="path_search",
        reaction_key="rxn-1",
        input_summary={},
        manifest_path=str((path_job_dir / "xtb_job.yaml").resolve()),
        resource_request={"max_cores": 4, "max_memory_gb": 12},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 12},
        job_dir=str(path_job_dir.resolve()),
    )

    path_result = runner_mod.finalize_xtb_job(path_running)
    assert path_result.status == "completed"
    assert path_result.reason == "completed"
    assert path_result.candidate_count == 2
    assert path_result.selected_candidate_paths == (
        str(path_job_dir / "a.xyz"),
        str(path_job_dir / "b.xyz"),
    )

    unknown_job_dir = tmp_path / "unknown-job"
    unknown_job_dir.mkdir()
    unknown_stdout = unknown_job_dir / "xtb.stdout.log"
    unknown_stderr = unknown_job_dir / "xtb.stderr.log"
    unknown_running = runner_mod.XtbRunningJob(
        process=cast(Any, SimpleNamespace(poll=lambda: 5)),
        command=("xtb", "input.xyz"),
        started_at="2026-04-20T00:00:00Z",
        stdout_log=str(unknown_stdout.resolve()),
        stderr_log=str(unknown_stderr.resolve()),
        stdout_handle=unknown_stdout.open("w", encoding="utf-8"),
        stderr_handle=unknown_stderr.open("w", encoding="utf-8"),
        selected_input_xyz=str((unknown_job_dir / "input.xyz").resolve()),
        job_type="mystery",
        reaction_key="rxn-2",
        input_summary={},
        manifest_path=str((unknown_job_dir / "xtb_job.yaml").resolve()),
        resource_request={"max_cores": 4, "max_memory_gb": 12},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 12},
        job_dir=str(unknown_job_dir.resolve()),
    )

    unknown_result = runner_mod.finalize_xtb_job(unknown_running)
    assert unknown_result.status == "failed"
    assert unknown_result.reason == "xtb_exit_code_5"
    assert unknown_result.candidate_count == 0
    assert unknown_result.analysis_summary == {}


def test_finalize_xtb_job_waits_for_process_and_uses_forced_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    stdout_path = job_dir / "xtb.stdout.log"
    stderr_path = job_dir / "xtb.stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")

    class _Process:
        def __init__(self) -> None:
            self.wait_called = False

        def poll(self) -> None:
            return None

        def wait(self) -> int:
            self.wait_called = True
            return 7

    process = _Process()
    optimized_xyz = _write_xyz(job_dir / "optimized.xyz")
    monkeypatch.setattr(
        runner_mod,
        "_collect_opt_candidates",
        lambda path, **kwargs: (
            1,
            (str(optimized_xyz),),
            ({"kind": "optimized_geometry"},),
            {"optimization_ok": True},
        ),
    )
    monkeypatch.setattr(runner_mod, "now_utc_iso", lambda: "2026-04-20T00:10:00Z")

    running = runner_mod.XtbRunningJob(
        process=cast(Any, process),
        command=("xtb", "input.xyz", "--opt", "tight"),
        started_at="2026-04-20T00:00:00Z",
        stdout_log=str(stdout_path.resolve()),
        stderr_log=str(stderr_path.resolve()),
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
        selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        job_type="opt",
        reaction_key="mol-1",
        input_summary={"input_xyz": str((job_dir / "input.xyz").resolve())},
        manifest_path=str((job_dir / "xtb_job.yaml").resolve()),
        resource_request={"max_cores": 4, "max_memory_gb": 12},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 12},
        job_dir=str(job_dir.resolve()),
    )

    result = runner_mod.finalize_xtb_job(
        running,
        forced_status="cancelled",
        forced_reason="cancel_requested",
    )

    assert process.wait_called is True
    assert result.status == "cancelled"
    assert result.reason == "cancel_requested"
    assert result.exit_code == 7
    assert result.candidate_count == 1
    assert result.selected_candidate_paths == (str(optimized_xyz),)
    assert result.analysis_summary == {"optimization_ok": True}


def test_finalize_xtb_job_uses_single_point_candidate_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "sp-job"
    job_dir.mkdir()
    stdout_path = job_dir / "xtb.stdout.log"
    stderr_path = job_dir / "xtb.stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    xtbout_json = job_dir / "xtbout.json"
    xtbout_json.write_text('{"energy": -4.2}\n', encoding="utf-8")

    monkeypatch.setattr(
        runner_mod,
        "_collect_sp_candidates",
        lambda path: (
            1,
            (str(xtbout_json),),
            ({"kind": "single_point_result"},),
            {"total_energy": -4.2},
        ),
    )
    monkeypatch.setattr(runner_mod, "now_utc_iso", lambda: "2026-04-20T00:10:00Z")

    running = runner_mod.XtbRunningJob(
        process=cast(Any, SimpleNamespace(poll=lambda: 0)),
        command=("xtb", "input.xyz", "--sp"),
        started_at="2026-04-20T00:00:00Z",
        stdout_log=str(stdout_path.resolve()),
        stderr_log=str(stderr_path.resolve()),
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
        selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        job_type="sp",
        reaction_key="mol-sp",
        input_summary={},
        manifest_path=str((job_dir / "xtb_job.yaml").resolve()),
        resource_request={"max_cores": 4, "max_memory_gb": 12},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 12},
        job_dir=str(job_dir.resolve()),
    )

    result = runner_mod.finalize_xtb_job(running)
    assert result.status == "completed"
    assert result.reason == "completed"
    assert result.candidate_count == 1
    assert result.selected_candidate_paths == (str(xtbout_json),)
    assert result.analysis_summary == {"total_energy": -4.2}


def test_finalize_xtb_job_rejects_output_mutation_during_identity_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "sp-mutation"
    job_dir.mkdir()
    stdout_path = job_dir / "xtb.stdout.log"
    stderr_path = job_dir / "xtb.stderr.log"
    xtbout_json = job_dir / "xtbout.json"
    xtbout_json.write_text('{"energy": -4.2}\n', encoding="utf-8")
    monkeypatch.setattr(
        runner_mod,
        "_collect_sp_candidates",
        lambda path: (
            1,
            (str(xtbout_json),),
            ({"kind": "single_point_result", "path": str(xtbout_json)},),
            {"total_energy": -4.2},
        ),
    )
    original_identity = engine_runner.confined_output_identity
    mutated = False

    def identity_then_mutate(root: str | Path, path: str | Path) -> dict[str, Any]:
        nonlocal mutated
        identity = original_identity(root, path)
        if Path(path).resolve() == xtbout_json.resolve() and not mutated:
            xtbout_json.write_text('{"energy": -1.0}\n', encoding="utf-8")
            mutated = True
        return identity

    monkeypatch.setattr(runner_mod._engine_runner, "confined_output_identity", identity_then_mutate)
    running = runner_mod.XtbRunningJob(
        process=cast(Any, SimpleNamespace(poll=lambda: 0)),
        command=("xtb", "input.xyz", "--sp"),
        started_at="2026-04-20T00:00:00Z",
        stdout_log=str(stdout_path.resolve()),
        stderr_log=str(stderr_path.resolve()),
        stdout_handle=stdout_path.open("w", encoding="utf-8"),
        stderr_handle=stderr_path.open("w", encoding="utf-8"),
        selected_input_xyz=str((job_dir / "input.xyz").resolve()),
        job_type="sp",
        reaction_key="mol-sp",
        input_summary={},
        manifest_path="",
        resource_request={"max_cores": 4, "max_memory_gb": 12},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 12},
        job_dir=str(job_dir.resolve()),
    )

    result = runner_mod.finalize_xtb_job(running)

    assert mutated is True
    assert result.status == "failed"
    assert result.reason == "xtb_output_changed_during_collection"


def _make_path_search_result(
    job_dir: Path,
    ts_guess_xyz: Path,
    *,
    status: str = "completed",
) -> runner_mod.XtbRunResult:
    import dataclasses

    base = make_ranking_result(ts_guess_xyz, status=status)
    ts_detail = {
        "rank": 1,
        "kind": "ts_guess",
        "path": str(ts_guess_xyz.resolve()),
        "score": 1000.0,
        "selected": True,
    }
    return dataclasses.replace(
        base,
        job_type="path_search",
        candidate_details=(ts_detail,),
        manifest_path=str((job_dir / "xtb_job.yaml").resolve()),
    )


def test_ts_hessian_followup_records_hessian_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "path-search-job"
    ts_guess_xyz = _write_xyz(job_dir / "xtbpath_ts.xyz")
    (job_dir / "xtb_job.yaml").write_text(
        "job_type: path_search\ngfn: 2\ncharge: 0\n", encoding="utf-8"
    )
    result = _make_path_search_result(job_dir, ts_guess_xyz)
    hessian_run_dir = job_dir / runner_mod.TS_HESSIAN_DIR_NAME
    hessian_result = make_ranking_result(ts_guess_xyz)

    def fake_sp_job(cfg_obj: AppConfig, **kwargs: Any) -> runner_mod.XtbRunResult:
        assert kwargs["job_type"] == "hess"
        assert kwargs["candidate_run_dir"] == hessian_run_dir
        hessian_run_dir.mkdir(parents=True, exist_ok=True)
        (hessian_run_dir / "input.xyz").write_bytes(kwargs["candidate_payload"])
        (hessian_run_dir / "hessian").write_text(
            "$hessian\n0.1 0 0 0 0.1 0 0 0 0.1\n", encoding="utf-8"
        )
        return hessian_result

    monkeypatch.setattr(runner_mod, "_run_candidate_sp_job", fake_sp_job)

    updated = runner_mod.run_path_search_ts_hessian_followup(cfg, result, job_dir=job_dir)

    expected_hessian = str((hessian_run_dir / "hessian").resolve())
    assert updated.candidate_details[0]["hessian_path"] == expected_hessian
    assert updated.analysis_summary["ts_hessian_path"] == expected_hessian


def test_ts_hessian_followup_skips_non_path_search_and_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "path-search-job"
    ts_guess_xyz = _write_xyz(job_dir / "xtbpath_ts.xyz")
    (job_dir / "xtb_job.yaml").write_text(
        "job_type: path_search\ngfn: 2\ncharge: 0\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        runner_mod,
        "_run_candidate_sp_job",
        lambda *args, **kwargs: pytest.fail("hessian job should not run"),
    )
    sp_result = make_ranking_result(ts_guess_xyz)
    assert (
        runner_mod.run_path_search_ts_hessian_followup(cfg, sp_result, job_dir=job_dir) is sp_result
    )

    failed = _make_path_search_result(job_dir, ts_guess_xyz, status="failed")
    assert runner_mod.run_path_search_ts_hessian_followup(cfg, failed, job_dir=job_dir) is failed


def test_ts_hessian_followup_falls_back_when_hessian_job_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "path-search-job"
    ts_guess_xyz = _write_xyz(job_dir / "xtbpath_ts.xyz")
    (job_dir / "xtb_job.yaml").write_text(
        "job_type: path_search\ngfn: 2\ncharge: 0\n", encoding="utf-8"
    )
    result = _make_path_search_result(job_dir, ts_guess_xyz)

    monkeypatch.setattr(
        runner_mod,
        "_run_candidate_sp_job",
        lambda *args, **kwargs: make_ranking_result(ts_guess_xyz, status="failed"),
    )
    failed_followup = runner_mod.run_path_search_ts_hessian_followup(cfg, result, job_dir=job_dir)
    assert failed_followup.status == "completed"
    assert failed_followup.analysis_summary["ts_hessian_provenance"]["status"] == "failed"

    def raising_sp_job(*args: Any, **kwargs: Any) -> runner_mod.XtbRunResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(runner_mod, "_run_candidate_sp_job", raising_sp_job)
    crashed_followup = runner_mod.run_path_search_ts_hessian_followup(cfg, result, job_dir=job_dir)
    assert crashed_followup.status == "completed"
    assert crashed_followup.analysis_summary["ts_hessian_provenance"]["reason"] == (
        "hessian_followup_exception"
    )


def test_collect_hessian_candidates(tmp_path: Path) -> None:
    job_dir = tmp_path / "hess-job"
    job_dir.mkdir()
    count, paths, details, summary = runner_artifacts._collect_hessian_candidates(job_dir)
    assert count == 0 and paths == () and details == ()
    assert summary["canonical_result_path"] == ""

    (job_dir / "hessian").write_text(
        "$hessian\n" + " ".join(["0.1"] * 9) + "\n",
        encoding="utf-8",
    )
    count, paths, details, summary = runner_artifacts._collect_hessian_candidates(job_dir)
    assert count == 1
    assert details[0]["kind"] == "hessian"
    assert summary["canonical_result_path"] == str((job_dir / "hessian").resolve())


def test_ts_hessian_followup_skips_geometry_invalid_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "path-search-job"
    ts_guess_xyz = _write_xyz(job_dir / "xtbpath_ts.xyz")
    (job_dir / "xtb_job.yaml").write_text(
        "job_type: path_search\ngfn: 2\ncharge: 0\n", encoding="utf-8"
    )
    result = _make_path_search_result(job_dir, ts_guess_xyz)
    invalid_detail = {**result.candidate_details[0], "geometry_valid": False}
    result = dataclasses.replace(result, candidate_details=(invalid_detail,))

    monkeypatch.setattr(
        runner_mod,
        "_run_candidate_sp_job",
        lambda *args, **kwargs: pytest.fail("hessian job should not run for invalid guess"),
    )
    skipped = runner_mod.run_path_search_ts_hessian_followup(cfg, result, job_dir=job_dir)
    assert skipped.analysis_summary["ts_hessian_provenance"] == {
        "status": "skipped",
        "reason": "ts_guess_geometry_invalid",
    }
