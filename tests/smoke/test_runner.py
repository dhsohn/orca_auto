from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.admission import (
    list_all_slots,
    list_slots,
    reconcile_stale_slots,
    release_slot,
    reserve_slot,
    update_slot_metadata,
)
from orca_auto.core.admission import store as admission_store
from orca_auto.smoke import manifest as smoke_manifest
from orca_auto.smoke import runner
from orca_auto.smoke.catalog import SmokeScenario


def _write_tiny_repo(repo_root: Path, *, status: str, pytest_body: str = "") -> None:
    (repo_root / "tests").mkdir(parents=True)
    (repo_root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    source = f"""\
import json
import pytest


def test_retained_case(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "job_state.json").write_text(
        json.dumps({{"status": {status!r}}}), encoding="utf-8"
    )
    (job / "job_report.json").write_text("{{}}", encoding="utf-8")
    (job / "job_report.md").write_text("# report\\n", encoding="utf-8")
{pytest_body}
"""
    (repo_root / "tests" / "test_case.py").write_text(source, encoding="utf-8")
    for arguments in (
        ("init", "-q"),
        ("config", "user.name", "Smoke Test"),
        ("config", "user.email", "smoke@example.invalid"),
        ("add", "pyproject.toml", "tests/test_case.py"),
        ("-c", "commit.gpgsign=false", "commit", "-qm", "initial"),
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )


def _scenario(
    *,
    expected_status: str,
    profile: str = "fake",
    timeout_seconds: float = 30,
) -> SmokeScenario:
    return SmokeScenario(
        scenario_id="tiny_case",
        surface="orca-standalone",
        description="tiny retained runner case",
        pytest_selector="tests/test_case.py::test_retained_case",
        expected_status=expected_status,
        required_artifacts=("job_state.json", "job_report.json", "job_report.md"),
        profile=profile,
        timeout_seconds=timeout_seconds,
    )


def _force_timeout_after_descendant_ready(
    monkeypatch: pytest.MonkeyPatch,
    pid_path: Path,
) -> None:
    original_start = runner._start_scenario_process

    def start_with_ready_timeout(*args: Any, **kwargs: Any) -> Any:
        supervised = original_start(*args, **kwargs)
        original_wait = supervised.process.wait
        first_wait = True

        def wait_after_ready(timeout: float | None = None) -> int:
            nonlocal first_wait
            if not first_wait:
                return original_wait(timeout=timeout)
            first_wait = False
            timeout_value = 0.0 if timeout is None else timeout

            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    descendant_pid = int(pid_path.read_text(encoding="utf-8"))
                except (FileNotFoundError, ValueError):
                    descendant_pid = 0
                if descendant_pid > 0 and (Path("/proc") / str(descendant_pid)).exists():
                    raise subprocess.TimeoutExpired(supervised.process.args, timeout_value)
                if supervised.process.poll() is not None:
                    return original_wait(timeout=0)
                time.sleep(0.01)

            # Drive the normal runner cleanup path even when fixture readiness
            # itself fails, so a failed regression cannot leak the supervisor.
            raise subprocess.TimeoutExpired(supervised.process.args, timeout_value)

        monkeypatch.setattr(supervised.process, "wait", wait_after_ready)
        return supervised

    monkeypatch.setattr(runner, "_start_scenario_process", start_with_ready_timeout)


@pytest.mark.parametrize("expected_status", ["completed", "failed"])
def test_run_smoke_suite_keeps_expected_terminal_separate_from_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected_status: str,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    _write_tiny_repo(repo_root, status=expected_status)
    monkeypatch.setattr(
        runner,
        "scenarios_for_profile",
        lambda _profile: (_scenario(expected_status=expected_status),),
    )

    result = runner.run_smoke_suite(repo_root=repo_root, runs_root=runs_root)

    assert result.exit_code == 0
    manifest = json.loads(result.batch_manifest_path.read_text(encoding="utf-8"))
    case = manifest["cases"][0]
    assert case["expected_terminal"]["status"] == expected_status
    assert case["observed_terminal"]["status"] == expected_status
    assert case["verdict"] == "passed"
    assert case["case_dir"] == "cases/tiny_case"
    assert result.review is not None
    assert result.review.index_path.is_file()
    assert result.review.summary_path.is_file()
    assert result.review.artifact_manifest_path.is_file()
    assert result.review.openable_count > 0
    assert result.review.hidden_harness_alias_count == 1
    assert manifest["review_packet"]["artifacts"].endswith("/artifacts.json")
    assert manifest["review_packet"]["openable_count"] == result.review.openable_count
    assert (
        manifest["review_packet"]["hidden_harness_alias_count"]
        == result.review.hidden_harness_alias_count
    )
    assert (runs_root / ".orca_auto_smoke" / "index.json").is_file()


def test_run_smoke_suite_fails_batch_but_still_builds_review_packet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    _write_tiny_repo(repo_root, status="completed", pytest_body="    assert False\n")
    monkeypatch.setattr(
        runner, "scenarios_for_profile", lambda _profile: (_scenario(expected_status="completed"),)
    )

    result = runner.run_smoke_suite(repo_root=repo_root, runs_root=runs_root)

    assert result.exit_code == 1
    manifest = json.loads(result.batch_manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["cases"][0]["verdict"] == "failed"
    assert "pytest_scenario_failed" in manifest["cases"][0]["failure_reasons"]
    assert result.review is not None and result.review.index_path.is_file()


def test_review_failure_and_final_manifest_failure_never_leave_passed_index_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    _write_tiny_repo(repo_root, status="completed")
    monkeypatch.setattr(
        runner,
        "scenarios_for_profile",
        lambda _profile: (_scenario(expected_status="completed"),),
    )
    monkeypatch.setattr(
        runner,
        "generate_review_packet",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(runner.ReviewPacketError("injected")),
    )
    original_write = runner.write_batch_manifest
    writes = 0

    def fail_final_write(*args: Any, **kwargs: Any) -> Path:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected final manifest failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(runner, "write_batch_manifest", fail_final_write)

    with pytest.raises(OSError, match="final manifest failure"):
        runner.run_smoke_suite(repo_root=repo_root, runs_root=runs_root)

    smoke_root = runs_root / ".orca_auto_smoke"
    batch_dir = next((smoke_root / "batches").iterdir())
    manifest = json.loads((batch_dir / "batch.json").read_text(encoding="utf-8"))
    index = json.loads((smoke_root / "index.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "finalizing"
    assert index["batches"][0]["status"] == "finalizing"


def test_batch_namespace_replacement_cannot_redirect_suite_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    _write_tiny_repo(repo_root, status="completed")
    monkeypatch.setattr(
        runner,
        "scenarios_for_profile",
        lambda _profile: (_scenario(expected_status="completed"),),
    )
    original_run = runner._run_scenario
    moved: dict[str, Path] = {}

    def replace_after_scenario(**kwargs: Any) -> dict[str, Any]:
        case_manifest = original_run(**kwargs)
        batches_root = runs_root / ".orca_auto_smoke" / "batches"
        advertised = next(batches_root.iterdir())
        held = advertised.with_name(f"{advertised.name}-held")
        advertised.rename(held)
        advertised.mkdir(mode=0o700)
        moved.update(advertised=advertised, held=held)
        return case_manifest

    monkeypatch.setattr(runner, "_run_scenario", replace_after_scenario)

    with pytest.raises(ValueError, match="Smoke batch directory identity changed"):
        runner.run_smoke_suite(repo_root=repo_root, runs_root=runs_root)

    assert list(moved["advertised"].iterdir()) == []
    assert (moved["held"] / "batch.json").is_file()


def test_case_parent_symlink_cannot_redirect_scenario_outputs(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    batch_dir = tmp_path / "batch"
    outside = tmp_path / "outside"
    repo_root.mkdir()
    batch_dir.mkdir()
    outside.mkdir()
    (batch_dir / "cases").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        runner._run_scenario(
            repo_root=repo_root,
            batch_dir=batch_dir,
            scenario=_scenario(expected_status="completed"),
            python_executable=sys.executable,
            real_engine_admission=None,
        )

    assert list(outside.iterdir()) == []


def test_runtime_namespace_replacement_cannot_redirect_scenario_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    batch_dir = tmp_path / "batch"
    outside = tmp_path / "outside"
    _write_tiny_repo(repo_root, status="completed")
    batch_dir.mkdir()
    outside.mkdir()
    scenario = _scenario(expected_status="completed")

    def replace_runtime_namespace(
        _scenario_value: SmokeScenario,
        case_dir: Path,
        _admission: runner.RealEngineAdmission | None,
    ) -> None:
        runtime_dir = case_dir / "runtime"
        runtime_dir.rename(case_dir / "runtime-held")
        runtime_dir.symlink_to(outside, target_is_directory=True)
        return None

    monkeypatch.setattr(runner, "_reserve_real_engine_slot", replace_runtime_namespace)

    with pytest.raises(ValueError, match="runtime directory identity changed"):
        runner._run_scenario(
            repo_root=repo_root,
            batch_dir=batch_dir,
            scenario=scenario,
            python_executable=sys.executable,
            real_engine_admission=None,
        )

    assert list(outside.iterdir()) == []


def test_run_smoke_suite_marks_skipped_scenario_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    _write_tiny_repo(
        repo_root,
        status="completed",
        pytest_body='    pytest.skip("engine is not configured")\n',
    )
    monkeypatch.setattr(
        runner,
        "scenarios_for_profile",
        lambda _profile: (_scenario(expected_status="completed"),),
    )

    result = runner.run_smoke_suite(repo_root=repo_root, runs_root=runs_root)

    assert result.exit_code == 1
    manifest = json.loads(result.batch_manifest_path.read_text(encoding="utf-8"))
    assert manifest["cases"][0]["verdict"] == "not_run"
    assert manifest["cases"][0]["failure_reasons"] == ["pytest_scenario_skipped"]


def test_run_smoke_suite_fails_when_source_changes_during_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    _write_tiny_repo(repo_root, status="completed")
    scenario = _scenario(expected_status="completed")
    monkeypatch.setattr(runner, "scenarios_for_profile", lambda _profile: (scenario,))
    source_at_start = {
        "git_head": "abc123",
        "git_short": "abc123",
        "dirty": True,
        "working_tree_digest": "before",
        "status_digest": "status",
        "untracked_digest": "unchanged",
        "untracked_file_count": 0,
        "untracked_digest_complete": True,
    }
    source_at_end = {
        **source_at_start,
        "working_tree_digest": "after",
    }
    monkeypatch.setattr(
        smoke_manifest,
        "source_identity",
        lambda _repo_root: dict(source_at_start),
    )
    monkeypatch.setattr(
        runner,
        "source_identity",
        lambda _repo_root: dict(source_at_end),
    )

    result = runner.run_smoke_suite(repo_root=repo_root, runs_root=runs_root)

    assert result.exit_code == 1
    manifest = json.loads(result.batch_manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["batch_errors"] == ["source_changed_during_run"]
    assert manifest["source"] == source_at_start
    assert manifest["source_at_start"] == source_at_start
    assert manifest["source_at_end"] == source_at_end
    assert manifest["cases"][0]["verdict"] == "passed"


@pytest.mark.parametrize(
    "identity_override",
    [
        pytest.param({"untracked_digest_complete": False}, id="incomplete"),
        pytest.param({"working_tree_digest": "unavailable"}, id="unavailable"),
    ],
)
def test_run_smoke_suite_retains_review_but_fails_for_untrusted_source_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_override: dict[str, object],
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    _write_tiny_repo(repo_root, status="completed")
    scenario = _scenario(expected_status="completed")
    monkeypatch.setattr(runner, "scenarios_for_profile", lambda _profile: (scenario,))
    untrusted_source: dict[str, object] = {
        "git_head": "abc123",
        "git_short": "abc123",
        "dirty": True,
        "working_tree_digest": "tracked",
        "status_digest": "status",
        "untracked_digest": "bounded",
        "untracked_file_count": 2,
        "untracked_digest_complete": True,
    }
    untrusted_source.update(identity_override)
    monkeypatch.setattr(
        smoke_manifest,
        "source_identity",
        lambda _repo_root: dict(untrusted_source),
    )
    monkeypatch.setattr(
        runner,
        "source_identity",
        lambda _repo_root: dict(untrusted_source),
    )

    result = runner.run_smoke_suite(repo_root=repo_root, runs_root=runs_root)

    assert result.exit_code == 1
    manifest = json.loads(result.batch_manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["batch_errors"] == ["source_identity_incomplete"]
    assert manifest["source_at_start"] == untrusted_source
    assert manifest["source_at_end"] == untrusted_source
    assert manifest["cases"][0]["verdict"] == "passed"
    assert result.review is not None
    assert result.review.index_path.is_file()
    assert result.review.summary_path.is_file()


@pytest.mark.parametrize("through_symlink", [False, True])
def test_run_smoke_suite_rejects_runs_root_inside_repository_before_writing(
    tmp_path: Path,
    through_symlink: bool,
) -> None:
    repo_root = tmp_path / "repo"
    inside_repo = repo_root / "retained-smoke"
    _write_tiny_repo(repo_root, status="completed")
    runs_root = inside_repo
    if through_symlink:
        runs_root = tmp_path / "runs-link"
        runs_root.symlink_to(inside_repo, target_is_directory=True)

    with pytest.raises(ValueError, match="outside the repository worktree"):
        runner.run_smoke_suite(repo_root=repo_root, runs_root=runs_root)

    assert inside_repo.exists() is False
    assert (repo_root / ".orca_auto_smoke").exists() is False


@pytest.mark.parametrize("real_profile", ["real-orca", "real-xtb"])
def test_real_engine_case_holds_and_releases_production_admission_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_profile: str,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    admission_root = tmp_path / "admission"
    _write_tiny_repo(repo_root, status="completed")
    real_scenario = _scenario(expected_status="completed", profile=real_profile)
    monkeypatch.setattr(runner, "scenarios_for_profile", lambda _profile: (real_scenario,))
    # The scenario cwd is the tiny repository, so this caller-relative entry
    # cannot locate orca_auto there. Supervisor startup must not depend on it.
    monkeypatch.setenv("PYTHONPATH", "src")

    result = runner.run_smoke_suite(
        repo_root=repo_root,
        runs_root=runs_root,
        profile=real_profile,
        real_engine_admission=runner.RealEngineAdmission(
            root=admission_root,
            global_limit=1,
            xtb_md_limit=1,
        ),
    )

    assert result.exit_code == 0
    assert list_slots(admission_root) == []


def test_real_orca_admission_uses_orca_app_without_xtb_subcap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def capture_reservation(root: Path, limit: int, **kwargs: Any) -> str:
        captured.update({"root": root, "limit": limit, **kwargs})
        return "slot-real-orca"

    monkeypatch.setattr(runner, "reserve_slot", capture_reservation)
    admission = runner.RealEngineAdmission(
        root=tmp_path / "admission",
        global_limit=3,
        xtb_md_limit=1,
    )
    scenario = _scenario(expected_status="completed", profile="real-orca")

    lease = runner._reserve_real_engine_slot(scenario, tmp_path / "case", admission)

    assert lease == (admission.root, "slot-real-orca")
    assert captured["root"] == admission.root
    assert captured["limit"] == 3
    assert captured["source"] == "orca_auto.smoke.real_orca"
    assert captured["app_name"] == "orca_auto_orca"
    assert "app_limit" not in captured


def test_real_orca_suite_requires_production_admission_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    _write_tiny_repo(repo_root, status="completed")
    scenario = _scenario(expected_status="completed", profile="real-orca")
    monkeypatch.setattr(runner, "scenarios_for_profile", lambda _profile: (scenario,))

    with pytest.raises(ValueError, match="production admission configuration"):
        runner.run_smoke_suite(
            repo_root=repo_root,
            runs_root=runs_root,
            profile="real-orca",
        )

    assert (runs_root / ".orca_auto_smoke").exists() is False


def test_real_engine_case_binds_slot_to_supervisor_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    admission_root = tmp_path / "admission"
    ready_path = tmp_path / "pytest-ready"
    release_path = tmp_path / "pytest-release"
    _write_tiny_repo(
        repo_root,
        status="completed",
        pytest_body=f"""    import os
    import time
    from pathlib import Path

    ready = Path({str(ready_path)!r})
    release = Path({str(release_path)!r})
    ready.write_text(str(os.getpid()), encoding="utf-8")
    deadline = time.monotonic() + 20
    while not release.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert release.is_file(), "test release gate timed out"
""",
    )
    real_scenario = _scenario(expected_status="completed", profile="real-xtb")
    monkeypatch.setattr(runner, "scenarios_for_profile", lambda _profile: (real_scenario,))

    admission = runner.RealEngineAdmission(
        root=admission_root,
        global_limit=1,
        xtb_md_limit=1,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runner.run_smoke_suite,
            repo_root=repo_root,
            runs_root=runs_root,
            profile="real-xtb",
            real_engine_admission=admission,
        )
        try:
            deadline = time.monotonic() + 15
            managed_pid: int | None = None
            slot = None
            while time.monotonic() < deadline:
                slots = list_all_slots(admission_root)
                if ready_path.is_file() and len(slots) == 1:
                    try:
                        managed_pid = int(ready_path.read_text(encoding="utf-8"))
                    except ValueError:
                        time.sleep(0.01)
                        continue
                    slot = slots[0]
                    break
                time.sleep(0.01)
            assert managed_pid is not None and slot is not None
            supervisor_pid = slot.owner_pid
            assert managed_pid != os.getpid()
            assert supervisor_pid not in {os.getpid(), managed_pid}
            assert os.getpgid(managed_pid) == supervisor_pid

            observed_owner_pids: list[int] = []

            def only_supervisor_is_alive(
                pid: int,
                _expected_ticks: int | None,
                _expected_boot_id: str | None = None,
            ) -> bool:
                observed_owner_pids.append(pid)
                return pid == supervisor_pid

            monkeypatch.setattr(
                admission_store,
                "_process_identity_alive",
                only_supervisor_is_alive,
            )
            assert reconcile_stale_slots(admission_root) == 0
            assert observed_owner_pids == [supervisor_pid]
            assert [retained.token for retained in list_all_slots(admission_root)] == [slot.token]
        finally:
            release_path.touch()

        result = future.result(timeout=30)

    assert result.exit_code == 0
    assert list_all_slots(admission_root) == []


def test_real_engine_timeout_reaps_detached_session_before_releasing_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    admission_root = tmp_path / "admission"
    engine_pid_path = tmp_path / "engine-pid"
    engine_term_path = tmp_path / "engine-term-seen"
    engine_code = f"""\
import os
import signal
import time
from pathlib import Path

pid_path = Path({str(engine_pid_path)!r})
term_path = Path({str(engine_term_path)!r})

def ignore_term(_signum, _frame):
    term_path.touch()

signal.signal(signal.SIGTERM, ignore_term)
pid_path.write_text(str(os.getpid()), encoding="utf-8")
while True:
    time.sleep(0.05)
"""
    _write_tiny_repo(
        repo_root,
        status="completed",
        pytest_body=f"""    import subprocess
    import sys
    import time

    subprocess.Popen(
        [sys.executable, "-c", {engine_code!r}],
        start_new_session=True,
    )
    while True:
        time.sleep(0.05)
""",
    )
    real_scenario = _scenario(
        expected_status="completed",
        profile="real-xtb",
        timeout_seconds=0.5,
    )
    monkeypatch.setattr(runner, "scenarios_for_profile", lambda _profile: (real_scenario,))
    _force_timeout_after_descendant_ready(monkeypatch, engine_pid_path)

    original_release_slot = runner.release_slot
    release_observations: list[tuple[bool, tuple[int, ...]]] = []

    def release_after_engine_exit(root: Path, token: str) -> bool:
        engine_pid = int(engine_pid_path.read_text(encoding="utf-8"))
        release_observations.append(
            (
                (Path("/proc") / str(engine_pid)).exists(),
                tuple(slot.owner_pid for slot in list_all_slots(root)),
            )
        )
        return original_release_slot(root, token)

    monkeypatch.setattr(runner, "release_slot", release_after_engine_exit)
    admission = runner.RealEngineAdmission(
        root=admission_root,
        global_limit=1,
        xtb_md_limit=1,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runner.run_smoke_suite,
            repo_root=repo_root,
            runs_root=runs_root,
            profile="real-xtb",
            real_engine_admission=admission,
        )

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if engine_pid_path.is_file() and engine_term_path.is_file():
                break
            time.sleep(0.01)
        assert engine_pid_path.is_file()
        assert engine_term_path.is_file()
        engine_pid = int(engine_pid_path.read_text(encoding="utf-8"))
        assert (Path("/proc") / str(engine_pid)).exists()

        slots_during_cleanup = list_all_slots(admission_root)
        assert len(slots_during_cleanup) == 1
        assert slots_during_cleanup[0].owner_pid != engine_pid
        assert reconcile_stale_slots(admission_root) == 0
        assert [slot.token for slot in list_all_slots(admission_root)] == [
            slots_during_cleanup[0].token
        ]

        result = future.result(timeout=30)

    assert result.exit_code == 1
    manifest = json.loads(result.batch_manifest_path.read_text(encoding="utf-8"))
    assert manifest["cases"][0]["pytest"]["timed_out"] is True
    assert release_observations == [(False, (slots_during_cleanup[0].owner_pid,))]
    assert not (Path("/proc") / str(engine_pid)).exists()
    assert list_all_slots(admission_root) == []


def test_fake_timeout_reaps_detached_session_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    child_pid_path = tmp_path / "child-pid"
    child_code = f"""\
import os
import time
from pathlib import Path

Path({str(child_pid_path)!r}).write_text(str(os.getpid()), encoding="utf-8")
while True:
    time.sleep(0.05)
"""
    _write_tiny_repo(
        repo_root,
        status="completed",
        pytest_body=f"""    import subprocess
    import sys
    import time

    subprocess.Popen(
        [sys.executable, "-c", {child_code!r}],
        start_new_session=True,
    )
    while True:
        time.sleep(0.05)
""",
    )
    scenario = _scenario(expected_status="completed", timeout_seconds=0.5)
    monkeypatch.setattr(runner, "scenarios_for_profile", lambda _profile: (scenario,))
    _force_timeout_after_descendant_ready(monkeypatch, child_pid_path)

    result = runner.run_smoke_suite(repo_root=repo_root, runs_root=runs_root)

    assert child_pid_path.is_file()
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    try:
        manifest = json.loads(result.batch_manifest_path.read_text(encoding="utf-8"))
        assert result.exit_code == 1
        assert manifest["cases"][0]["pytest"]["timed_out"] is True
        assert not (Path("/proc") / str(child_pid)).exists()
    finally:
        try:
            os.kill(child_pid, 9)
        except ProcessLookupError:
            pass


def test_real_engine_supervisor_crash_retains_pending_admission_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    admission_root = tmp_path / "admission"
    child_pid_path = tmp_path / "pytest-pid"
    _write_tiny_repo(
        repo_root,
        status="completed",
        pytest_body=f"""    import os
    import time
    from pathlib import Path

    Path({str(child_pid_path)!r}).write_text(str(os.getpid()), encoding="utf-8")
    while True:
        time.sleep(0.05)
""",
    )
    scenario = _scenario(
        expected_status="completed",
        profile="real-xtb",
        timeout_seconds=30,
    )
    monkeypatch.setattr(runner, "scenarios_for_profile", lambda _profile: (scenario,))
    admission = runner.RealEngineAdmission(
        root=admission_root,
        global_limit=1,
        xtb_md_limit=1,
    )

    retained_token = ""
    child_pid = 0
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runner.run_smoke_suite,
            repo_root=repo_root,
            runs_root=runs_root,
            profile="real-xtb",
            real_engine_admission=admission,
        )
        try:
            deadline = time.monotonic() + 15
            slot = None
            while time.monotonic() < deadline:
                slots = list_all_slots(admission_root)
                if child_pid_path.is_file() and len(slots) == 1:
                    slot = slots[0]
                    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                    break
                time.sleep(0.01)
            assert slot is not None
            assert slot.engine_process_state == "pending"
            retained_token = slot.token

            os.kill(slot.owner_pid, signal.SIGKILL)
            result = future.result(timeout=30)

            manifest = json.loads(result.batch_manifest_path.read_text(encoding="utf-8"))
            retained = list_all_slots(admission_root)
            assert result.exit_code == 1
            assert "cleanup could not be confirmed" in manifest["cases"][0]["pytest"]["setup_error"]
            assert [item.token for item in retained] == [retained_token]
            assert retained[0].engine_process_state == "pending"
            assert reconcile_stale_slots(admission_root) == 0
            assert reserve_slot(admission_root, 1, source="second-smoke") is None
            assert (Path("/proc") / str(child_pid)).exists()
        finally:
            if child_pid > 0:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if retained_token:
                update_slot_metadata(
                    admission_root,
                    retained_token,
                    engine_process_state="idle",
                )
                release_slot(admission_root, retained_token)


def test_real_engine_interrupt_retains_slot_when_cleanup_is_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    batch_dir = tmp_path / "batch"
    admission_root = tmp_path / "admission"
    batch_dir.mkdir()
    scenario = _scenario(expected_status="completed", profile="real-xtb")

    class InterruptedProcess:
        returncode = None

        def wait(self, timeout: float | None = None) -> int:
            raise KeyboardInterrupt

        def poll(self) -> int | None:
            return None

    def start_interrupted_process(
        *_args: Any,
        **kwargs: Any,
    ) -> runner._SupervisedScenarioProcess:
        stderr_handle = kwargs["stderr_handle"]
        stderr_handle.write(b"supervisor cleanup detail\n")
        stderr_handle.flush()
        proof_read_fd, proof_write_fd = os.pipe()
        os.close(proof_write_fd)
        return runner._SupervisedScenarioProcess(InterruptedProcess(), proof_read_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_start_scenario_process", start_interrupted_process)
    monkeypatch.setattr(runner, "_terminate_supervised_process", lambda *_args: False)
    released: list[str] = []
    monkeypatch.setattr(
        runner,
        "release_slot",
        lambda _root, token: released.append(token),
    )

    manifest = runner._run_scenario(
        repo_root=repo_root,
        batch_dir=batch_dir,
        scenario=scenario,
        python_executable=sys.executable,
        real_engine_admission=runner.RealEngineAdmission(
            root=admission_root,
            global_limit=1,
            xtb_md_limit=1,
        ),
    )

    slots = list_all_slots(admission_root)
    assert len(slots) == 1
    assert released == []
    assert "cleanup could not be confirmed" in manifest["pytest"]["setup_error"]
    stderr_log = (
        batch_dir
        / "cases"
        / scenario.scenario_id
        / "runtime"
        / "_smoke_harness"
        / "harness.stderr.log"
    ).read_text(encoding="utf-8")
    assert "supervisor cleanup detail" in stderr_log
    assert "smoke harness setup failed" in stderr_log
    assert release_slot(admission_root, slots[0].token) is True


def test_real_engine_case_terminates_child_when_admission_rebind_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    admission_root = tmp_path / "admission"
    executed_path = tmp_path / "pytest-executed"
    _write_tiny_repo(
        repo_root,
        status="completed",
        pytest_body=f"""    from pathlib import Path

    Path({str(executed_path)!r}).touch()
""",
    )
    real_scenario = _scenario(expected_status="completed", profile="real-xtb")
    monkeypatch.setattr(runner, "scenarios_for_profile", lambda _profile: (real_scenario,))
    monkeypatch.setattr(
        runner,
        "update_slot_metadata",
        lambda *_args, **_kwargs: None,
    )

    result = runner.run_smoke_suite(
        repo_root=repo_root,
        runs_root=runs_root,
        profile="real-xtb",
        real_engine_admission=runner.RealEngineAdmission(
            root=admission_root,
            global_limit=1,
            xtb_md_limit=1,
        ),
    )

    assert result.exit_code == 1
    manifest = json.loads(result.batch_manifest_path.read_text(encoding="utf-8"))
    setup_error = manifest["cases"][0]["pytest"]["setup_error"]
    assert "could not bind production admission capacity" in setup_error
    assert executed_path.exists() is False
    assert list_all_slots(admission_root) == []


def test_real_engine_case_fails_closed_when_production_admission_is_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runs_root = tmp_path / "runs"
    admission_root = tmp_path / "admission"
    _write_tiny_repo(repo_root, status="completed")
    real_scenario = _scenario(expected_status="completed", profile="real-xtb")
    monkeypatch.setattr(runner, "scenarios_for_profile", lambda _profile: (real_scenario,))
    occupied = reserve_slot(
        admission_root,
        1,
        source="existing-production-job",
        owner_pid=None,
    )
    assert occupied is not None

    result = runner.run_smoke_suite(
        repo_root=repo_root,
        runs_root=runs_root,
        profile="real-xtb",
        real_engine_admission=runner.RealEngineAdmission(
            root=admission_root,
            global_limit=1,
            xtb_md_limit=1,
        ),
    )

    assert result.exit_code == 1
    manifest = json.loads(result.batch_manifest_path.read_text(encoding="utf-8"))
    assert manifest["cases"][0]["pytest"]["tests"] == 0
    assert "admission capacity is unavailable" in manifest["cases"][0]["pytest"]["setup_error"]
    assert [slot.token for slot in list_slots(admission_root)] == [occupied]
    assert release_slot(admission_root, occupied) is True
