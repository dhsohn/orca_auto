"""Every Python child keeps the import isolation of the interpreter that starts it.

Installed and prepared-runtime units start the supervisor with ``python -I``.
The supervisor starts the queue worker, the worker starts each job child, and
the job child runs the ORCA launch gate. If any of those commands dropped
``-I``, that child would read ``PYTHONPATH``, the user site and the current
directory, and could import another ``orca_auto`` than the one its parent
verified.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import orca_auto
from orca_auto.core.utils import process as process_utils

_REAL_PACKAGE = str(Path(orca_auto.__file__).resolve())

# Prints the two command prefixes the real builders produce in this
# interpreter: supervisor -> queue worker and queue worker -> job child.
_PRINT_CHILD_COMMANDS = """
import json, sys
from types import SimpleNamespace
from orca_auto import cli_workers
from orca_auto.orca.commands.queue import QUEUE_WORKER_MODULE
from orca_auto.orca.worker_execution import WORKER_JOB_MODULE, build_worker_child_command

captured = {}
cli_workers._load_worker_config = lambda _path: None
cli_workers.command_config_path = lambda _args: "/synthetic/orca_auto.yaml"
cli_workers.emit_json = lambda payload: captured.update(payload)
cli_workers.cmd_queue_worker(SimpleNamespace(json=True))
worker = captured["workers"][0]["argv"]
job = build_worker_child_command(config_path="/c.yaml", queue_root="/q", queue_id="q1")
print(json.dumps({
    "worker": [worker, QUEUE_WORKER_MODULE],
    "job": [job, WORKER_JOB_MODULE],
}))
"""

_PROBE = (
    "import json, sys, orca_auto; "
    "print(json.dumps({'isolated': sys.flags.isolated, 'package': orca_auto.__file__}))"
)


@pytest.mark.parametrize(("isolated", "expected"), [(0, []), (1, ["-I"])])
def test_interpreter_argv_carries_isolated_mode_only(
    monkeypatch: pytest.MonkeyPatch, isolated: int, expected: list[str]
) -> None:
    # The other import flags are set here too: an environment variable can
    # turn them on, and the child inherits that environment anyway.
    flags = SimpleNamespace(
        isolated=isolated, ignore_environment=1, no_user_site=1, safe_path=1, dont_write_bytecode=1
    )
    monkeypatch.setattr(process_utils.sys, "flags", flags)

    assert process_utils.python_interpreter_argv() == [sys.executable, *expected]


def _decoy_package(root: Path) -> Path:
    """A harmless ``orca_auto`` that only reports where it was imported from."""
    package = root / "orca_auto"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# decoy: must never be imported\n", encoding="utf-8")
    return root


def _child_prefixes(parent_flags: list[str], *, cwd: Path) -> dict[str, list[str]]:
    """Interpreter part (before ``-m <module>``) of each child command."""
    result = subprocess.run(
        [sys.executable, *parent_flags, "-c", _PRINT_CHILD_COMMANDS],
        cwd=cwd,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        capture_output=True,
        text=True,
        check=True,
    )
    prefixes: dict[str, list[str]] = {}
    for name, (command, module) in json.loads(result.stdout).items():
        index = command.index("-m")
        assert command[index + 1] == module, name
        prefixes[name] = command[:index]
    return prefixes


def _probe(prefix: list[str], *, cwd: Path, pythonpath: Path) -> dict[str, object]:
    result = subprocess.run(
        [*prefix, "-c", _PROBE],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(pythonpath)},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def test_isolated_parent_starts_isolated_children_that_ignore_a_decoy(tmp_path: Path) -> None:
    decoy_path = _decoy_package(tmp_path / "decoy_path")
    decoy_cwd = _decoy_package(tmp_path / "decoy_cwd")

    prefixes = _child_prefixes(["-I"], cwd=tmp_path)

    assert set(prefixes) == {"worker", "job"}
    for name, prefix in prefixes.items():
        assert prefix == [sys.executable, "-I"], name
        # PYTHONPATH and the working directory both hold a decoy orca_auto.
        probe = _probe(prefix, cwd=decoy_cwd, pythonpath=decoy_path)
        assert probe["isolated"] == 1, name
        assert str(Path(str(probe["package"])).resolve()) == _REAL_PACKAGE, name


def test_non_isolated_parent_keeps_the_development_import_path(tmp_path: Path) -> None:
    decoy_path = _decoy_package(tmp_path / "decoy_path")

    prefixes = _child_prefixes([], cwd=tmp_path)

    for name, prefix in prefixes.items():
        assert prefix == [sys.executable], name
        # A development run still honors PYTHONPATH (``PYTHONPATH=src``).
        probe = _probe(prefix, cwd=tmp_path, pythonpath=decoy_path)
        assert probe["isolated"] == 0, name
        assert str(Path(str(probe["package"])).resolve()) == str(
            (decoy_path / "orca_auto" / "__init__.py").resolve()
        ), name
