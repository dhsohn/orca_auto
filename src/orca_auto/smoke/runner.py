from __future__ import annotations

import os
import select
import signal
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from orca_auto.core.admission import (
    complete_slot_engine_process,
    release_slot,
    reserve_slot,
    update_slot_metadata,
)
from orca_auto.core.paths import SMOKE_RESULTS_DIRNAME
from orca_auto.core.utils.persistence import now_utc_iso

from ._dirfd import (
    DirectoryIdentity,
    assert_named_directory_identity,
    directory_open_flags,
    read_pinned_regular_file,
    validate_owned_directory,
)
from .catalog import SmokeScenario, scenarios_for_profile
from .manifest import (
    PinnedBatchDirectory,
    build_case_manifest,
    create_pinned_batch_directory,
    create_pinned_case_directory,
    prepare_smoke_root,
    rebuild_smoke_index,
    source_identity,
    write_batch_manifest,
    write_case_manifest,
)
from .review import ReviewPacketError, ReviewPacketResult, generate_review_packet

_MAX_JUNIT_BYTES = 16 * 1024 * 1024
_SUPERVISOR_EXIT_TIMEOUT_SECONDS = 15.0
_PROCESS_SUPERVISOR_PATH = Path(__file__).with_name("_process_supervisor.py").resolve(strict=True)
_PYTEST_PINNED_TMP_PLUGIN = "orca_auto.smoke._pytest_pinned_tmp"


class _SmokeProcessCleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class RealEngineAdmission:
    root: Path
    global_limit: int
    xtb_md_limit: int | None = None
    scratch_root: Path | None = None
    scratch_min_free_gb: int | None = None


@dataclass(frozen=True)
class SmokeSuiteResult:
    batch_dir: Path
    batch_manifest_path: Path
    review: ReviewPacketResult | None
    exit_code: int


@dataclass(frozen=True)
class _SupervisedScenarioProcess:
    process: subprocess.Popen[bytes]
    cleanup_proof_fd: int


def _safe_count(value: object) -> int:
    try:
        return max(0, int(str(value or "0")))
    except (TypeError, ValueError):
        return 0


def _source_identity_available(identity: Mapping[str, Any]) -> bool:
    required_text_fields = (
        "git_head",
        "git_short",
        "working_tree_digest",
        "status_digest",
    )
    unavailable_values = {"", "unknown", "unavailable"}
    for field in required_text_fields:
        value = identity.get(field)
        if not isinstance(value, str) or value.strip().lower() in unavailable_values:
            return False
    return True


def _read_stable_regular_file_descriptor(descriptor: int, *, max_bytes: int) -> bytes:
    readable_fd = os.dup(descriptor)
    try:
        return read_pinned_regular_file(readable_fd, max_bytes=max_bytes)
    finally:
        os.close(readable_fd)


def _pytest_result(
    junit_fd: int,
    *,
    return_code: int | None,
    timed_out: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "return_code": return_code,
        "timed_out": timed_out,
        "tests": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "passed": False,
    }
    try:
        payload = _read_stable_regular_file_descriptor(junit_fd, max_bytes=_MAX_JUNIT_BYTES)
        root = ET.fromstring(payload)
    except (ET.ParseError, OSError, ValueError):
        return result

    if root.tag in {"testsuite", "testsuites"}:
        result["tests"] = _safe_count(root.attrib.get("tests"))
        result["failures"] = _safe_count(root.attrib.get("failures"))
        result["errors"] = _safe_count(root.attrib.get("errors"))
        result["skipped"] = _safe_count(root.attrib.get("skipped"))
    if result["tests"] == 0 and root.tag == "testsuites":
        suites = tuple(root.findall("./testsuite"))
        for key in ("tests", "failures", "errors", "skipped"):
            result[key] = sum(_safe_count(suite.attrib.get(key)) for suite in suites)

    result["passed"] = bool(
        return_code == 0
        and not timed_out
        and result["tests"] > 0
        and result["failures"] == 0
        and result["errors"] == 0
        and result["skipped"] == 0
    )
    return result


