"""Golden CLI documents: JSON and plain-text command output, the argparse surface and unit text.

Each document is compared with ``golden/cli/<name>`` after normalization. A
JSON document is stored with its exit code and stderr; a text document as the
exit code, stdout and stderr in one file.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from argparse import Namespace
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from orca_auto import activity_labels, cli_systemd_status, systemd_plan, terminal_table
from orca_auto._version import package_version
from orca_auto.cli import build_parser
from orca_auto.cli import main as cli_main
from orca_auto.orca import direct_cancel
from orca_auto.orca.scratch_config import ScratchConfig
from tests.conftest import enqueue_entry, make_queue_entry
from tests.contracts.conftest import Harness
from tests.contracts.normalize import Normalizer, assert_golden

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ELAPSED_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}\b")


def _json_document(h: Harness, name: str, *argv: str) -> dict[str, Any]:
    """Run one ``--json`` command and compare its exit code, stdout and stderr."""
    rc, out, err = h.cli(*argv)
    document = {"exit_code": rc, "stdout": json.loads(out) if out else None, "stderr": err}
    normalized: dict[str, Any] = h.n(document)
    assert_golden(f"cli/{name}.json", normalized)
    return document


def _text_document(h: Harness, name: str, *argv: str) -> None:
    rc, out, err = h.cli(*argv)
    text = h.n.text(f"exit={rc}\n--- stdout\n{out}--- stderr\n{err}")
    # Elapsed cells depend on how long the fake calculation took.
    assert_golden(f"cli/{name}.txt", _ELAPSED_RE.sub("<hh:mm:ss>", text))


def _queue_id(h: Harness, job: Path) -> str:
    [row] = [row for row in h.rows() if row["metadata"]["reaction_dir"] == str(job)]
    queue_id: str = row["queue_id"]
    return queue_id


def test_run_dir_json(harness: Harness) -> None:
    job = harness.job("submitted")
    _json_document(harness, "run_dir", "run-dir", str(job), "--json")


def test_queue_list_and_clear(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    done = harness.job("done")
    assert harness.cli("run-dir", str(done))[0] == 0
    assert harness.run_worker() == 0
    stopped = harness.job("stopped")
    assert harness.cli("run-dir", str(stopped))[0] == 0
    assert harness.cli("queue", "cancel", _queue_id(harness, stopped))[0] == 0
    waiting = harness.job("waiting")
    assert harness.cli("run-dir", str(waiting))[0] == 0
    # Relative age columns are measured from the enqueue time of the newest row.
    newest = max(row["enqueued_at"] for row in harness.rows())
    monkeypatch.setattr(activity_labels, "queue_table_now", lambda: datetime.fromisoformat(newest))
    # Piped output: no terminal width, whatever terminal runs the tests.
    monkeypatch.setattr(terminal_table, "terminal_max_width", lambda: None)

    _json_document(harness, "queue_list", "queue", "list", "--json")
    _text_document(harness, "queue_list_plain", "queue", "list")
    _json_document(harness, "queue_list_clear", "queue", "list", "clear", "--json")
    _json_document(harness, "queue_list_after_clear", "queue", "list", "--json")


def test_queue_cancel_outcomes(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    done = harness.job("done")
    assert harness.cli("run-dir", str(done))[0] == 0
    assert harness.run_worker() == 0
    first = harness.job("first")
    second = harness.job("second")
    for job in (first, second):
        assert harness.cli("run-dir", str(job))[0] == 0
    # Fixed ids: an ambiguity error lists its matches in id order.
    rows = [
        ("same-a", harness.job("a/same_name"), {}),
        ("same-b", harness.job("b/same_name"), {}),
        ("legacy-row", harness.job("legacy_owned"), {"workflow_id": "legacy-flow"}),
    ]
    for queue_id, job, metadata in rows:
        enqueue_entry(
            harness.runs,
            make_queue_entry(
                queue_id=queue_id,
                task_id=f"{queue_id}-task",
                reaction_dir=job,
                metadata=metadata,
            ),
        )

    _json_document(harness, "queue_cancel_ambiguous", "queue", "cancel", "same_name", "--json")
    _json_document(harness, "queue_cancel_not_found", "queue", "cancel", "no-such-job", "--json")
    _json_document(
        harness,
        "queue_cancel_already_terminal",
        "queue",
        "cancel",
        _queue_id(harness, done),
        "--json",
    )
    _json_document(harness, "queue_cancel_retired", "queue", "cancel", "legacy-row", "--json")
    _json_document(
        harness,
        "queue_cancel_success",
        "queue",
        "cancel",
        _queue_id(harness, first),
        "--json",
    )

    real_cancel = direct_cancel.queue_adapter.cancel

    def cancel_then_fail(*args: Any, **kwargs: Any) -> Any:
        real_cancel(*args, **kwargs)
        raise OSError("lost the reply after the cancel was written")

    monkeypatch.setattr(direct_cancel.queue_adapter, "cancel", cancel_then_fail)
    _json_document(
        harness,
        "queue_cancel_exception_after_commit",
        "queue",
        "cancel",
        _queue_id(harness, second),
        "--json",
    )


def test_index_rebuild_dry_run_and_prune(harness: Harness) -> None:
    kept = harness.job("kept")
    gone = harness.job("gone")
    for job in (kept, gone):
        assert harness.cli("run-dir", str(job))[0] == 0
        assert harness.run_worker() == 0
    (harness.runs / "job_locations.json").unlink()
    _json_document(harness, "index_rebuild_dry_run", "index", "rebuild", "--dry-run", "--json")
    assert harness.cli("index", "rebuild")[0] == 0
    shutil.rmtree(gone)
    _json_document(harness, "index_prune", "index", "prune", "--json")


def test_scratch_list_and_clear(harness: Harness, shm_scratch_root: Path) -> None:
    harness.paths[shm_scratch_root] = "<scratch>"
    harness.configure(scratch=ScratchConfig(root=str(shm_scratch_root), min_free_gb=1))
    _json_document(harness, "scratch_list_empty", "scratch", "list", "--json")
    # A workspace without a manifest cannot be verified and blocks scratch launches.
    (shm_scratch_root / "attempt-999999-0123456789abcdef").mkdir(mode=0o700)
    _json_document(harness, "scratch_list_invalid", "scratch", "list", "--json")
    _json_document(harness, "scratch_clear_all_stale", "scratch", "clear", "--all-stale", "--json")


def test_queue_worker_json(harness: Harness) -> None:
    harness.paths[sys.executable] = "<python>"
    _json_document(harness, "queue_worker", "queue", "worker", "--json")


def _systemctl_states(states: dict[tuple[str, str], str]) -> Callable[..., Any]:
    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=f"{states[(argv[1], argv[2])]}\n")

    return run


@pytest.mark.parametrize(
    ("name", "worker_active"), [("healthy", "active"), ("worker_failed", "failed")]
)
def test_service_status_json(
    name: str, worker_active: str, capsys: pytest.CaptureFixture[str]
) -> None:
    states = {
        ("is-active", "orca_auto-runtime@alice.target"): "active",
        ("is-enabled", "orca_auto-runtime@alice.target"): "enabled",
        ("is-active", "orca_auto-engine-workers@alice.target"): "active",
        ("is-enabled", "orca_auto-engine-workers@alice.target"): "disabled",
        ("is-active", "orca_auto-queue-worker@alice.service"): worker_active,
        ("is-enabled", "orca_auto-queue-worker@alice.service"): "disabled",
        ("is-active", "orca_auto-bot@alice.service"): "inactive",
        ("is-enabled", "orca_auto-bot@alice.service"): "disabled",
    }
    rc = cli_systemd_status.cmd_service_status(
        Namespace(target_user=None, json=True),
        deps=cli_systemd_status.ServiceStatusDeps(
            default_service_user=lambda: "alice",
            run=_systemctl_states(states),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            collect_worker_staleness=lambda statuses, run=None: None,
        ),
    )
    captured = capsys.readouterr()
    assert_golden(
        f"cli/service_status_{name}.json",
        {"exit_code": rc, "stdout": json.loads(captured.out), "stderr": captured.err},
    )


def _parser_tree(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Subcommands, option strings, choices and defaults; destination names are internal."""
    arguments: list[dict[str, Any]] = []
    subcommands: dict[str, Any] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            arguments.append({"subcommand": True, "required": action.required})
            for name, subparser in action.choices.items():
                subcommands[name] = _parser_tree(subparser)
            continue
        argument: dict[str, Any] = {
            "options": list(action.option_strings),
            "action": type(action).__name__,
            "nargs": action.nargs,
            "choices": list(action.choices) if action.choices is not None else None,
            "default": action.default,
            "required": action.required,
        }
        version = getattr(action, "version", None)
        if version is not None:
            argument["version"] = version.replace(package_version(), "<version>")
        arguments.append(argument)
    return {"prog": parser.prog, "arguments": arguments, "subcommands": subcommands}


