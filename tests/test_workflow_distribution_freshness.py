from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orca_auto import _process_evidence, cli_systemd_units
from orca_auto import cli_systemd_freshness as freshness
from tests.test_cli_systemd_freshness import _fake_proc_stat


def test_real_git_inventory_observes_the_relocated_workflow_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    core = tmp_path / "src/orca_auto/_process_evidence.py"
    flow = tmp_path / "extensions/workflows/src/orca_auto/flow/__init__.py"
    module = flow.parent / "changed.py"
    for path in (core, flow, module):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# original\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, timeout=10)
    assert freshness._tracked_checkout_for_import_source(flow, run=subprocess.run) == tmp_path
    assert not freshness._checkout_import_package_dirty(tmp_path, flow, run=subprocess.run)
    module.write_text("# changed extension source\n", encoding="utf-8")
    assert freshness._checkout_import_package_dirty(tmp_path, flow, run=subprocess.run)
    assert not freshness._checkout_import_package_dirty(tmp_path, core, run=subprocess.run)


@pytest.mark.parametrize(
    "condition,expected",
    [
        ("clean", "workers"),
        ("extension_dirty", "undetermined"),
        ("extension_updated", "stale"),
        ("core_updated", "stale"),
        ("missing_extension_evidence", "undetermined"),
        ("extension_wheel", "workers"),
        ("core_wheel", "workers"),
        ("both_wheels", None),
    ],
)
def test_workflow_worker_judges_both_distribution_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, condition: str, expected: str | None
) -> None:
    core_root = tmp_path / "core-checkout"
    extension_root = tmp_path / "extension-checkout"
    core_source = core_root / "src" / "orca_auto" / "_process_evidence.py"
    extension_source = (
        extension_root / "extensions" / "workflows" / "src" / "orca_auto" / "flow" / "__init__.py"
    )
    for source in (core_source, extension_source):
        source.parent.mkdir(parents=True)
        source.write_text("# source evidence\n", encoding="utf-8")
    roots: dict[Path, Path | None] = {core_source: core_root, extension_source: extension_root}
    if condition in {"core_wheel", "both_wheels"}:
        roots[core_source] = None
    if condition in {"extension_wheel", "both_wheels"}:
        roots[extension_source] = None
    monkeypatch.setattr(freshness, "_unit_main_pid", lambda *_args, **_kwargs: 77)
    monkeypatch.setattr(freshness, "_unit_start_epoch", lambda *_args, **_kwargs: 200.0)
    monkeypatch.setattr(
        freshness, "_tracked_checkout_for_import_source", lambda source, **_: roots[source]
    )
    inspected: list[Path] = []

    def dirty(_root: Path, source: Path, **_: object) -> bool:
        inspected.append(source)
        return condition == "extension_dirty" and source == extension_source

    monkeypatch.setattr(freshness, "_checkout_import_package_dirty", dirty)

    def head(root: Path, **_: object) -> freshness._CheckoutHeadEvidence:
        updated = (root == extension_root and condition == "extension_updated") or (
            root == core_root and condition == "core_updated"
        )
        return freshness._CheckoutHeadEvidence(root, "a" * 40, 50, 300 if updated else 100)

    monkeypatch.setattr(freshness, "_checkout_head_evidence", head)

    def process_file(path: str) -> bytes:
        if path.endswith("/stat"):
            return _fake_proc_stat(77)
        assert path == "/proc/77/environ"
        value = f"{_process_evidence.PROCESS_IMPORT_SOURCE_ENV}={core_source}\0"
        if condition != "missing_extension_evidence":
            value += f"{_process_evidence.PROCESS_WORKFLOW_IMPORT_SOURCE_ENV}={extension_source}\0"
        return value.encode()

    status = cli_systemd_units.ServiceUnitStatus(
        "workflow", "workflow.service", "active", "enabled"
    )
    result = freshness.collect_worker_staleness((status,), read_process_file=process_file)
    if expected is None:
        assert result is None
        return
    assert result is not None
    assert len(result[expected]) == 1
    row = result[expected][0]
    assert row["unit"] == status.unit
    assert set(row["components"]) == {"core", "workflows"}
    if expected == "undetermined":
        assert result["workers"] == []
        assert result["stale"] == []
        assert row["detail"].startswith("workflows:")
    else:
        assert result["undetermined"] == []
        assert row["components"]["core"]["import_source"] == str(core_source)
        assert row["components"]["workflows"]["import_source"] == str(extension_source)
        assert len(result["stale"]) == int(expected == "stale")
    if condition in {"clean", "extension_dirty", "extension_updated", "core_updated"}:
        assert core_source in inspected
        assert extension_source in inspected


def test_workflow_source_observations_cannot_merge_different_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(freshness, "_unit_main_pid", lambda *_args, **_kwargs: 77)
    monkeypatch.setattr(freshness, "_read_process_start_ticks", lambda *_args, **_kwargs: 123)

    def source_verdict(
        _status: object, *, source_env_var: str, **_: object
    ) -> freshness._WorkerVerdict:
        pid = 77 if source_env_var == _process_evidence.PROCESS_IMPORT_SOURCE_ENV else 78
        return freshness._WorkerVerdict("worker", {"pid": pid})

    monkeypatch.setattr(freshness, "_judge_worker_source", source_verdict)
    status = cli_systemd_units.ServiceUnitStatus(
        "workflow", "workflow.service", "active", "enabled"
    )
    result = freshness.collect_worker_staleness((status,))
    assert result is not None
    assert result["workers"] == []
    assert "main PID changed between" in result["undetermined"][0]["detail"]
