"""Build and exercise core/workflows distributions without editable source fallback.

Run with ``python -m scripts.check_distributions`` from the repository root.
Builds and installed artifacts are retained in a new temporary directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import venv
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from packaging.requirements import Requirement

from scripts.check_wheel_contents import check_disjoint_ownership, check_wheel_contents

REPO_ROOT = Path(__file__).resolve().parents[1]


def _environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "ORCA_AUTO_", "PIP_"))
    }
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1"})
    return environment


def _run(argv: list[str], *, cwd: Path, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed {argv!r}\n{result.stdout[-8000:]}\n{result.stderr[-8000:]}"
        )
    return result


def _copy_project(source: Path, destination: Path, *, include_workflows: bool = False) -> None:
    destination.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in", "setup.cfg", "setup.py"):
        if (source / name).is_file():
            shutil.copy2(source / name, destination / name)
    shutil.copytree(
        source / "src",
        destination / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    if include_workflows:
        # The initial core build must see the actual monorepo layout: hiding
        # nested extension sources would conceal overly broad package discovery
        # and a broken source-distribution manifest.
        (destination / "extensions").mkdir()
        _copy_project(source / "extensions" / "workflows", destination / "extensions" / "workflows")


def _assert_core_sdist(archive: Path) -> None:
    with tarfile.open(archive) as source:
        bundled_workflows = [
            member.name
            for member in source
            if PurePosixPath(member.name).parts[1:2] == ("extensions",)
            or PurePosixPath(member.name).parts[1:4] == ("src", "orca_auto", "flow")
        ]
    assert not bundled_workflows, "core sdist includes workflows source: " + ", ".join(
        bundled_workflows
    )


def _build(project: Path, output: Path, *, wheel_only: bool = False) -> tuple[Path, Path | None]:
    output.mkdir()
    flags = ["--wheel"] if wheel_only else ["--wheel", "--sdist"]
    _run(
        [sys.executable, "-m", "build", "--no-isolation", *flags, "--outdir", str(output)],
        cwd=project,
    )
    wheels = list(output.glob("*.whl"))
    sdists = list(output.glob("*.tar.gz"))
    assert len(wheels) == 1, wheels
    assert len(sdists) == (0 if wheel_only else 1), sdists
    return wheels[0], sdists[0] if sdists else None


def _unpack_sdist(archive: Path, destination: Path) -> Path:
    destination.mkdir()
    roots: set[str] = set()
    with tarfile.open(archive) as source:
        for member in source:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or "\\" in member.name or not name.parts:
                raise ValueError(f"unsafe sdist path: {member.name}")
            roots.add(name.parts[0])
            target = destination.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                contents = source.extractfile(member)
                assert contents is not None
                with contents, target.open("wb") as output:
                    shutil.copyfileobj(contents, output)
            else:
                raise ValueError(f"sdist contains unsupported non-regular entry: {member.name}")
    assert len(roots) == 1, roots
    return destination / roots.pop()


def _metadata(wheel: Path) -> dict[str, object]:
    with ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        assert len(names) == 1
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    return {
        "name": str(metadata["Name"]),
        "version": str(metadata["Version"]),
        "requires": list(metadata.get_all("Requires-Dist", [])),
    }


def _assert_pair(core: Path, workflows: Path, core_source: Path, workflow_source: Path) -> None:
    errors = check_wheel_contents(core, core_source)
    errors += check_wheel_contents(
        workflows, workflow_source, package="orca_auto/flow", distribution="orca_auto_workflows"
    )
    errors += check_disjoint_ownership(core, workflows)
    assert not errors, "\n".join(errors)
    core_metadata, workflow_metadata = _metadata(core), _metadata(workflows)
    assert core_metadata["version"] == workflow_metadata["version"]
    requirements = workflow_metadata["requires"]
    assert isinstance(requirements, list)
    normalized_requires = {
        str(requirement).replace("-", "_").replace(" ", "") for requirement in requirements
    }
    assert f"orca_auto=={core_metadata['version']}" in normalized_requires, workflow_metadata
    core_requirements = core_metadata["requires"]
    assert isinstance(core_requirements, list)
    extras = [
        Requirement(str(value))
        for value in core_requirements
        if Requirement(str(value)).name.replace("-", "_") == "orca_auto_workflows"
    ]
    assert len(extras) == 1, core_metadata
    assert str(extras[0].specifier) == f"=={workflow_metadata['version']}", core_metadata
    assert extras[0].marker is not None
    assert extras[0].marker.evaluate({"extra": "workflows"})
    assert not extras[0].marker.evaluate({"extra": ""})


def _new_environment(path: Path, wheelhouse: Path, *, cwd: Path) -> Path:
    venv.EnvBuilder(with_pip=True).create(path)
    python = path / "bin" / "python"
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "PyYAML",
            "setuptools",
            "wheel",
        ],
        cwd=cwd,
    )
    return python


def _pip(python: Path, wheelhouse: Path, *requirements: str, cwd: Path) -> None:
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-build-isolation",
            "--find-links",
            str(wheelhouse),
            *requirements,
        ],
        cwd=cwd,
    )
    _run([str(python), "-m", "pip", "check"], cwd=cwd)


def _probe(python: Path, *, cwd: Path, core_source: Path, flow_source: Path | None) -> None:
    code = """\
    import importlib.metadata
    import importlib.util
    import sys
    from pathlib import Path
    import orca_auto

    expected_core = Path(sys.argv[1]).resolve()
    assert Path(orca_auto.__file__).resolve().parent == expected_core, orca_auto.__file__
    if sys.argv[2]:
        import orca_auto.flow
        assert Path(orca_auto.flow.__file__).resolve().parent == Path(sys.argv[2]).resolve()
        assert importlib.metadata.version("orca_auto") == importlib.metadata.version("orca_auto_workflows")
    else:
        assert importlib.util.find_spec("orca_auto.flow") is None
        try:
            importlib.metadata.version("orca_auto_workflows")
        except importlib.metadata.PackageNotFoundError:
            pass
        else:
            raise AssertionError("uninstalled workflows metadata survived")
    """
    _run(
        [str(python), "-I", "-c", textwrap.dedent(code), str(core_source), str(flow_source or "")],
        cwd=cwd,
    )
    _run([str(python), "-I", "-m", "orca_auto.cli", "--help"], cwd=cwd)
    _run([str(python.parent / "orca_auto"), "--version"], cwd=cwd)


def _site_package(python: Path, *, cwd: Path) -> Path:
    result = _run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], cwd=cwd
    )
    return Path(result.stdout.strip()) / "orca_auto"


def _core_fingerprints(package: Path) -> dict[str, str]:
    return {
        str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in package.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and "flow" not in path.relative_to(package).parts
    }


def _scaffold(python: Path, root: Path, *, cwd: Path) -> None:
    _run(
        [str(python), "-I", "-m", "orca_auto.cli", "scaffold", "conformer_search", str(root)],
        cwd=cwd,
    )
    assert (root / "flow.yaml").is_file()


def _refuse_scaffold(python: Path, root: Path, *, cwd: Path, reason: str) -> None:
    assert not root.exists()
    result = subprocess.run(
        [str(python), "-I", "-m", "orca_auto.cli", "scaffold", "conformer_search", str(root)],
        cwd=cwd,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0, result.stdout
    assert reason in result.stderr + result.stdout, (result.stdout, result.stderr)
    assert not root.exists()


def _mismatched_version(python: Path, package: Path, *, cwd: Path) -> None:
    metadata_files = list(package.parent.glob("orca_auto_workflows-*.dist-info/METADATA"))
    assert len(metadata_files) == 1
    path = metadata_files[0]
    original = path.read_bytes()
    lines = original.decode("utf-8").splitlines(keepends=True)
    assert sum(line.startswith("Version: ") for line in lines) == 1
    changed = "".join(
        "Version: 0.0.0\n" if line.startswith("Version: ") else line for line in lines
    )
    # Deliberate corruption of only disposable installation metadata exercises
    # the runtime guard separately from the wheel's exact Requires-Dist pin.
    try:
        path.write_text(changed, encoding="utf-8")
        _refuse_scaffold(python, cwd / "mismatched-scaffold", cwd=cwd, reason="version mismatch")
    finally:
        path.write_bytes(original)
    _scaffold(python, cwd / "restored-version-scaffold", cwd=cwd)


def _fake_orca(python: Path, root: Path, *, package: Path) -> Path:
    root.mkdir()
    runs = root / "runs"
    job = runs / "public_h2"
    job.mkdir(parents=True)
    admission = root / "admission"
    admission.mkdir()
    counter = root / "engine-count"
    engine = root / "fake-orca"
    engine.write_text(
        f"#!{python}\n"
        "from pathlib import Path\nimport importlib.util\nimport orca_auto\n"
        f"assert Path(orca_auto.__file__).resolve().parent == Path({str(package)!r})\n"
        "assert importlib.util.find_spec('orca_auto.flow') is None\n"
        f"Path({str(counter)!r}).write_text('1')\n"
        "print('Program Version 6.0.1 - RELEASE -')\n"
        "print('CARTESIAN COORDINATES (ANGSTROEM)')\n"
        "print('---------------------------------')\n"
        "print(' H 0.000000 0.000000 0.000000')\n"
        "print(' H 0.000000 0.000000 0.740000')\nprint('')\n"
        "print('FINAL SINGLE POINT ENERGY -1.100000000000')\n"
        "print('****ORCA TERMINATED NORMALLY****')\n",
        encoding="utf-8",
    )
    engine.chmod(0o755)
    (job / "h2.inp").write_text(
        "! HF STO-3G\n%pal nprocs 1 end\n%maxcore 128\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )
    config = root / "orca_auto.yaml"
    config.write_text(
        json.dumps(
            {
                "runs_root": str(runs),
                "scheduler": {"admission_root": str(admission), "max_active_simulations": 1},
                "resources": {"max_cores_per_task": 1, "max_memory_gb_per_task": 1},
                "orca": {"paths": {"orca_executable": str(engine)}},
                "messenger": {},
            }
        ),
        encoding="utf-8",
    )
    _run(
        [str(python), "-I", "-m", "orca_auto.cli", "run-dir", str(job), "--config", str(config)],
        cwd=root,
    )
    code = """\
    import sys
    from orca_auto.core.admission import list_slots
    from orca_auto.orca.config import load_config
    from orca_auto.orca.queue.adapter import list_queue
    from orca_auto.orca.queue.worker import QueueWorker
    worker = QueueWorker(load_config(sys.argv[1]), sys.argv[1], max_concurrent=1)
    worker.poll_interval_seconds = 0.01
    assert worker.run_once(idle_message=None, blocked_message=None) == 0
    entries = list_queue(sys.argv[2])
    assert len(entries) == 1 and entries[0].status == "completed", entries
    assert list_slots(sys.argv[3]) == []
    """
    _run(
        [str(python), "-I", "-c", textwrap.dedent(code), str(config), str(runs), str(admission)],
        cwd=root,
    )
    assert counter.read_text(encoding="utf-8") == "1"
    machines = list(job.rglob("machine.json"))
    assert len(machines) == 1
    command = [str(python), "-I", "-m", "orca_auto.cli", "queue", "list"]
    listed = _run([*command, "--config", str(config), "--json"], cwd=root)
    activities = json.loads(listed.stdout)["activities"]
    assert len(activities) == 1 and activities[0]["status"] == "completed"
    _run([*command, "clear", "--config", str(config), "--json"], cwd=root)
    listed = _run([*command, "--config", str(config), "--json"], cwd=root)
    assert json.loads(listed.stdout)["activities"] == []
    return machines[0]


def _monolith_upgrade(
    python: Path,
    wheelhouse: Path,
    core_project: Path,
    flow_project: Path,
    core: Path,
    flow: Path,
    work: Path,
) -> None:
    monolith = work / "synthetic-monolith-source"
    _copy_project(core_project, monolith)
    shutil.copytree(
        flow_project / "src" / "orca_auto" / "flow", monolith / "src" / "orca_auto" / "flow"
    )
    # A synthetic old ownership layout, not a claim to execute historical 4.1
    # runtime code: one RECORD owns core+flow, including one retired sentinel.
    sentinel = "_monolith_only_fixture.py"
    (monolith / "src" / "orca_auto" / sentinel).write_text("", encoding="utf-8")
    (monolith / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n'
        '[project]\nname = "orca_auto"\nversion = "4.1.0"\ndependencies = ["PyYAML>=6"]\n'
        '[project.scripts]\norca_auto = "orca_auto.cli:main"\n'
        '[tool.setuptools.packages.find]\nwhere = ["src"]\ninclude = ["orca_auto*"]\n',
        encoding="utf-8",
    )
    old_wheel, _ = _build(monolith, work / "synthetic-monolith-dist", wheel_only=True)
    _run(
        [str(python), "-m", "pip", "uninstall", "-y", "orca_auto", "orca_auto_workflows"], cwd=work
    )
    _pip(python, wheelhouse, str(old_wheel), cwd=work)
    package = _site_package(python, cwd=work)
    assert (package / sentinel).is_file() and (package / "flow" / "__init__.py").is_file()
    _pip(python, wheelhouse, str(core), str(flow), cwd=work)
    assert not (package / sentinel).exists()
    _probe(python, cwd=work, core_source=package, flow_source=package / "flow")
    _scaffold(python, work / "upgraded-monolith-scaffold", cwd=work)


def run_matrix(work: Path) -> dict[str, object]:
    print(f"[distributions] retained workspace: {work}", flush=True)
    core_project, flow_project = work / "core-source", work / "workflows-source"
    _copy_project(REPO_ROOT, core_project, include_workflows=True)
    _copy_project(REPO_ROOT / "extensions" / "workflows", flow_project)
    core, core_sdist = _build(core_project, work / "core-dist")
    flow, flow_sdist = _build(flow_project, work / "workflows-dist")
    assert core_sdist is not None and flow_sdist is not None
    _assert_core_sdist(core_sdist)
    _assert_pair(
        core, flow, core_project / "src" / "orca_auto", flow_project / "src" / "orca_auto" / "flow"
    )
    core_unpacked = _unpack_sdist(core_sdist, work / "core-sdist")
    flow_unpacked = _unpack_sdist(flow_sdist, work / "workflows-sdist")
    core_rebuilt, _ = _build(core_unpacked, work / "core-rebuilt", wheel_only=True)
    flow_rebuilt, _ = _build(flow_unpacked, work / "workflows-rebuilt", wheel_only=True)
    _assert_pair(
        core_rebuilt,
        flow_rebuilt,
        core_project / "src" / "orca_auto",
        flow_project / "src" / "orca_auto" / "flow",
    )
    wheelhouse = work / "wheelhouse"
    wheelhouse.mkdir()
    for wheel in (core, flow):
        shutil.copy2(wheel, wheelhouse / wheel.name)
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            str(wheelhouse),
            "PyYAML>=6.0.1",
            "setuptools>=68",
            "wheel",
        ],
        cwd=work,
    )
    python = _new_environment(work / "installed", wheelhouse, cwd=work)
    _pip(python, wheelhouse, str(core), cwd=work)
    package = _site_package(python, cwd=work)
    _probe(python, cwd=work, core_source=package, flow_source=None)
    _refuse_scaffold(
        python, work / "core-only-scaffold", cwd=work, reason="workflows extension is not installed"
    )
    machine = _fake_orca(python, work / "fake-worker", package=package)
    print("[distributions] core-only installed fake worker passed", flush=True)
    fingerprints = _core_fingerprints(package)
    _pip(python, wheelhouse, str(flow), cwd=work)
    _probe(python, cwd=work, core_source=package, flow_source=package / "flow")
    _scaffold(python, work / "wheel-scaffold", cwd=work)
    worker_plan = _run(
        [
            str(python),
            "-I",
            "-m",
            "orca_auto.cli",
            "queue",
            "worker",
            "--app",
            "workflow",
            "--json",
            "--config",
            str(work / "fake-worker" / "orca_auto.yaml"),
        ],
        cwd=work,
    )
    assert {worker["app"] for worker in json.loads(worker_plan.stdout)["workers"]} == {
        "workflow",
        "xtb",
        "crest",
    }
    assert _core_fingerprints(package) == fingerprints
    _run([str(python), "-m", "pip", "uninstall", "-y", "orca_auto_workflows"], cwd=work)
    assert _core_fingerprints(package) == fingerprints
    _probe(python, cwd=work, core_source=package, flow_source=None)
    _refuse_scaffold(
        python,
        work / "uninstalled-scaffold",
        cwd=work,
        reason="workflows extension is not installed",
    )
    _pip(python, wheelhouse, str(flow), cwd=work)
    _scaffold(python, work / "reinstalled-scaffold", cwd=work)
    print("[distributions] wheel add/remove/reinstall passed", flush=True)
    # The second clean environment proves the source archives independently
    # rebuild installable distributions, with no access to a sibling project.
    rebuilt_python = _new_environment(work / "rebuilt-installed", wheelhouse, cwd=work)
    _pip(rebuilt_python, wheelhouse, str(core_rebuilt), str(flow_rebuilt), cwd=work)
    rebuilt_package = _site_package(rebuilt_python, cwd=work)
    _probe(
        rebuilt_python, cwd=work, core_source=rebuilt_package, flow_source=rebuilt_package / "flow"
    )
    _scaffold(rebuilt_python, work / "sdist-scaffold", cwd=work)
    _mismatched_version(rebuilt_python, rebuilt_package, cwd=work)
    _monolith_upgrade(rebuilt_python, wheelhouse, core_project, flow_project, core, flow, work)
    # Mixed editable/wheel installs exercise pkgutil namespace path composition
    # in both directions; each transition runs a fresh interpreter.
    _run(
        [str(python), "-m", "pip", "uninstall", "-y", "orca_auto", "orca_auto_workflows"], cwd=work
    )
    _pip(python, wheelhouse, "-e", str(core_project), str(flow), cwd=work)
    _probe(
        python,
        cwd=work,
        core_source=core_project / "src" / "orca_auto",
        flow_source=package / "flow",
    )
    _scaffold(python, work / "editable-core-scaffold", cwd=work)
    _run(
        [str(python), "-m", "pip", "uninstall", "-y", "orca_auto", "orca_auto_workflows"], cwd=work
    )
    _pip(python, wheelhouse, str(core), "-e", str(flow_project), cwd=work)
    _probe(
        python,
        cwd=work,
        core_source=package,
        flow_source=flow_project / "src" / "orca_auto" / "flow",
    )
    _scaffold(python, work / "editable-workflows-scaffold", cwd=work)
    _run([str(python), "-m", "pip", "uninstall", "-y", "orca_auto"], cwd=work)
    _pip(python, wheelhouse, "-e", str(core_project), cwd=work)
    _probe(
        python,
        cwd=work,
        core_source=core_project / "src" / "orca_auto",
        flow_source=flow_project / "src" / "orca_auto" / "flow",
    )
    _scaffold(python, work / "both-editable-scaffold", cwd=work)
    _run([str(python), "-m", "pip", "uninstall", "-y", "orca_auto_workflows"], cwd=work)
    _probe(python, cwd=work, core_source=core_project / "src" / "orca_auto", flow_source=None)
    _refuse_scaffold(
        python,
        work / "editable-core-only-scaffold",
        cwd=work,
        reason="workflows extension is not installed",
    )
    print("[distributions] independent sdists and mixed editable installs passed", flush=True)
    return {
        "work_dir": str(work),
        "core_wheel": str(core),
        "workflows_wheel": str(flow),
        "machine": str(machine),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir", type=Path, help="new or existing empty directory to retain artifacts"
    )
    args = parser.parse_args(argv)
    if args.work_dir is None:
        work = Path(tempfile.mkdtemp(prefix="orca-workflow-boundary-distributions-", dir="/tmp"))
    else:
        work = args.work_dir.resolve()
        if work.exists() and any(work.iterdir()):
            parser.error("--work-dir must be empty; existing artifacts will not be overwritten")
        work.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run_matrix(work), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
