from __future__ import annotations

import fcntl
import json
import multiprocessing
import shlex
import subprocess
from argparse import Namespace
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orca_auto import cli_systemd_restart, cli_systemd_restart_guard
from orca_auto.core.admission import store

WORKER = "orca_auto-queue-worker@alice.service"
WORKFLOW = "orca_auto-workflow-worker@alice.service"


@dataclass(frozen=True)
class _Site:
    config: Path
    admission: Path


def _site(tmp_path: Path, name: str = "service", *, lock: bool = True, slots: bool = True) -> _Site:
    base = tmp_path / name
    base.mkdir(parents=True)
    (base / "runs").mkdir()
    admission = base / "admission"
    admission.mkdir()
    if lock:
        (admission / "admission.lock").touch(mode=0o600)
    if slots:
        (admission / "admission_slots.json").write_text("[]", encoding="utf-8")
    config = base / "orca_auto.yaml"
    config.write_text(
        json.dumps(
            {
                "runs_root": str(base / "runs"),
                "orca": {"paths": {"orca_executable": "/usr/bin/true"}},
                "scheduler": {"admission_root": str(admission), "max_active_simulations": 4},
            }
        ),
        encoding="utf-8",
    )
    return _Site(config, admission)


def _proc_stat(ticks: int = 12345) -> bytes:
    fields = ["S", *[str(index) for index in range(1, 19)], str(ticks)]
    return ("4242 (python worker) " + " ".join(fields)).encode()


