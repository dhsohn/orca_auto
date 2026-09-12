from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import venv
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ISOLATED_BOOTSTRAP = """\
import importlib.util
import sys
from pathlib import Path

staged = Path(sys.argv[1]).resolve()
source = sys.argv[2]
sys.argv = ["core-only-acceptance", *sys.argv[3:]]
sys.path.insert(0, str(staged))
import orca_auto
assert Path(orca_auto.__file__).resolve().parent == staged / "orca_auto"
if not (staged / "orca_auto" / "flow").exists():
    assert importlib.util.find_spec("orca_auto.flow") is None
exec(compile(source, "<core-only-acceptance>", "exec"))
"""
_CLI_SOURCE = "from orca_auto.cli import main\nraise SystemExit(main(sys.argv[1:]))\n"


@dataclass(frozen=True)
class _CoreOnlyInstallation:
    python: Path
    imports: Path
    runtime: Path
    runs: Path
    admission: Path
    config: Path
    engine_counter: Path

    def run_code(self, source: str, *args: str) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("ORCA_AUTO_", "PYTHON"))
        }
        environment.update(
            {
                # Engine children do not inherit -I/-S, but must still import
                # this regular package, not the repository's editable install.
                "PYTHONPATH": str(self.imports),
                "PYTHONDONTWRITEBYTECODE": "1",
                "ORCA_AUTO_CONFIG": str(self.config),
                "NO_COLOR": "1",
            }
        )
        return subprocess.run(
            [
                str(self.python),
                "-I",
                "-S",
                "-B",
                "-c",
                _ISOLATED_BOOTSTRAP,
                str(self.imports),
                textwrap.dedent(source),
                *args,
            ],
            cwd=self.runtime,
            env=environment,
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_code(_CLI_SOURCE, *args)

    def snapshot(self, *, include_locks: bool = True) -> dict[str, bytes | None]:
        return {
            str(path.relative_to(self.runtime)): path.read_bytes() if path.is_file() else None
            for path in self.runtime.rglob("*")
            if include_locks or path.suffix != ".lock"
        }

    def write_orca_input(self, name: str = "public_h2") -> Path:
        directory = self.runs / name
        directory.mkdir()
        (directory / "h2.inp").write_text(
            "! HF STO-3G\n%pal nprocs 1 end\n%maxcore 128\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
            encoding="utf-8",
        )
        return directory


@pytest.fixture(scope="session")
def core_only_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    environment = tmp_path_factory.mktemp("core-only-interpreter")
    venv.EnvBuilder(with_pip=False).create(environment)
    return environment / "bin" / "python"


@pytest.fixture
def core_only(tmp_path: Path, core_only_python: Path) -> _CoreOnlyInstallation:
    imports = tmp_path / "imports"
    imports.mkdir()
    shutil.copytree(
        _REPO_ROOT / "src" / "orca_auto",
        imports / "orca_auto",
        ignore=shutil.ignore_patterns("flow", "__pycache__", "*.pyc"),
    )
    # Only the declared runtime dependency and distribution metadata accompany
    # the staged source. -I -S disables editable .pth files and site imports.
    shutil.copytree(
        Path(yaml.__file__).resolve().parent,
        imports / "yaml",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    installed = metadata.distribution("orca_auto")
    dist_info = imports / f"orca_auto-{installed.version}.dist-info"
    dist_info.mkdir()
    # Source-first hook paths may select egg-info (PKG-INFO), while an installed
    # wheel uses dist-info (METADATA). Copy either supported layout verbatim.
    metadata_text = installed.read_text("METADATA") or installed.read_text("PKG-INFO")
    assert metadata_text is not None
    (dist_info / "METADATA").write_text(metadata_text, encoding="utf-8")

    runtime = tmp_path / "runtime"
    runs = runtime / "runs"
    admission = runtime / "admission"
    runs.mkdir(parents=True)
    admission.mkdir()
    counter = runtime / "fake-engine-count"
    executable = runtime / "fake-orca"
    executable.write_text(
        textwrap.dedent(
            f"""\
            #!{core_only_python}
            import importlib.util
            from pathlib import Path
            import orca_auto

            assert Path(orca_auto.__file__).resolve().parent == Path({str(imports)!r}) / "orca_auto"
            assert importlib.util.find_spec("orca_auto.flow") is None
            counter = Path({str(counter)!r})
            counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")
            print("Program Version 6.0.1 - RELEASE -")
            print("CARTESIAN COORDINATES (ANGSTROEM)")
            print("---------------------------------")
            print(" H 0.000000 0.000000 0.000000")
            print(" H 0.000000 0.000000 0.740000")
            print("")
            print("FINAL SINGLE POINT ENERGY -1.100000000000")
            print("TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds")
            print("****ORCA TERMINATED NORMALLY****")
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    config = runtime / "orca_auto.yaml"
    config.write_text(
        json.dumps(
            {
                "runs_root": str(runs),
                "scheduler": {
                    "admission_root": str(admission),
                    "max_active_simulations": 1,
                },
                "resources": {"max_cores_per_task": 1, "max_memory_gb_per_task": 1},
                "orca": {"paths": {"orca_executable": str(executable)}},
                "messenger": {},
            }
        ),
        encoding="utf-8",
    )
    return _CoreOnlyInstallation(
        core_only_python, imports, runtime, runs, admission, config, counter
    )


def _assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (result.stdout, result.stderr)


def _assert_workflows_unavailable(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode != 0, (result.stdout, result.stderr)
    output = result.stdout + result.stderr
    assert "workflows extension is not installed" in output
    assert "matching workflow-enabled ORCA_auto installation" in output
    assert "Traceback" not in output


@pytest.mark.parametrize("option", ["--help", "--version"])
def test_core_help_and_version_without_workflow_files(
    core_only: _CoreOnlyInstallation, option: str
) -> None:
    before = core_only.snapshot()
    result = core_only.cli(option)
    _assert_success(result)
    assert "orca_auto" in result.stdout
    if option == "--version":
        assert metadata.version("orca_auto") in result.stdout
    assert core_only.snapshot() == before


def test_core_empty_queue_json_and_text_are_read_only(core_only: _CoreOnlyInstallation) -> None:
    # Read locks retain acquisition diagnostics; queue/index data must not change.
    before = core_only.snapshot(include_locks=False)
    result = core_only.cli("queue", "list", "--json")
    _assert_success(result)
    payload = json.loads(result.stdout)
    assert payload["activities"] == []
    assert payload["active_simulations"] == 0
    text = core_only.cli("queue", "list")
    _assert_success(text)
    assert "Traceback" not in text.stderr
    assert core_only.snapshot(include_locks=False) == before


def test_core_orca_submission_listing_and_cancellation(core_only: _CoreOnlyInstallation) -> None:
    input_dir = core_only.write_orca_input()
    _assert_success(core_only.cli("run-dir", str(input_dir), "--json"))
    before_listing = core_only.snapshot(include_locks=False)
    listed = core_only.cli("queue", "list", "--json")
    _assert_success(listed)
    activities = json.loads(listed.stdout)["activities"]
    assert len(activities) == 1
    assert activities[0]["engine"] == "orca"
    assert activities[0]["status"] == "pending"
    assert core_only.snapshot(include_locks=False) == before_listing
    _assert_success(core_only.cli("queue", "cancel", str(input_dir), "--json"))
    cancelled = core_only.cli("queue", "list", "--json")
    _assert_success(cancelled)
    assert json.loads(cancelled.stdout)["activities"][0]["status"] == "cancelled"
    assert not core_only.engine_counter.exists()
    preserved_input = (input_dir / "h2.inp").read_bytes()
    pending_replay = core_only.cli("queue", "list", "clear", "--json")
    _assert_success(pending_replay)
    assert json.loads(pending_replay.stdout)["cleared"]["orca_queue_entries"] == 0
    cleared = core_only.cli("queue", "list", "--json")
    _assert_success(cleared)
    assert json.loads(cleared.stdout)["activities"][0]["status"] == "cancelled"
    assert (input_dir / "h2.inp").read_bytes() == preserved_input


def test_core_default_worker_plan_is_orca_only(core_only: _CoreOnlyInstallation) -> None:
    before = core_only.snapshot()
    result = core_only.cli("queue", "worker", "--json")
    _assert_success(result)
    workers = json.loads(result.stdout)["workers"]
    assert [worker["app"] for worker in workers] == ["orca"]
    assert workers[0]["argv"][-2:] == ["--engine", "orca"]
    assert core_only.snapshot() == before


def test_core_worker_runs_fake_orca_child_without_workflow_files(
    core_only: _CoreOnlyInstallation,
) -> None:
    input_dir = core_only.write_orca_input()
    _assert_success(core_only.cli("run-dir", str(input_dir), "--json"))
    result = core_only.run_code(
        """\
        from orca_auto.core.admission import list_slots
        from orca_auto.orca.config import load_config
        from orca_auto.orca.queue.adapter import list_queue
        from orca_auto.orca.queue.worker import QueueWorker

        cfg = load_config(sys.argv[1])
        worker = QueueWorker(cfg, sys.argv[1], max_concurrent=1)
        worker.poll_interval_seconds = 0.01
        assert worker.run_once(idle_message=None, blocked_message=None) == 0
        entries = list_queue(sys.argv[2])
        assert len(entries) == 1
        assert entries[0].status == "completed", entries[0]
        assert list_slots(sys.argv[3]) == []
        """,
        str(core_only.config),
        str(core_only.runs),
        str(core_only.admission),
    )
    _assert_success(result)
    assert core_only.engine_counter.read_text(encoding="utf-8") == "1"
    machines = list(input_dir.rglob("machine.json"))
    assert len(machines) == 1
    machine = json.loads(machines[0].read_text(encoding="utf-8"))
    assert machine["producer"]["name"] == "orca_auto"
    assert machine["payload"]["contract"] == {"name": "chemistry/results-bundle", "version": 1}
    machine_bytes = machines[0].read_bytes()
    cleared = core_only.cli("queue", "list", "clear", "--json")
    _assert_success(cleared)
    assert json.loads(cleared.stdout)["cleared"]["orca_queue_entries"] == 1
    listed = core_only.cli("queue", "list", "--json")
    _assert_success(listed)
    assert json.loads(listed.stdout)["activities"] == []
    assert machines[0].read_bytes() == machine_bytes


@pytest.mark.parametrize("operation", ["scaffold", "worker", "run-dir", "mixed-run-dir"])
def test_explicit_workflows_refuse_before_runtime_mutation(
    core_only: _CoreOnlyInstallation, operation: str
) -> None:
    target = core_only.runs / "requested_workflow"
    command: tuple[str, ...]
    if operation in {"run-dir", "mixed-run-dir"}:
        target.mkdir()
        (target / "flow.yaml").write_text("workflow_type: conformer_screening\n", encoding="utf-8")
        if operation == "mixed-run-dir":
            (target / "plausible.inp").write_text(
                "! HF STO-3G\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8"
            )
        command = ("run-dir", str(target), "--json")
    elif operation == "scaffold":
        command = ("scaffold", "scan-ts", str(target))
    else:
        command = ("queue", "worker", "--app", "workflow", "--json")
    before = core_only.snapshot()
    _assert_workflows_unavailable(core_only.cli(*command))
    assert core_only.snapshot() == before


@pytest.mark.parametrize(
    "selection",
    [
        ("--engine", "workflow"),
        ("--engine", "xtb"),
        ("--engine", "crest"),
        ("--kind", "workflow"),
        ("--refresh",),
    ],
)
def test_explicit_workflow_queue_filters_refuse_without_mutation(
    core_only: _CoreOnlyInstallation, selection: tuple[str, ...]
) -> None:
    before = core_only.snapshot()
    _assert_workflows_unavailable(core_only.cli("queue", "list", *selection, "--json"))
    assert core_only.snapshot() == before


@pytest.mark.parametrize("evidence", ["registry", "workspace"])
@pytest.mark.parametrize("operation", ["list", "clear", "cancel"])
def test_retained_workflow_state_cannot_be_hidden_or_mutated_without_extension(
    core_only: _CoreOnlyInstallation, evidence: str, operation: str
) -> None:
    input_dir = core_only.write_orca_input()
    _assert_success(core_only.cli("run-dir", str(input_dir), "--json"))
    if evidence == "registry":
        (core_only.runs / "workflow_registry.json").write_text(
            '{"version": 1, "workflows": {"retained": {"status": "running"}}}\n',
            encoding="utf-8",
        )
    else:
        retained = core_only.runs / "retained"
        retained.mkdir()
        (retained / "workflow.json").write_text(
            '{"workflow_id": "retained", "status": "running"}\n', encoding="utf-8"
        )
    command = {
        "list": ("queue", "list", "--json"),
        "clear": ("queue", "list", "clear", "--json"),
        "cancel": ("queue", "cancel", str(input_dir), "--json"),
    }[operation]
    before = core_only.snapshot()
    result = core_only.cli(*command)
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "Workflow state exists" in result.stderr
    assert "restore the matching ORCA_auto workflows extension" in result.stderr
    assert "Traceback" not in result.stderr
    assert core_only.snapshot() == before


@pytest.mark.parametrize("failure", ["RuntimeError", "ModuleNotFoundError"])
def test_broken_installed_workflows_are_not_treated_as_absent(
    core_only: _CoreOnlyInstallation, failure: str
) -> None:
    flow = core_only.imports / "orca_auto" / "flow"
    flow.mkdir()
    (flow / "__init__.py").write_text(
        f"raise {failure}('installed workflow is broken')\n", encoding="utf-8"
    )
    result = core_only.run_code(
        """\
        from orca_auto.core.extensions import require_workflows, workflows_available
        assert workflows_available() is True
        require_workflows()
        """
    )
    assert result.returncode != 0
    assert f"{failure}: installed workflow is broken" in result.stderr
    assert "workflows extension is not installed" not in result.stderr