def _terminate_supervised_process(process: subprocess.Popen[bytes]) -> bool:
    try:
        if process.poll() is not None:
            return True
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=_SUPERVISOR_EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # The supervisor deliberately remains the admission owner when it
        # cannot prove every detached descendant has exited. Never SIGKILL it
        # here, because doing so would let stale-slot reconciliation free real
        # engine capacity while an untracked engine still runs.
        return False
    except BaseException:  # noqa: BLE001 - callers must retain the production lease
        return False
    return True


def _cleanup_failure_message(*, lease_retained: bool) -> str:
    detail = "smoke process-tree cleanup could not be confirmed"
    if lease_retained:
        return f"{detail}; production admission capacity remains retained"
    return f"{detail}; the smoke supervisor remains responsible for cleanup"


def _consume_cleanup_proof(proof_fd: int) -> bool:
    try:
        readable, _writable, _exceptional = select.select([proof_fd], [], [], 1.0)
        if not readable:
            return False
        return os.read(proof_fd, 2) == b"1"
    except OSError:
        return False
    finally:
        try:
            os.close(proof_fd)
        except OSError:
            pass


def _discard_cleanup_proof(proof_fd: int) -> None:
    try:
        os.close(proof_fd)
    except OSError:
        pass


def _reserve_real_engine_slot(
    scenario: SmokeScenario,
    case_dir: Path,
    admission: RealEngineAdmission | None,
) -> tuple[Path, str] | None:
    if scenario.profile == "fake":
        return None
    if scenario.profile not in {"real-orca", "real-xtb"}:
        raise ValueError(f"unsupported real-engine smoke profile: {scenario.profile}")
    if admission is None:
        raise ValueError(
            f"{scenario.profile} smoke requires the production admission configuration"
        )
    slot_kwargs: dict[str, Any] = {
        "source": f"orca_auto.smoke.{scenario.profile.replace('-', '_')}",
        "task_id": scenario.scenario_id,
        "work_dir": case_dir,
        "owner_pid": os.getpid(),
        "engine_process_state": "idle",
    }
    if scenario.profile == "real-xtb":
        if admission.xtb_md_limit is None:
            raise ValueError("real-xtb smoke requires the production xTB-MD admission limit")
        slot_kwargs.update(
            {
                "app_name": "orca_auto_xtb_md",
                "app_limit": admission.xtb_md_limit,
            }
        )
    else:
        slot_kwargs["app_name"] = "orca_auto_orca"
    token = reserve_slot(
        admission.root,
        admission.global_limit,
        **slot_kwargs,
    )
    if token is None:
        raise RuntimeError(
            f"production admission capacity is unavailable for {scenario.profile} smoke"
        )
    return admission.root, token


def _start_scenario_process(
    command: Sequence[str],
    *,
    repo_root: Path,
    environment: dict[str, str],
    stdout_handle: BinaryIO,
    stderr_handle: BinaryIO,
    lease: tuple[Path, str] | None,
) -> _SupervisedScenarioProcess:
    if not command:
        raise ValueError("smoke scenario command must not be empty")

    gate_read_fd, gate_write_fd = os.pipe()
    proof_read_fd, proof_write_fd = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    proof_read_transferred = False
    launch_gate_released = False
    try:
        process = subprocess.Popen(
            [
                command[0],
                str(_PROCESS_SUPERVISOR_PATH),
                str(gate_read_fd),
                str(proof_write_fd),
                *command,
            ],
            cwd=repo_root,
            env=environment,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            pass_fds=(gate_read_fd, proof_write_fd),
        )
        # The proof pipe is supervisor-to-runner only. Closing the runner's
        # writer immediately guarantees EOF if the supervisor exits before it
        # can publish proof, including the pre-handler startup race.
        os.close(proof_write_fd)
        proof_write_fd = -1
        if lease is not None:
            lease_root, lease_token = lease
            updated = update_slot_metadata(
                lease_root,
                lease_token,
                owner_pid=process.pid,
                engine_process_state="pending",
            )
            if (
                updated is None
                or updated.owner_pid != process.pid
                or updated.engine_process_state != "pending"
            ):
                raise RuntimeError(
                    "could not bind production admission capacity to the real-engine "
                    "smoke supervisor"
                )
        if process.poll() is not None:
            raise RuntimeError("smoke supervisor exited before the launch gate was released")
        # Once the write begins the child may observe the byte even if Python
        # is interrupted before the call returns, so require cleanup proof
        # from this point onward.
        launch_gate_released = True
        if os.write(gate_write_fd, b"1") != 1:
            raise OSError("could not release the smoke supervisor launch gate")
        proof_read_transferred = True
        return _SupervisedScenarioProcess(
            process=process,
            cleanup_proof_fd=proof_read_fd,
        )
    except BaseException as exc:  # noqa: BLE001 - never leave an unbound real-engine child alive
        if process is not None:
            terminated = _terminate_supervised_process(process)
            if terminated:
                if launch_gate_released:
                    cleanup_proven = _consume_cleanup_proof(proof_read_fd)
                else:
                    _discard_cleanup_proof(proof_read_fd)
                    cleanup_proven = True
            else:
                os.close(proof_read_fd)
                cleanup_proven = False
            proof_read_transferred = True
            if not cleanup_proven:
                raise _SmokeProcessCleanupError(
                    _cleanup_failure_message(lease_retained=lease is not None)
                ) from exc
        raise
    finally:
        for gate_fd in (gate_read_fd, gate_write_fd, proof_write_fd):
            if gate_fd < 0:
                continue
            try:
                os.close(gate_fd)
            except OSError:
                pass
        if not proof_read_transferred:
            try:
                os.close(proof_read_fd)
            except OSError:
                pass