class _Evidence:
    def __init__(self, sites: dict[str, _Site]) -> None:
        self.units: dict[str, dict[str, str]] = {}
        self.environ: dict[int, bytes] = {}
        self.commands: list[tuple[str, ...]] = []
        for index, (unit, site) in enumerate(sites.items(), start=4242):
            app = "workflow" if "workflow-worker" in unit else "orca"
            self.units[unit] = {
                "EnvironmentFiles": "",
                "UnsetEnvironment": "",
                "Environment": shlex.quote(f"ORCA_AUTO_CONFIG={site.config}"),
                "ExecStart": (
                    "{ path=/fixture/.venv/bin/python ; argv[]=/fixture/.venv/bin/python -m orca_auto.cli "
                    f"queue worker --app {app} ; ignore_errors=no ; }}"
                ),
                "MainPID": str(index),
                "ExecMainStartTimestamp": "Mon 2099-01-05 00:00:00 UTC",
            }
            self.environ[index] = f"ORCA_AUTO_CONFIG={site.config}\0".encode()

    def run(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(tuple(argv))
        assert argv[:2] == ["systemctl", "show"], "guard must never mutate services"
        property_name = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--property="))
        return subprocess.CompletedProcess(
            argv, 0, stdout=self.units[argv[-1]][property_name] + "\n", stderr=""
        )

    def read_process_file(self, path: str) -> bytes:
        pid = int(Path(path).parent.name)
        assert pid in self.environ
        if path.endswith("/environ"):
            return self.environ[pid]
        assert path.endswith("/stat")
        return _proc_stat()

    def guard(self) -> Any:
        return cli_systemd_restart_guard.guard_service_restart(
            tuple(self.units), run=self.run, read_process_file=self.read_process_file
        )


def _assert_locked(root: Path) -> None:
    with (root / "admission.lock").open("r+") as handle:
        with pytest.raises(BlockingIOError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _assert_unlocked(root: Path) -> None:
    with (root / "admission.lock").open("r+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _reserve(site: _Site, *, source: str = "orca_auto.orca.queue_worker") -> str:
    token = store.reserve_slot(site.admission, 4, source=source, state="reserved")
    assert token is not None
    return token


def test_idle_guard_holds_existing_lock_without_rewriting_slots(tmp_path: Path) -> None:
    site = _site(tmp_path)
    evidence = _Evidence({WORKER: site})
    before = (site.admission / "admission_slots.json").read_bytes()
    with evidence.guard():
        _assert_locked(site.admission)
    _assert_unlocked(site.admission)
    assert (site.admission / "admission_slots.json").read_bytes() == before


def test_existing_lock_with_absent_slots_file_is_idle_without_initializing_it(
    tmp_path: Path,
) -> None:
    site = _site(tmp_path, slots=False)
    with _Evidence({WORKER: site}).guard():
        _assert_locked(site.admission)
    _assert_unlocked(site.admission)
    assert not (site.admission / "admission_slots.json").exists()


@pytest.mark.parametrize("engine", ["orca", "xtb", "crest"])
def test_active_cross_engine_reservation_blocks_restart(tmp_path: Path, engine: str) -> None:
    site = _site(tmp_path)
    _reserve(site, source=f"orca_auto.{engine}.queue_worker")
    before = (site.admission / "admission_slots.json").read_bytes()
    with pytest.raises(ValueError, match="1 active or reserved"), _Evidence({WORKER: site}).guard():
        pytest.fail("an admitted calculation must block restart")
    assert (site.admission / "admission_slots.json").read_bytes() == before
    _assert_unlocked(site.admission)


@pytest.mark.parametrize("engine_state", ["pending", "active", "idle"])
def test_dead_owner_keeps_unresolved_engine_evidence(tmp_path: Path, engine_state: str) -> None:
    site = _site(tmp_path)
    _reserve(site)
    slots_path = site.admission / "admission_slots.json"
    records = json.loads(slots_path.read_text())
    records[0]["owner_pid"] = 999999999
    records[0]["engine_process_state"] = engine_state
    if engine_state == "active":
        records[0].update(
            engine_pid=999999998,
            engine_pgid=999999998,
            engine_process_start_ticks=123,
            engine_process_boot_id=records[0]["owner_boot_id"],
        )
    slots_path.write_text(json.dumps(records))
    before = slots_path.read_bytes()
    if engine_state == "idle":
        with _Evidence({WORKER: site}).guard():
            _assert_locked(site.admission)
    else:
        with (
            pytest.raises(ValueError, match="1 active or reserved"),
            _Evidence({WORKER: site}).guard(),
        ):
            pytest.fail("a dead owner cannot clear unresolved engine ownership")
    assert slots_path.read_bytes() == before


@pytest.mark.parametrize("unknown", ["boot", "process_ticks"])
def test_unknown_slot_liveness_blocks_without_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown: str
) -> None:
    site = _site(tmp_path)
    _reserve(site)
    if unknown == "boot":
        monkeypatch.setattr(store, "_linux_boot_id", lambda: None)
    else:
        monkeypatch.setattr(store, "_process_start_ticks", lambda _pid: None)
    with pytest.raises(ValueError, match="1 active or reserved"), _Evidence({WORKER: site}).guard():
        pytest.fail("unreadable process identity must not look idle")


@pytest.mark.parametrize("payload", ["{broken", "{}", "[{}]"])
def test_corrupt_admission_never_falls_back_to_zero(tmp_path: Path, payload: str) -> None:
    site = _site(tmp_path)
    slots = site.admission / "admission_slots.json"
    slots.write_text(payload)
    with (
        pytest.raises(ValueError, match="Cannot read service admission state"),
        _Evidence({WORKER: site}).guard(),
    ):
        pytest.fail("corrupt admission cannot authorize restart")
    assert slots.read_text() == payload


def test_missing_lock_is_not_created_by_restart(tmp_path: Path) -> None:
    site = _site(tmp_path, lock=False)
    with (
        pytest.raises(ValueError, match="Cannot lock existing service admission"),
        _Evidence({WORKER: site}).guard(),
    ):
        pytest.fail("restart must not initialize service-owned admission state")
    assert not (site.admission / "admission.lock").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_nonregular_or_shared_lock_is_rejected(tmp_path: Path, kind: str) -> None:
    site = _site(tmp_path, lock=False)
    lock = site.admission / "admission.lock"
    if kind == "directory":
        lock.mkdir()
    else:
        target = site.admission / "other.lock"
        target.touch(mode=0o600)
        if kind == "symlink":
            lock.symlink_to(target)
        else:
            lock.hardlink_to(target)
    with (
        pytest.raises(ValueError, match="Cannot lock existing service admission"),
        _Evidence({WORKER: site}).guard(),
    ):
        pytest.fail("restart requires a stable existing single-link regular lock")


@pytest.mark.parametrize("reserved", [False, True])
def test_stopped_worker_still_requires_idle_admission(tmp_path: Path, reserved: bool) -> None:
    site = _site(tmp_path)
    evidence = _Evidence({WORKER: site})
    evidence.units[WORKER]["MainPID"] = "0"
    evidence.environ.clear()
    if reserved:
        _reserve(site)
        with pytest.raises(ValueError, match="1 active or reserved"), evidence.guard():
            pytest.fail("a stopped worker does not clear admitted calculations")
    else:
        with evidence.guard():
            _assert_locked(site.admission)
    _assert_unlocked(site.admission)


@pytest.mark.parametrize("failure", ["returncode", "stderr", "exception"])
def test_unreadable_systemd_evidence_refuses_restart(tmp_path: Path, failure: str) -> None:
    evidence = _Evidence({WORKER: _site(tmp_path)})

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = evidence.run(argv, **kwargs)
        if "--property=Environment" in argv:
            if failure == "exception":
                raise OSError("injected bus error")
            if failure == "returncode":
                result.returncode = 1
            else:
                result.stderr = "injected bus warning"
        return result

    with (
        pytest.raises(ValueError, match="Cannot read service configuration binding"),
        cli_systemd_restart_guard.guard_service_restart(
            (WORKER,), run=run, read_process_file=evidence.read_process_file
        ),
    ):
        pytest.fail("a successful-looking property value cannot hide a failed query")


def test_caller_config_cannot_hide_service_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _site(tmp_path, "actual-service")
    caller = _site(tmp_path, "caller-empty")
    _reserve(service)
    monkeypatch.setenv("ORCA_AUTO_CONFIG", str(caller.config))
    monkeypatch.chdir(caller.config.parent)
    with (
        pytest.raises(ValueError, match="1 active or reserved"),
        _Evidence({WORKER: service}).guard(),
    ):
        pytest.fail("the running service configuration must be authoritative")


def test_config_written_after_worker_start_is_not_trusted(tmp_path: Path) -> None:
    site = _site(tmp_path)
    evidence = _Evidence({WORKER: site})
    evidence.units[WORKER]["ExecMainStartTimestamp"] = "Mon 2000-01-03 00:00:00 UTC"
    with (
        pytest.raises(ValueError, match="Cannot verify unchanged running configuration"),
        evidence.guard(),
    ):
        pytest.fail("a worker may still be using the previous admission root")


def test_process_environment_must_match_unit_config(tmp_path: Path) -> None:
    site = _site(tmp_path)
    alternate = _site(tmp_path, "other")
    evidence = _Evidence({WORKER: site})
    evidence.environ[4242] = f"ORCA_AUTO_CONFIG={alternate.config}\0".encode()
    with (
        pytest.raises(ValueError, match="Cannot verify unchanged running configuration"),
        evidence.guard(),
    ):
        pytest.fail("unit and process roots disagree")


@pytest.mark.parametrize("target", ["MainPID", "stat"])
def test_process_identity_changes_during_check_refuse_restart(tmp_path: Path, target: str) -> None:
    evidence = _Evidence({WORKER: _site(tmp_path)})
    evidence.environ[4243] = evidence.environ[4242]
    reads = 0

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal reads
        result = evidence.run(argv, **kwargs)
        if target == "MainPID" and "--property=MainPID" in argv:
            reads += 1
            if reads > 1:
                result.stdout = "4243\n"
        return result

    def read_process_file(path: str) -> bytes:
        nonlocal reads
        result = evidence.read_process_file(path)
        if target == "stat" and path.endswith("/stat"):
            reads += 1
            return _proc_stat(12345 + reads)
        return result

    with (
        pytest.raises(ValueError, match="Cannot verify unchanged running configuration"),
        cli_systemd_restart_guard.guard_service_restart(
            (WORKER,), run=run, read_process_file=read_process_file
        ),
    ):
        pytest.fail("changing process identity is not trustworthy idle evidence")


@pytest.mark.parametrize(
    ("property_name", "value", "diagnostic"),
    [
        (
            "EnvironmentFiles",
            "/tmp/another-environment-file (ignore_errors=no)",
            "overridden service configuration",
        ),
        ("UnsetEnvironment", "ORCA_AUTO_CONFIG", "overridden service configuration"),
        ("Environment", "ORCA_AUTO_CONFIG=relative.yaml", "existing absolute regular file"),
        (
            "ExecStart",
            "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 custom_worker.py ; }",
            "Unsupported worker command",
        ),
    ],
)
def test_unresolved_unit_configuration_is_rejected(
    tmp_path: Path, property_name: str, value: str, diagnostic: str
) -> None:
    evidence = _Evidence({WORKER: _site(tmp_path)})
    evidence.units[WORKER][property_name] = value
    with pytest.raises(ValueError, match=diagnostic), evidence.guard():
        pytest.fail("an unsupported launch cannot use a guessed admission root")


def test_unreadable_process_environment_is_not_silently_ignored(tmp_path: Path) -> None:
    evidence = _Evidence({WORKER: _site(tmp_path)})

    def read_process_file(path: str) -> bytes:
        if path.endswith("/environ"):
            raise PermissionError("injected process inspection denial")
        return evidence.read_process_file(path)

    with (
        pytest.raises(ValueError, match="Cannot verify unchanged running configuration"),
        cli_systemd_restart_guard.guard_service_restart(
            (WORKER,), run=evidence.run, read_process_file=read_process_file
        ),
    ):
        pytest.fail("unreadable process evidence cannot authorize restart")


def test_config_is_rechecked_after_lock_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site(tmp_path)
    original_lock = cli_systemd_restart_guard.admission_lock

    @contextmanager
    def change_config(root: str | Path) -> Iterator[None]:
        with original_lock(root):
            site.config.write_text(site.config.read_text() + "\n")
            yield

    monkeypatch.setattr(cli_systemd_restart_guard, "admission_lock", change_config)
    with (
        pytest.raises(ValueError, match="Service configuration or process changed"),
        _Evidence({WORKER: site}).guard(),
    ):
        pytest.fail("a configuration race must be caught before restart")
    _assert_unlocked(site.admission)


def test_multiple_service_roots_are_sorted_and_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    high = _site(tmp_path, "z-root")
    low = _site(tmp_path, "a-root")
    evidence = _Evidence({WORKER: high, WORKFLOW: low, "orca_auto-queue-worker@bob.service": high})
    original_lock = cli_systemd_restart_guard.admission_lock
    acquired: list[Path] = []

    @contextmanager
    def record_lock(root: str | Path) -> Iterator[None]:
        acquired.append(Path(root))
        with original_lock(root):
            yield

    monkeypatch.setattr(cli_systemd_restart_guard, "admission_lock", record_lock)
    with evidence.guard():
        _assert_locked(low.admission)
        _assert_locked(high.admission)
    assert acquired == [low.admission, high.admission]


def test_guard_releases_lock_when_guarded_operation_raises(tmp_path: Path) -> None:
    site = _site(tmp_path)
    with pytest.raises(RuntimeError, match="operation failed"), _Evidence({WORKER: site}).guard():
        raise RuntimeError("operation failed")
    _assert_unlocked(site.admission)


def _reserve_in_process(root: str, connection: Any) -> None:
    try:
        connection.send("attempting-reservation")
        token = store.reserve_slot(Path(root), 4, source="test.concurrent-restart")
        connection.send(("reserved", token))
    except (OSError, RuntimeError, ValueError) as exc:
        connection.send(("error", type(exc).__name__))
    finally:
        connection.close()


@pytest.mark.parametrize("failure_index", [None, 0, 1, 2])
def test_reservation_waits_through_entire_restart_and_resumes_after_exit(
    tmp_path: Path, failure_index: int | None
) -> None:
    site = _site(tmp_path)
    evidence = _Evidence({WORKER: site})
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_reserve_in_process, args=(str(site.admission), child))
    mutations: list[str] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1] == "show":
            return evidence.run(argv, **kwargs)
        if argv[1] == "is-active":
            return subprocess.CompletedProcess(argv, 3, stdout="inactive\n", stderr="")
        assert argv[1] in {"restart", "reset-failed"}
        _assert_locked(site.admission)
        if not mutations:
            process.start()
            child.close()
            assert parent.poll(5), "reservation process did not start"
            assert parent.recv() == "attempting-reservation"
        assert not parent.poll(0.1), "a calculation was admitted between restart commands"
        mutations.append(argv[1])
        return subprocess.CompletedProcess(argv, 5 if len(mutations) - 1 == failure_index else 0)

    def guard(worker_units: tuple[str, ...], *, run: Any) -> Any:
        return cli_systemd_restart_guard.guard_service_restart(
            worker_units, run=run, read_process_file=evidence.read_process_file
        )

    try:
        result = cli_systemd_restart.cmd_service_restart(
            Namespace(target_user="alice"),
            deps=cli_systemd_restart.ServiceRestartDeps(
                run=run,
                is_root=lambda: True,
                which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
                restart_unit_for_user=lambda target_user, run: (
                    f"orca_auto-runtime@{target_user}.target"
                ),
                restart_guard=guard,
            ),
        )
        assert result == (0 if failure_index is None else 5)
        assert len(mutations) == (3 if failure_index is None else failure_index + 1)
        assert parent.poll(5), "reservation stayed blocked after restart returned"
        outcome, token = parent.recv()
        assert outcome == "reserved" and token
        process.join(5)
        assert process.exitcode == 0
        _assert_unlocked(site.admission)
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(5)
        parent.close()
        child.close()