def test_argparse_surface() -> None:
    assert_golden("cli/argparse_tree.json", _parser_tree(build_parser()))


def _unit_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout with unit templates, a venv python and a fixed config."""
    repo = tmp_path / "orca_auto"
    shutil.copytree(_REPO_ROOT / "systemd", repo / "systemd")
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    orca = repo / "orca"
    orca.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    orca.chmod(0o755)
    for name in ("orca_runs", "admission"):
        (repo / name).mkdir()
    config = repo / "config" / "orca_auto.yaml"
    config.parent.mkdir()
    config.write_text(
        f"runs_root: {repo / 'orca_runs'}\n"
        "scheduler:\n"
        "  max_active_simulations: 2\n"
        f"  admission_root: {repo / 'admission'}\n"
        "orca:\n"
        "  paths:\n"
        f"    orca_executable: {orca}\n",
        encoding="utf-8",
    )
    return repo, config


def test_systemd_units_for_fixed_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    n = Normalizer({tmp_path: "<tmp>"})
    repo, config = _unit_repo(tmp_path)
    unit_dir = tmp_path / "units"
    exit_code = cli_main(
        [
            "systemd",
            "install",
            "--user",
            "alice",
            "--repo",
            str(repo),
            "--config",
            str(config),
            "--unit-dir",
            str(unit_dir),
            "--no-sudo",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert_golden(
        "cli/systemd_install_dry_run.txt",
        n.text(f"exit={exit_code}\n--- stdout\n{captured.out}--- stderr\n{captured.err}"),
    )
    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config,
        unit_dir=unit_dir,
        no_sudo=True,
        is_root=lambda: False,
    )
    assert_golden(
        "cli/systemd_units.txt",
        n.text("".join(f"### {unit.name}\n{unit.content}" for unit in plan.units)),
    )
