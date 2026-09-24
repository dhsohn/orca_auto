from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

from orca_auto import (
    _process_evidence,
    cli_systemd_freshness,
    cli_systemd_status,
    cli_systemd_units,
)
from orca_auto.core.runtime_bundle import (
    PROCESS_RUNTIME_BUILD_ENV,
    RUNTIME_MANIFEST_NAME,
    runtime_build_id,
    runtime_inventory,
    verify_runtime_bundle,
)
from orca_auto.systemd_plan import build_systemd_install_plan
from tests.test_cli_systemd_restart_guard import WORKER, _assert_locked, _Evidence, _reserve, _site


def _make_bundle(root: Path, *, version: str = "6.0.0") -> Path:
    source = root / ".venv/lib/python3/site-packages/orca_auto/_process_evidence.py"
    source.parent.mkdir(parents=True)
    source.write_text("# fixture package\n")
    shutil.copytree(Path(__file__).resolve().parents[1] / "systemd", root / "systemd")
    for path in root.rglob("*"):
        path.chmod(path.stat().st_mode & ~0o222)
    identity = {"version": version, "wheels": [], "systemd": {}}
    manifest = {
        "schema_version": 1,
        "state": "ready",
        "runtime_root": str(root),
        "identity": identity,
        "build_id": runtime_build_id(identity),
        "files": runtime_inventory(root),
    }
    marker = root / RUNTIME_MANIFEST_NAME
    marker.write_text(json.dumps(manifest))
    marker.chmod(0o444)
    root.chmod(0o555)
    return root


def _unseal(root: Path) -> None:
    root.chmod(0o755)
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


@pytest.fixture
def bundle(tmp_path: Path) -> Iterator[Path]:
    root = _make_bundle(tmp_path / "release")
    yield root
    _unseal(root)


def _source(root: Path) -> Path:
    return root / ".venv/lib/python3/site-packages/orca_auto/_process_evidence.py"


def test_runtime_receipts_reject_changed_code_even_when_read_only(bundle: Path) -> None:
    assert verify_runtime_bundle(bundle)["identity"]["version"] == "6.0.0"
    source = _source(bundle)
    source.chmod(0o644)
    source.write_text("# changed after preparation\n")
    source.chmod(0o444)
    with pytest.raises(ValueError, match="differ from the prepared build"):
        verify_runtime_bundle(bundle)


def test_runtime_receipts_reject_writable_code(bundle: Path) -> None:
    _source(bundle).chmod(0o644)
    with pytest.raises(ValueError, match="writable"):
        verify_runtime_bundle(bundle)


def test_systemd_plan_pins_build_and_external_config(bundle: Path, tmp_path: Path) -> None:
    manifest = verify_runtime_bundle(bundle)
    plan = build_systemd_install_plan(
        target_user="testuser",
        repo=bundle,
        config=tmp_path / "config.yaml",
        no_enable=True,
        no_sudo=True,
    )
    worker = next(
        unit.content for unit in plan.units if unit.name == "orca_auto-queue-worker@.service"
    )
    assert f"Environment={PROCESS_RUNTIME_BUILD_ENV}={manifest['build_id']}" in worker
    assert f"ReadOnlyPaths={bundle}" in worker
    assert f"{bundle}/.venv/bin/python -I -m orca_auto.cli" in worker
    with pytest.raises(ValueError, match="configuration must be outside"):
        build_systemd_install_plan(
            target_user="testuser",
            repo=bundle,
            config=bundle / "config.yaml",
            no_enable=True,
        )
    # Omitting --config renders the target user's discoverable home config,
    # never a path inside the runtime.
    default_plan = build_systemd_install_plan(target_user="testuser", repo=bundle, no_enable=True)
    assert default_plan.config == Path("/home/testuser/orca_auto/config/orca_auto.yaml")


def test_incomplete_runtime_cannot_install_units(tmp_path: Path) -> None:
    (tmp_path / RUNTIME_MANIFEST_NAME).write_text('{"schema_version": 1, "state": "preparing"}')
    with pytest.raises(ValueError, match="incomplete"):
        build_systemd_install_plan(target_user="testuser", repo=tmp_path, no_enable=True)


@pytest.mark.parametrize("correct_identity", [True, False])
def test_status_binds_runtime_build_to_active_process(bundle: Path, correct_identity: bool) -> None:
    build_id = verify_runtime_bundle(bundle)["build_id"] if correct_identity else "wrong-build"
    status = cli_systemd_units.ServiceUnitStatus(
        label="worker",
        unit="orca_auto-queue-worker@testuser.service",
        active="active",
        enabled="enabled",
    )

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        values = {
            "MainPID": "41",
            "Environment": f"{PROCESS_RUNTIME_BUILD_ENV}={verify_runtime_bundle(bundle)['build_id']}",
            "WorkingDirectory": str(bundle),
            "EnvironmentFiles": "",
            "UnsetEnvironment": "",
        }
        prop = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--property="))
        return subprocess.CompletedProcess(argv, 0, stdout=values[prop] + "\n", stderr="")

    def read(path: str) -> bytes:
        if path.endswith("/stat"):
            fields = ["S", *("0" for _ in range(18)), "123456"]
            return f"41 (worker) {' '.join(fields)}".encode()
        return (
            f"{_process_evidence.PROCESS_IMPORT_SOURCE_ENV}={_source(bundle)}\0"
            f"{PROCESS_RUNTIME_BUILD_ENV}={build_id}\0"
        ).encode()

    payload = cli_systemd_freshness.collect_worker_staleness(
        [status], run=run, read_process_file=read
    )
    assert payload is not None
    if correct_identity:
        assert payload["workers"][0]["runtime_build_id"] == build_id
        assert payload["undetermined"] == []
    else:
        assert "mismatched" in payload["undetermined"][0]["detail"]


