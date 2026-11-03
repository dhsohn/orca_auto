"""Prepared-runtime and service-restart fixtures shared by the runtime, systemd and
distribution tests."""

from __future__ import annotations

import fcntl
import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orca_auto import cli_systemd_restart_guard
from orca_auto.core.admission import admission_dir, store


def unseal_runtime(root: Path) -> None:
    root.chmod(0o755)
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


WORKER = "orca_auto-queue-worker@alice.service"


@dataclass(frozen=True)
class AdmissionSite:
    config: Path
    admission: Path


def admission_site(
    tmp_path: Path, name: str = "service", *, lock: bool = True, slots: bool = True
) -> AdmissionSite:
    base = tmp_path / name
    base.mkdir(parents=True)
    (base / "runs").mkdir()
    admission = admission_dir(base / "runs")
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
                "scheduler": {"max_active_simulations": 4},
            }
        ),
        encoding="utf-8",
    )
    return AdmissionSite(config, admission)


def proc_stat_bytes(ticks: int = 12345) -> bytes:
    fields = ["S", *[str(index) for index in range(1, 19)], str(ticks)]
    return ("4242 (python worker) " + " ".join(fields)).encode()


class RestartEvidence:
    def __init__(self, sites: dict[str, AdmissionSite]) -> None:
        self.units: dict[str, dict[str, str]] = {}
        self.environ: dict[int, bytes] = {}
        self.commands: list[tuple[str, ...]] = []
        for index, (unit, site) in enumerate(sites.items(), start=4242):
            self.units[unit] = {
                "EnvironmentFiles": "",
                "UnsetEnvironment": "",
                "Environment": shlex.quote(f"ORCA_AUTO_CONFIG={site.config}"),
                "ExecStart": (
                    "{ path=/fixture/.venv/bin/python ; argv[]=/fixture/.venv/bin/python -m orca_auto.cli "
                    "queue worker ; ignore_errors=no ; }"
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
        return proc_stat_bytes()

    def guard(self) -> Any:
        [unit] = self.units
        return cli_systemd_restart_guard.guard_service_restart(
            unit, run=self.run, read_process_file=self.read_process_file
        )


def assert_admission_locked(root: Path) -> None:
    with (root / "admission.lock").open("r+") as handle:
        with pytest.raises(BlockingIOError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def reserve_admission_slot(
    site: AdmissionSite, *, source: str = "orca_auto.orca.queue_worker"
) -> str:
    token = store.reserve_slot(site.admission, 4, source=source, state="reserved")
    assert token is not None
    return token
