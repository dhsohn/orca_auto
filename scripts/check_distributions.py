"""Build and exercise the ORCA_auto distribution without editable source fallback.

Run with ``python -m scripts.check_distributions`` from the repository root.
Builds and installed artifacts are retained in a new temporary directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import tomllib
import venv
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts.check_wheel_contents import check_wheel_contents
from scripts.prepare_runtime import prepare_runtime

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


def _copy_project(source: Path, destination: Path) -> None:
    destination.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in", "setup.cfg", "setup.py"):
        if (source / name).is_file():
            shutil.copy2(source / name, destination / name)
    shutil.copytree(
        source / "src",
        destination / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )


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
        "extras": list(metadata.get_all("Provides-Extra", [])),
    }


def _assert_distribution(wheel: Path, source: Path, *, expected_version: str) -> None:
    errors = check_wheel_contents(wheel, source)
    assert not errors, "\n".join(errors)
    metadata = _metadata(wheel)
    assert metadata["version"] == expected_version, "wheel version differs from source release"
    extras, requirements = metadata["extras"], metadata["requires"]
    assert isinstance(extras, list) and isinstance(requirements, list)
    assert "workflows" not in extras, "retired workflows extra is advertised"
    assert not any(
        canonicalize_name(Requirement(str(requirement)).name) == "orca-auto-workflows"
        for requirement in requirements
    ), "retired workflows dependency is advertised"


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


def _probe(
    python: Path,
    *,
    cwd: Path,
    core_source: Path,
    expected_version: str,
) -> None:
    code = """\
    import importlib.metadata
    import importlib.util
    import sys
    from pathlib import Path
    import orca_auto

    expected_core = Path(sys.argv[1]).resolve()
    expected_version = sys.argv[2]
    assert Path(orca_auto.__file__).resolve().parent == expected_core, orca_auto.__file__
    assert importlib.metadata.version("orca_auto") == expected_version, "core version does not match source release"
    assert importlib.util.find_spec("orca_auto.flow") is None
    try:
        importlib.metadata.version("orca_auto_workflows")
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError("retired workflows distribution is installed")
    """
    _run(
        [
            str(python),
            "-I",
            "-c",
            textwrap.dedent(code),
            str(core_source),
            expected_version,
        ],
        cwd=cwd,
    )
    _run([str(python), "-I", "-m", "orca_auto.cli", "--help"], cwd=cwd)
    version_output = _run([str(python.parent / "orca_auto"), "--version"], cwd=cwd)
    assert version_output.stdout == f"orca_auto {expected_version}\n", version_output.stdout


def _site_package(python: Path, *, cwd: Path) -> Path:
    result = _run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], cwd=cwd
    )
    return Path(result.stdout.strip()) / "orca_auto"


def _wheel_config_default(python: Path, home: Path, *, cwd: Path) -> None:
    code = """\
    import os
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from orca_auto.core.config.discovery import resolve_shared_config_path
    from orca_auto.orca.commands.init import _resolve_init_config_path

    home = Path(sys.argv[1])
    assert not home.exists()
    os.environ["HOME"] = str(home)
    expected = home / "orca_auto" / "config" / "orca_auto.yaml"
    assert _resolve_init_config_path(SimpleNamespace()) == expected
    assert resolve_shared_config_path(None) is None
    """
    _run(
        [str(python), "-I", "-c", textwrap.dedent(code), str(home)],
        cwd=cwd,
    )


def _prepared_runtime_smoke(core: Path, wheelhouse: Path, *, work: Path) -> None:
    yaml_wheels = [
        path for path in wheelhouse.glob("*.whl") if path.name.lower().startswith("pyyaml-")
    ]
    assert len(yaml_wheels) == 1
    wheels = [core, *yaml_wheels]
    root = prepare_runtime(
        wheels=wheels, releases_root=work / "releases", templates=REPO_ROOT / "systemd"
    )
    assert (
        prepare_runtime(
            wheels=wheels, releases_root=work / "releases", templates=REPO_ROOT / "systemd"
        )
        == root
    )
    python = root / ".venv/bin/python"
    package = _site_package(python, cwd=work)
    machine = _fake_orca(python, work / "prepared-runtime-worker", package=package)
    assert machine.is_file()
    config = work / "prepared-runtime-worker/orca_auto.yaml"
    result = _run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "orca_auto.cli",
            "queue",
            "worker",
            "--json",
            "--config",
            str(config),
        ],
        cwd=work,
    )
    assert json.loads(result.stdout)["workers"][0]["app"] == "orca"
    code = """\
    import sys
    from pathlib import Path
    from orca_auto.core.runtime_bundle import verify_runtime_bundle
    from orca_auto.systemd_plan import build_systemd_install_plan
    root, config = Path(sys.argv[1]), Path(sys.argv[2])
    build_id = verify_runtime_bundle(root)["build_id"]
    plan = build_systemd_install_plan(target_user="testuser", repo=root, config=config, no_enable=True, no_sudo=True)
    worker = next(unit.content for unit in plan.units if unit.name == "orca_auto-queue-worker@.service")
    assert build_id in worker and f"ReadOnlyPaths={root}" in worker
    """
    _run([str(python), "-I", "-B", "-c", textwrap.dedent(code), str(root), str(config)], cwd=work)
    _assert_runtime_writes_no_bytecode(root, work=work)
    print(
        "[distributions] prepared read-only runtime, idempotency, fake worker, service plan"
        " and bytecode passed",
        flush=True,
    )


def _tree_state(root: Path) -> dict[str, tuple[int, int, int, int]]:
    # Bytecode is replaced by rename, so the inode also changes when coarse
    # timestamps do not.
    state = {}
    for path in root.rglob("*"):
        info = path.lstat()
        state[path.relative_to(root).as_posix()] = (
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
        )
    return state


def _assert_runtime_writes_no_bytecode(root: Path, *, work: Path) -> None:
    # Root ignores the runtime's read-only modes, and -I ignores the
    # PYTHONDONTWRITEBYTECODE that _run exports. Running without -B in a writable
    # copy shows what such an interpreter would write into the prepared runtime.
    # Moving every source mtime forward rejects timestamp-validated bytecode,
    # which an interpreter rewrites after such a change.
    copy = work / "writable-runtime"
    shutil.copytree(root, copy, symlinks=True)
    for path in (copy, *copy.rglob("*")):
        if path.is_symlink():
            continue
        path.chmod(path.stat().st_mode | 0o200)
        if path.suffix == ".py":
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    before = _tree_state(copy)
    python = copy / ".venv/bin/python"
    code = """\
    import importlib, pkgutil, orca_auto, yaml
    for package in (orca_auto, yaml):
        for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            if not module.name.endswith(".__main__"):
                importlib.import_module(module.name)
    """
    _run([str(python), "-I", "-c", textwrap.dedent(code)], cwd=work)
    _run([str(python), "-I", "-m", "orca_auto.cli", "--version"], cwd=work)
    # ensurepip leaves timestamp bytecode for pip unless preparation recompiles it.
    _run([str(python), "-I", "-m", "pip", "--version"], cwd=work)
    after = _tree_state(copy)
    written = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )
    assert not written, f"prepared runtime interpreter wrote {len(written)} paths: {written[:5]}"


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
    from orca_auto.orca.queue.worker import OrcaQueueWorker
    worker = OrcaQueueWorker(load_config(sys.argv[1]), sys.argv[1], max_concurrent=1)
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


def run_matrix(work: Path) -> dict[str, object]:
    print(f"[distributions] retained workspace: {work}", flush=True)
    with (REPO_ROOT / "pyproject.toml").open("rb") as source:
        expected_version = str(tomllib.load(source)["project"]["version"])
    project = work / "core-source"
    _copy_project(REPO_ROOT, project)
    wheel, sdist = _build(project, work / "core-dist")
    assert sdist is not None
    _assert_core_sdist(sdist)
    unpacked = _unpack_sdist(sdist, work / "core-sdist")
    rebuilt, _ = _build(unpacked, work / "core-rebuilt", wheel_only=True)
    for candidate in (wheel, rebuilt):
        _assert_distribution(
            candidate, project / "src" / "orca_auto", expected_version=expected_version
        )
    wheelhouse = work / "wheelhouse"
    wheelhouse.mkdir()
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
    _pip(python, wheelhouse, str(wheel), cwd=work)
    package = _site_package(python, cwd=work)
    _probe(python, cwd=work, core_source=package, expected_version=expected_version)
    _wheel_config_default(python, work / "wheel-config-home", cwd=work)
    _prepared_runtime_smoke(wheel, wheelhouse, work=work)
    machine = _fake_orca(python, work / "fake-worker", package=package)
    # A second fresh environment proves the source archive independently
    # rebuilds an installable distribution without the original checkout.
    rebuilt_python = _new_environment(work / "rebuilt-installed", wheelhouse, cwd=work)
    _pip(rebuilt_python, wheelhouse, str(rebuilt), cwd=work)
    _probe(
        rebuilt_python,
        cwd=work,
        core_source=_site_package(rebuilt_python, cwd=work),
        expected_version=expected_version,
    )
    _run([str(python), "-m", "pip", "uninstall", "-y", "orca_auto"], cwd=work)
    _pip(python, wheelhouse, "-e", str(project), cwd=work)
    _probe(
        python,
        cwd=work,
        core_source=project / "src" / "orca_auto",
        expected_version=expected_version,
    )
    print("[distributions] wheel, rebuilt sdist and editable installation passed", flush=True)
    return {"work_dir": str(work), "core_wheel": str(wheel), "machine": str(machine)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir", type=Path, help="new or existing empty directory to retain artifacts"
    )
    args = parser.parse_args(argv)
    if args.work_dir is None:
        work = Path(tempfile.mkdtemp(prefix="orca-distributions-", dir="/tmp"))
    else:
        work = args.work_dir.resolve()
        if work.exists() and any(work.iterdir()):
            parser.error("--work-dir must be empty; existing artifacts will not be overwritten")
        work.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run_matrix(work), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
