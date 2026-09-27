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
import sys
from pathlib import Path

staged = Path(sys.argv[1]).resolve()
source = sys.argv[2]
sys.argv = ["isolated-acceptance", *sys.argv[3:]]
sys.path.insert(0, str(staged))
import orca_auto
assert Path(orca_auto.__file__).resolve().parent == staged / "orca_auto"
exec(compile(source, "<isolated-acceptance>", "exec"))
"""
_CLI_SOURCE = "from orca_auto.cli import main\nraise SystemExit(main(sys.argv[1:]))\n"


@dataclass(frozen=True)
class _IsolatedInstallation:
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

    def write_orca_input(self, name: str = "public_h2") -> Path:
        directory = self.runs / name
        directory.mkdir()
        (directory / "h2.inp").write_text(
            "! HF STO-3G\n%pal nprocs 1 end\n%maxcore 128\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
            encoding="utf-8",
        )
        return directory


@pytest.fixture(scope="session")
def isolated_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    environment = tmp_path_factory.mktemp("isolated-interpreter")
    venv.EnvBuilder(with_pip=False).create(environment)
    return environment / "bin" / "python"


@pytest.fixture
def isolated(tmp_path: Path, isolated_python: Path) -> _IsolatedInstallation:
    imports = tmp_path / "imports"
    imports.mkdir()
    shutil.copytree(
        _REPO_ROOT / "src" / "orca_auto",
        imports / "orca_auto",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
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
            #!{isolated_python}
            from pathlib import Path
            import orca_auto

            assert Path(orca_auto.__file__).resolve().parent == Path({str(imports)!r}) / "orca_auto"
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
    # Observe the executed gate directly, independently of startup hooks.
    gate = imports / "orca_auto" / "orca" / "launch_gate.py"
    entrypoint = 'if __name__ == "__main__":\n'
    source = gate.read_text(encoding="utf-8")
    assert source.count(entrypoint) == 1
    prefix_record = runtime / "launch-python-prefix"
    gate.write_text(
        source.replace(
            entrypoint,
            entrypoint
            + f"    with open({str(prefix_record)!r}, 'w') as prefix_probe:\n"
            + "        prefix_probe.write(sys.prefix)\n",
        ),
        encoding="utf-8",
    )
    return _IsolatedInstallation(
        isolated_python, imports, runtime, runs, admission, config, counter
    )


def _assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.mark.slow
def test_worker_runs_fake_orca_child_from_an_isolated_installation(
    isolated: _IsolatedInstallation,
) -> None:
    input_dir = isolated.write_orca_input()
    _assert_success(isolated.cli("run-dir", str(input_dir), "--json"))
    result = isolated.run_code(
        """\
        from orca_auto.core.admission import list_slots
        from orca_auto.orca.config import load_config
        from orca_auto.orca.queue.adapter import list_queue
        from orca_auto.orca.queue.worker import OrcaQueueWorker

        cfg = load_config(sys.argv[1])
        worker = OrcaQueueWorker(cfg, sys.argv[1], max_concurrent=1)
        worker.poll_interval_seconds = 0.01
        assert worker.run_once(idle_message=None, blocked_message=None) == 0
        entries = list_queue(sys.argv[2])
        assert len(entries) == 1
        assert entries[0].status == "completed", entries[0]
        assert list_slots(sys.argv[3]) == []
        """,
        str(isolated.config),
        str(isolated.runs),
        str(isolated.admission),
    )
    _assert_success(result)
    assert isolated.engine_counter.read_text(encoding="utf-8") == "1"
    assert (isolated.runtime / "launch-python-prefix").read_text() == str(
        isolated.python.parent.parent
    )
    machines = list(input_dir.rglob("machine.json"))
    assert len(machines) == 1
    machine = json.loads(machines[0].read_text(encoding="utf-8"))
    assert machine["producer"]["name"] == "orca_auto"
    assert machine["payload"]["contract"] == {"name": "chemistry/results-bundle", "version": 1}
    machine_bytes = machines[0].read_bytes()
    cleared = isolated.cli("queue", "list", "clear", "--json")
    _assert_success(cleared)
    assert json.loads(cleared.stdout)["cleared"]["orca_queue_entries"] == 1
    listed = isolated.cli("queue", "list", "--json")
    _assert_success(listed)
    assert json.loads(listed.stdout)["activities"] == []
    assert machines[0].read_bytes() == machine_bytes