def _open_direct_cases_root(batch_dir: Path) -> tuple[int, int, DirectoryIdentity]:
    batch_fd = os.open(batch_dir, directory_open_flags())
    cases_fd: int | None = None
    try:
        try:
            os.mkdir("cases", mode=0o700, dir_fd=batch_fd)
            os.fsync(batch_fd)
        except FileExistsError:
            pass
        cases_fd = os.open("cases", directory_open_flags(), dir_fd=batch_fd)
        cases_identity = validate_owned_directory(cases_fd, label="Smoke cases directory")
        assert_named_directory_identity(
            batch_fd,
            "cases",
            cases_identity,
            label="Smoke cases directory",
        )
        return batch_fd, cases_fd, cases_identity
    except BaseException:
        if cases_fd is not None:
            os.close(cases_fd)
        os.close(batch_fd)
        raise


def _create_pinned_harness_file(
    harness_fd: int,
    name: str,
) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=harness_fd)
    try:
        details = os.fstat(descriptor)
        named = os.stat(name, dir_fd=harness_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or named.st_nlink != 1
            or (details.st_dev, details.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ValueError("smoke harness output file is unsafe")
        return descriptor, (details.st_dev, details.st_ino)
    except BaseException:
        os.close(descriptor)
        raise


def _assert_pinned_harness_file(
    harness_fd: int,
    name: str,
    descriptor: int,
    expected_identity: tuple[int, int],
) -> None:
    details = os.fstat(descriptor)
    named = os.stat(name, dir_fd=harness_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or (details.st_dev, details.st_ino) != expected_identity
        or (named.st_dev, named.st_ino) != expected_identity
    ):
        raise ValueError(f"Smoke harness file identity changed: {name}")


def _append_harness_error(stderr_fd: int, message: str) -> None:
    os.lseek(stderr_fd, 0, os.SEEK_END)
    encoded = f"smoke harness setup failed: {message}\n".encode("utf-8", errors="replace")
    view = memoryview(encoded)
    while view:
        written = os.write(stderr_fd, view)
        if written < 1:
            raise OSError("could not append the smoke harness error log")
        view = view[written:]
    os.fsync(stderr_fd)


def _unlink_pytest_current_aliases(pytest_fd: int) -> None:
    """Remove pytest's transient latest-temp convenience aliases.

    The aliases point into the runner's procfd namespace and die with it; the
    numbered directories they reference remain in the retained runtime tree,
    so the review packet lists real artifacts only.
    """

    proc_parent = PurePosixPath("/proc", str(os.getpid()), "fd", str(pytest_fd))
    with os.scandir(pytest_fd) as iterator:
        names = [entry.name for entry in iterator]
    for name in names:
        if not name.endswith("current"):
            continue
        opened = os.stat(name, dir_fd=pytest_fd, follow_symlinks=False)
        if not stat.S_ISLNK(opened.st_mode):
            continue
        target_text = os.readlink(name, dir_fd=pytest_fd)
        target = PurePosixPath(target_text)
        if target.parent != proc_parent or target.name in {"", ".", ".."}:
            continue
        os.unlink(name, dir_fd=pytest_fd)
    os.fsync(pytest_fd)


def _assert_pinned_harness_files(
    harness_fd: int,
    entries: tuple[tuple[str, int, tuple[int, int]], ...],
    *,
    fsync_files: bool = False,
) -> None:
    for name, descriptor, identity in entries:
        if fsync_files:
            os.fsync(descriptor)
        _assert_pinned_harness_file(harness_fd, name, descriptor, identity)


def _settle_cleanup_proof(
    supervised: _SupervisedScenarioProcess,
    process: subprocess.Popen[bytes],
    *,
    lease_retained: bool,
    cause: BaseException,
) -> None:
    """Require durable cleanup proof after the child stopped (or was stopped)."""
    terminated = process.poll() is not None or _terminate_supervised_process(process)
    if terminated:
        cleanup_proven = _consume_cleanup_proof(supervised.cleanup_proof_fd)
    else:
        _discard_cleanup_proof(supervised.cleanup_proof_fd)
        cleanup_proven = False
    if not cleanup_proven:
        raise _SmokeProcessCleanupError(
            _cleanup_failure_message(lease_retained=lease_retained)
        ) from cause


def _run_scenario(
    *,
    repo_root: Path,
    batch_dir: Path,
    scenario: SmokeScenario,
    python_executable: str,
    real_engine_admission: RealEngineAdmission | None,
    pinned_batch: PinnedBatchDirectory | None = None,
) -> dict[str, Any]:
    case_dir = batch_dir / "cases" / scenario.scenario_id
    runtime_dir = case_dir / "runtime"
    owned_parent_fds: tuple[int, int] | None = None
    if pinned_batch is None:
        batch_fd, cases_fd, cases_identity = _open_direct_cases_root(batch_dir)
        owned_parent_fds = (cases_fd, batch_fd)
    else:
        pinned_batch.assert_namespace_identity()
        batch_fd = pinned_batch.batch_fd
        cases_fd = pinned_batch.cases_fd
        cases_identity = pinned_batch.cases_identity
    try:
        pinned_case = create_pinned_case_directory(
            batch_fd=batch_fd,
            cases_fd=cases_fd,
            cases_identity=cases_identity,
            case_id=scenario.scenario_id,
        )
    except BaseException:
        if owned_parent_fds is not None:
            for descriptor in owned_parent_fds:
                os.close(descriptor)
        raise
    output_fds: list[int] = []
    try:
        stdout_fd, stdout_identity = _create_pinned_harness_file(
            pinned_case.harness_fd,
            "harness.stdout.log",
        )
        output_fds.append(stdout_fd)
        stderr_fd, stderr_identity = _create_pinned_harness_file(
            pinned_case.harness_fd,
            "harness.stderr.log",
        )
        output_fds.append(stderr_fd)
        junit_fd, junit_identity = _create_pinned_harness_file(
            pinned_case.harness_fd,
            "pytest.xml",
        )
        output_fds.append(junit_fd)
        os.fsync(pinned_case.harness_fd)
    except BaseException:
        for descriptor in output_fds:
            os.close(descriptor)
        pinned_case.close()
        if owned_parent_fds is not None:
            for descriptor in owned_parent_fds:
                os.close(descriptor)
        raise

    started_at = now_utc_iso()
    timed_out = False
    return_code: int | None = None
    setup_errors: list[str] = []
    lease: tuple[Path, str] | None = None
    lease_release_safe = True
    harness_files = (
        ("harness.stdout.log", stdout_fd, stdout_identity),
        ("harness.stderr.log", stderr_fd, stderr_identity),
        ("pytest.xml", junit_fd, junit_identity),
    )

    junit_access_path = Path("/proc") / str(os.getpid()) / "fd" / str(junit_fd)
    command = [
        python_executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        _PYTEST_PINNED_TMP_PLUGIN,
        f"--junitxml={junit_access_path}",
        scenario.pytest_selector,
    ]
    environment = os.environ.copy()
    source_path = str(repo_root / "src")
    harness_source_path = str(Path(__file__).resolve().parents[2])
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (source_path, str(repo_root), harness_source_path, existing_pythonpath)
        if part
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    for variable in (
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTEST_DEBUG_TEMPROOT",
        "XTB_MD_REAL_SCRATCH_ROOT",
        "XTB_MD_REAL_SCRATCH_MIN_FREE_GB",
    ):
        environment.pop(variable, None)
    environment["ORCA_AUTO_SMOKE_PYTEST_TMP_PATH"] = str(pinned_case.pytest_access_path)
    environment["ORCA_AUTO_SMOKE_PYTEST_TMP_DEVICE"] = str(pinned_case.pytest_identity.device)
    environment["ORCA_AUTO_SMOKE_PYTEST_TMP_INODE"] = str(pinned_case.pytest_identity.inode)
    if (
        scenario.profile == "real-xtb"
        and real_engine_admission is not None
        and real_engine_admission.scratch_root is not None
    ):
        environment["XTB_MD_REAL_SCRATCH_ROOT"] = str(real_engine_admission.scratch_root)
        environment["XTB_MD_REAL_SCRATCH_MIN_FREE_GB"] = str(
            real_engine_admission.scratch_min_free_gb or 8
        )

    try:
        pinned_case.assert_namespace_identity()
        _assert_pinned_harness_files(pinned_case.harness_fd, harness_files)
        lease = _reserve_real_engine_slot(scenario, case_dir, real_engine_admission)
        with (
            os.fdopen(os.dup(stdout_fd), "wb", closefd=True) as stdout_handle,
            os.fdopen(os.dup(stderr_fd), "wb", closefd=True) as stderr_handle,
        ):
            supervised = _start_scenario_process(
                command,
                repo_root=repo_root,
                environment=environment,
                stdout_handle=stdout_handle,
                stderr_handle=stderr_handle,
                lease=lease,
            )
            process = supervised.process
            try:
                return_code = process.wait(timeout=scenario.timeout_seconds)
                if not _consume_cleanup_proof(supervised.cleanup_proof_fd):
                    raise _SmokeProcessCleanupError(
                        _cleanup_failure_message(lease_retained=lease is not None)
                    )
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                _settle_cleanup_proof(
                    supervised,
                    process,
                    lease_retained=lease is not None,
                    cause=exc,
                )
                return_code = process.returncode
            except BaseException as exc:  # noqa: BLE001 - release only after child exit
                if isinstance(exc, _SmokeProcessCleanupError):
                    raise
                _settle_cleanup_proof(
                    supervised,
                    process,
                    lease_retained=lease is not None,
                    cause=exc,
                )
                raise
        pinned_case.assert_namespace_identity()
        _assert_pinned_harness_files(pinned_case.harness_fd, harness_files)
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, _SmokeProcessCleanupError):
            lease_release_safe = False
        setup_error = f"{type(exc).__name__}: {exc}"
        setup_errors.append(setup_error)
        _append_harness_error(stderr_fd, setup_error)
    finally:
        if lease is not None and lease_release_safe:
            lease_root, lease_token = lease
            try:
                releasable = complete_slot_engine_process(lease_root, lease_token)
                if releasable is None or releasable.engine_process_state != "idle":
                    raise RuntimeError(
                        "could not mark the real-engine smoke admission lease releasable"
                    )
                release_slot(lease_root, lease_token)
            except (OSError, RuntimeError, ValueError) as exc:
                setup_errors.append(f"{type(exc).__name__}: {exc}")
        try:
            pinned_case.assert_namespace_identity()
            _unlink_pytest_current_aliases(pinned_case.pytest_fd)
            _assert_pinned_harness_files(
                pinned_case.harness_fd,
                harness_files,
                fsync_files=True,
            )
            os.fsync(pinned_case.harness_fd)
        except (OSError, RuntimeError, ValueError) as exc:
            finalization_error = f"{type(exc).__name__}: {exc}"
            setup_errors.append(finalization_error)
            try:
                _append_harness_error(stderr_fd, finalization_error)
            except OSError:
                pass

    try:
        pytest_result = _pytest_result(
            junit_fd,
            return_code=return_code,
            timed_out=timed_out,
        )
        pytest_result["selector"] = scenario.pytest_selector
        if setup_errors:
            pytest_result["setup_error"] = "; ".join(setup_errors)
            pytest_result["passed"] = False
        pinned_case.assert_namespace_identity()
        manifest = build_case_manifest(
            scenario=scenario,
            batch_dir=batch_dir,
            case_dir=case_dir,
            runtime_dir=runtime_dir,
            runtime_fd=pinned_case.runtime_fd,
            pytest_result=pytest_result,
            started_at=started_at,
            finished_at=now_utc_iso(),
        )
        pinned_case.assert_namespace_identity()
        write_case_manifest(case_dir, manifest, directory_fd=pinned_case.case_fd)
        pinned_case.assert_namespace_identity()
        return manifest
    finally:
        for descriptor in output_fds:
            os.close(descriptor)
        pinned_case.close()
        if owned_parent_fds is not None:
            for descriptor in owned_parent_fds:
                os.close(descriptor)


def _selected_scenarios(
    profile: str,
    selected_ids: Sequence[str] | None,
) -> tuple[SmokeScenario, ...]:
    available = scenarios_for_profile(profile)
    if not selected_ids:
        return available
    requested = tuple(dict.fromkeys(item.strip() for item in selected_ids if item.strip()))
    by_id = {scenario.scenario_id: scenario for scenario in available}
    unknown = sorted(set(requested) - set(by_id))
    if unknown:
        raise ValueError(f"unknown smoke scenario(s) for {profile}: {', '.join(unknown)}")
    return tuple(by_id[scenario_id] for scenario_id in requested)


def run_smoke_suite(
    *,
    repo_root: Path,
    runs_root: Path,
    profile: str = "fake",
    selected_ids: Sequence[str] | None = None,
    python_executable: str = sys.executable,
    real_engine_admission: RealEngineAdmission | None = None,
) -> SmokeSuiteResult:
    resolved_repo = repo_root.expanduser().resolve()
    if not (resolved_repo / "pyproject.toml").is_file():
        raise ValueError(f"repository root does not contain pyproject.toml: {resolved_repo}")
    resolved_runs_root = runs_root.expanduser().resolve()
    smoke_root_candidate = (resolved_runs_root / SMOKE_RESULTS_DIRNAME).resolve()
    if resolved_runs_root.is_relative_to(resolved_repo) or smoke_root_candidate.is_relative_to(
        resolved_repo
    ):
        raise ValueError("smoke runs root must be outside the repository worktree")
    scenarios = _selected_scenarios(profile, selected_ids)
    if not scenarios:
        raise ValueError("at least one smoke scenario must be selected")
    if any(scenario.profile in {"real-orca", "real-xtb"} for scenario in scenarios):
        if real_engine_admission is None:
            raise ValueError("real-engine smoke requires the production admission configuration")

    smoke_root = prepare_smoke_root(resolved_runs_root)
    pinned_batch = create_pinned_batch_directory(
        smoke_root,
        profile=profile,
        repo_root=resolved_repo,
    )
    batch_dir = pinned_batch.batch_access_path
    advertised_batch_dir = pinned_batch.batch_dir
    batch_manifest = pinned_batch.manifest
    case_manifests: list[dict[str, Any]] = []
    review: ReviewPacketResult | None = None
    harness_error: Exception | None = None
    try:
        try:
            for scenario in scenarios:
                pinned_batch.assert_namespace_identity()
                case_manifests.append(
                    _run_scenario(
                        repo_root=resolved_repo,
                        batch_dir=advertised_batch_dir,
                        scenario=scenario,
                        python_executable=python_executable,
                        real_engine_admission=real_engine_admission,
                        pinned_batch=pinned_batch,
                    )
                )
                pinned_batch.assert_namespace_identity()
        except Exception as exc:  # noqa: BLE001 - durable harness terminalization boundary
            harness_error = exc
        passed_count = sum(case.get("verdict") == "passed" for case in case_manifests)
        all_cases_ran = len(case_manifests) == len(scenarios)
        batch_errors: list[str] = []
        if harness_error is not None:
            batch_errors.append(f"smoke_harness_error:{type(harness_error).__name__}")
        source_at_start = dict(batch_manifest["source"])
        source_identity_comparable = True
        try:
            source_at_end = source_identity(resolved_repo)
        except Exception as exc:  # noqa: BLE001 - terminalize an unverifiable source identity
            source_identity_comparable = False
            source_at_end = {
                "available": False,
                "error_type": type(exc).__name__,
            }
            batch_errors.append(f"source_identity_error:{type(exc).__name__}")
        source_at_start_available = _source_identity_available(source_at_start)
        source_at_end_available = _source_identity_available(source_at_end)
        if not source_at_start_available or not source_at_end_available:
            batch_errors.append("source_identity_incomplete")
        if (
            source_identity_comparable
            and source_at_start_available
            and source_at_end_available
            and source_at_end != source_at_start
        ):
            batch_errors.append("source_changed_during_run")
        candidate_status = (
            "passed"
            if all_cases_ran and passed_count == len(case_manifests) and not batch_errors
            else "failed"
        )
        batch_manifest.update(
            {
                # A terminal pass is published only after the review packet and
                # its metadata are complete. If that final write fails, the
                # durable manifest remains visibly nonterminal.
                "status": "finalizing",
                "finished_at": now_utc_iso(),
                "source_at_start": source_at_start,
                "source_at_end": source_at_end,
                "case_count": len(case_manifests),
                "expected_case_count": len(scenarios),
                "passed_count": passed_count,
                "failed_count": len(case_manifests) - passed_count,
                "batch_errors": batch_errors,
                "cases": case_manifests,
            }
        )
        pinned_batch.assert_namespace_identity()
        write_batch_manifest(
            batch_dir,
            batch_manifest,
            directory_fd=pinned_batch.batch_fd,
        )
        review_manifest = dict(batch_manifest)
        review_manifest["status"] = candidate_status
        try:
            review = generate_review_packet(
                advertised_batch_dir,
                review_manifest,
                case_manifests,
                batch_root_fd=pinned_batch.batch_fd,
                advertised_batch_dir=advertised_batch_dir,
                expected_batch_identity=(
                    pinned_batch.batch_identity.device,
                    pinned_batch.batch_identity.inode,
                ),
            )
        except (OSError, ReviewPacketError) as exc:
            batch_manifest["status"] = "failed"
            batch_manifest["batch_errors"].append(f"review_packet_error:{type(exc).__name__}")
        if review is not None:
            batch_manifest["status"] = candidate_status
            batch_manifest["review_packet"] = {
                "summary": str(review.summary_path.relative_to(advertised_batch_dir)),
                "index": str(review.index_path.relative_to(advertised_batch_dir)),
                "artifacts": str(review.artifact_manifest_path.relative_to(advertised_batch_dir)),
                "artifact_count": review.artifact_count,
                "openable_count": review.openable_count,
            }
        pinned_batch.assert_namespace_identity()
        write_batch_manifest(
            batch_dir,
            batch_manifest,
            directory_fd=pinned_batch.batch_fd,
        )
    finally:
        try:
            rebuild_smoke_index(
                smoke_root,
                smoke_root_fd=pinned_batch.smoke_root_fd,
                batches_fd=pinned_batch.batches_fd,
            )
            pinned_batch.assert_namespace_identity()
        finally:
            pinned_batch.close()

    if harness_error is not None:
        raise harness_error

    exit_code = 0 if batch_manifest.get("status") == "passed" else 1
    return SmokeSuiteResult(
        batch_dir=advertised_batch_dir,
        batch_manifest_path=advertised_batch_dir / "batch.json",
        review=review,
        exit_code=exit_code,
    )


__all__ = [
    "RealEngineAdmission",
    "SmokeSuiteResult",
    "run_smoke_suite",
]