def test_worker_reexec_publishes_verified_build_evidence(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_process_evidence, "__file__", str(_source(bundle)))
    monkeypatch.setattr(_process_evidence.sys, "argv", ["orca_auto", "queue", "worker"])
    monkeypatch.delenv(PROCESS_RUNTIME_BUILD_ENV, raising=False)
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        _process_evidence.os, "execve", lambda executable, argv, env: calls.append(env)
    )
    _process_evidence.exec_with_import_source_evidence()
    assert calls[0][PROCESS_RUNTIME_BUILD_ENV] == verify_runtime_bundle(bundle)["build_id"]


@pytest.mark.parametrize("busy", [False, True])
def test_rendered_runtime_command_keeps_idle_restart_guard(
    bundle: Path, tmp_path: Path, busy: bool
) -> None:
    site = _site(tmp_path)
    plan = build_systemd_install_plan(
        target_user="alice", repo=bundle, config=site.config, no_enable=True, no_sudo=True
    )
    worker = next(
        unit.content for unit in plan.units if unit.name == "orca_auto-queue-worker@.service"
    )
    command = next(
        line.removeprefix("ExecStart=")
        for line in worker.splitlines()
        if line.startswith("ExecStart=")
    )
    evidence = _Evidence({WORKER: site})
    evidence.units[WORKER]["ExecStart"] = (
        f"{{ path={shlex.split(command)[0]} ; argv[]={command} ; ignore_errors=no ; }}"
    )
    if busy:
        _reserve(site)
        with pytest.raises(ValueError, match="1 active or reserved"), evidence.guard():
            pytest.fail("a managed runtime must preserve admission protection")
    else:
        with evidence.guard():
            _assert_locked(site.admission)


@pytest.mark.parametrize("old_install", ["managed", "editable", "same-build-other-root"])
def test_status_detects_installed_unit_cutover_before_worker_restart(
    bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], old_install: str
) -> None:
    desired = _make_bundle(
        tmp_path / "next-release",
        version="6.0.0" if old_install == "same-build-other-root" else "6.0.1",
    )
    desired_build = verify_runtime_bundle(desired)["build_id"]
    source = _source(bundle)
    build = verify_runtime_bundle(bundle)["build_id"]
    if old_install == "editable":
        source = tmp_path / "checkout/src/orca_auto/_process_evidence.py"
        source.parent.mkdir(parents=True)
        source.write_text("# editable package\n")
        checkout = tmp_path / "checkout"
        subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "add", "src"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--quiet",
                "-m",
                "fixture",
            ],
            check=True,
        )
        build = ""
    status = cli_systemd_units.ServiceUnitStatus(
        label="worker", unit=WORKER, active="active", enabled="enabled"
    )

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        prop = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--property="))
        values = {
            "MainPID": "41",
            "Environment": f"{PROCESS_RUNTIME_BUILD_ENV}={desired_build}",
            "WorkingDirectory": str(desired),
            "EnvironmentFiles": "",
            "UnsetEnvironment": "",
            "ExecMainStartTimestamp": "Mon 2099-01-05 00:00:00 UTC",
        }
        return subprocess.CompletedProcess(argv, 0, stdout=values[prop] + "\n", stderr="")

    def read(path: str) -> bytes:
        if path.endswith("/stat"):
            return f"41 (worker) {' '.join(['S', *('0' for _ in range(18)), '123456'])}".encode()
        return f"{_process_evidence.PROCESS_IMPORT_SOURCE_ENV}={source}\0{PROCESS_RUNTIME_BUILD_ENV}={build}\0".encode()

    try:
        payload = cli_systemd_freshness.collect_worker_staleness(
            [status], run=run, read_process_file=read
        )
        assert payload is not None
        assert payload["undetermined"] == []
        assert payload["stale"][0]["expected_runtime_build_id"] == desired_build
        assert payload["stale"][0]["expected_runtime_root"] == str(desired)
        assert (
            cli_systemd_status.cmd_service_status(
                Namespace(user="alice", json=False),
                deps=cli_systemd_status.ServiceStatusDeps(
                    which=lambda command: command,
                    collect_service_status=lambda *args, **kwargs: (status,),
                    collect_worker_staleness=lambda *args, **kwargs: payload,
                ),
            )
            == 1
        )
        assert f"installed unit requires runtime {desired_build}" in capsys.readouterr().err
    finally:
        _unseal(desired)


@pytest.mark.parametrize("retired_version", ["6.0.0", "7.0.0"])
def test_runtime_preparation_refuses_retired_distribution_before_creating_runtime(
    tmp_path: Path, retired_version: str
) -> None:
    from scripts.prepare_runtime import prepare_runtime

    wheels = []
    for name, version in (("orca_auto", "7.0.0"), ("orca_auto_workflows", retired_version)):
        wheel = tmp_path / f"{name}-{version}-py3-none-any.whl"
        with ZipFile(wheel, "w") as archive:
            archive.writestr(
                f"{name}-{version}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            )
        wheels.append(wheel)
    releases = tmp_path / "releases"
    with pytest.raises(ValueError, match="workflow distributions are no longer supported"):
        prepare_runtime(wheels=wheels, releases_root=releases, templates=tmp_path / "absent")
    assert not releases.exists()
