from __future__ import annotations

import os
import subprocess
import sys
from importlib import metadata
from importlib.machinery import ModuleSpec
from pathlib import Path

import pytest

from orca_auto import _process_evidence
from orca_auto.core import extensions


@pytest.mark.parametrize(
    "core_version,workflow_version", [("5.0.0.dev0", "5.0.0.dev0"), ("5", "4")]
)
def test_workflow_installation_requires_equal_versions(
    monkeypatch: pytest.MonkeyPatch, core_version: str, workflow_version: str
) -> None:
    versions = {"orca_auto": core_version, "orca_auto_workflows": workflow_version}
    monkeypatch.setattr(extensions.metadata, "version", versions.__getitem__)
    if core_version == workflow_version:
        extensions.validate_workflow_installation()
    else:
        with pytest.raises(ValueError, match="core/workflows version mismatch"):
            extensions.validate_workflow_installation()


@pytest.mark.parametrize("missing", ["orca_auto", "orca_auto_workflows"])
def test_workflow_installation_requires_distribution_metadata(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    def version(name: str) -> str:
        if name == missing:
            raise metadata.PackageNotFoundError(name)
        return "5.0.0.dev0"

    monkeypatch.setattr(extensions.metadata, "version", version)
    with pytest.raises(ValueError, match="requires installed metadata for both"):
        extensions.validate_workflow_installation()


def test_cache_only_workflow_namespace_is_not_an_installed_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = ModuleSpec("orca_auto.flow", None, is_package=True)
    monkeypatch.setattr(extensions, "find_spec", lambda _: namespace)
    assert not extensions.workflows_available()
    with pytest.raises(ValueError, match="workflows extension is not installed"):
        extensions.require_workflows()


def test_direct_workflow_import_rejects_mismatched_distribution_versions() -> None:
    script = """
import importlib.metadata
import orca_auto
versions = {'orca_auto': '5.0.0.dev0', 'orca_auto_workflows': '4.1.0'}
importlib.metadata.version = versions.__getitem__
import orca_auto.flow
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0
    assert "core/workflows version mismatch" in result.stderr


@pytest.mark.parametrize("app_args", [["--app", "workflow"], ["--app=workflow"]])
def test_workflow_worker_reexec_records_both_actual_import_sources(
    monkeypatch: pytest.MonkeyPatch, app_args: list[str]
) -> None:
    from orca_auto import flow

    monkeypatch.setattr(
        _process_evidence.sys, "argv", ["orca_auto.cli", "queue", "worker", *app_args]
    )
    monkeypatch.delenv(_process_evidence.PROCESS_IMPORT_SOURCE_ENV, raising=False)
    monkeypatch.delenv(_process_evidence.PROCESS_WORKFLOW_IMPORT_SOURCE_ENV, raising=False)
    captured: dict[str, str] = {}

    def execve(_executable: str, _argv: list[str], environment: dict[str, str]) -> None:
        captured.update(environment)

    monkeypatch.setattr(_process_evidence.os, "execve", execve)
    _process_evidence.exec_with_import_source_evidence()
    assert captured[_process_evidence.PROCESS_IMPORT_SOURCE_ENV] == str(
        Path(_process_evidence.__file__).resolve()
    )
    assert flow.__file__ is not None
    assert captured[_process_evidence.PROCESS_WORKFLOW_IMPORT_SOURCE_ENV] == str(
        Path(flow.__file__).resolve()
    )
    for key in (
        _process_evidence.PROCESS_IMPORT_SOURCE_ENV,
        _process_evidence.PROCESS_WORKFLOW_IMPORT_SOURCE_ENV,
    ):
        monkeypatch.setenv(key, captured[key])
    monkeypatch.setattr(
        _process_evidence.os, "execve", lambda *_: pytest.fail("matching evidence re-executed")
    )
    _process_evidence.exec_with_import_source_evidence()


def test_core_worker_clears_inherited_workflow_evidence_without_importing_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _process_evidence.sys, "argv", ["orca_auto.cli", "queue", "worker", "--app", "orca"]
    )
    monkeypatch.setenv(
        _process_evidence.PROCESS_IMPORT_SOURCE_ENV, str(Path(_process_evidence.__file__).resolve())
    )
    monkeypatch.setenv(
        _process_evidence.PROCESS_WORKFLOW_IMPORT_SOURCE_ENV, "/old/flow/__init__.py"
    )
    monkeypatch.setattr(
        _process_evidence, "import_module", lambda _: pytest.fail("ORCA worker imported workflows")
    )
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        _process_evidence.os, "execve", lambda _exe, _argv, env: captured.update(env)
    )
    _process_evidence.exec_with_import_source_evidence()
    assert _process_evidence.PROCESS_WORKFLOW_IMPORT_SOURCE_ENV not in captured
    assert (
        captured[_process_evidence.PROCESS_IMPORT_SOURCE_ENV]
        == os.environ[_process_evidence.PROCESS_IMPORT_SOURCE_ENV]
    )
