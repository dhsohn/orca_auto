from __future__ import annotations

import base64
import csv
import hashlib
import io
import subprocess
import sys
import tarfile
import venv
from importlib import metadata, util
from pathlib import Path
from types import ModuleType
from typing import Any
from zipfile import ZipFile

import pytest

from scripts.check_distributions import (
    _assert_core_sdist,
    _assert_distribution,
    _assert_runtime_writes_no_bytecode,
    _probe,
    _unpack_sdist,
)
from scripts.check_wheel_contents import check_wheel_contents
from tests.test_runtime_bundle import _unseal


def _wheel(
    path: Path,
    payload: dict[str, bytes],
    *,
    name: str = "orca_auto",
    version: str = "5.0.0.dev0",
    requires: tuple[str, ...] = (),
    extras: tuple[str, ...] = (),
) -> Path:
    metadata = f"{name}-{version}.dist-info"
    files = {
        **payload,
        f"{metadata}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {value}\n" for value in requires)
            + "".join(f"Provides-Extra: {value}\n" for value in extras)
        ).encode(),
        f"{metadata}/WHEEL": b"Wheel-Version: 1.0\nTag: py3-none-any\n",
    }
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    for filename, contents in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(contents).digest()).rstrip(b"=")
        writer.writerow([filename, "sha256=" + digest.decode(), str(len(contents))])
    writer.writerow([f"{metadata}/RECORD", "", ""])
    files[f"{metadata}/RECORD"] = output.getvalue().encode()
    with ZipFile(path, "w") as archive:
        for filename, contents in files.items():
            archive.writestr(filename, contents)
    return path


def _source(root: Path, payload: dict[str, bytes], prefix: str) -> Path:
    root.mkdir()
    for name, contents in payload.items():
        relative = name.removeprefix(prefix + "/")
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    return root


def test_distribution_owns_only_its_source_payload(tmp_path: Path) -> None:
    payload = {"orca_auto/__init__.py": b"", "orca_auto/py.typed": b""}
    source = _source(tmp_path / "source", payload, "orca_auto")
    assert check_wheel_contents(_wheel(tmp_path / "package.whl", payload), source) == []


@pytest.mark.parametrize(
    "version,requires,extras,accepted",
    [
        ("2.3.4", (), (), True),
        ("2.2.0", (), (), False),
        ("2.3.4", ("orca_auto_workflows==2.3.4",), (), False),
        ("2.3.4", ("orca.auto.workflows==2.3.4",), (), False),
        ("2.3.4", (), ("workflows",), False),
    ],
)
def test_distribution_gate_rejects_stale_version_and_retired_dependency(
    tmp_path: Path, version: str, requires: tuple[str, ...], extras: tuple[str, ...], accepted: bool
) -> None:
    payload = {"orca_auto/__init__.py": b"", "orca_auto/py.typed": b""}
    source = _source(tmp_path / "source", payload, "orca_auto")
    wheel = _wheel(
        tmp_path / "package.whl", payload, version=version, requires=requires, extras=extras
    )
    if accepted:
        _assert_distribution(wheel, source, expected_version="2.3.4")
    else:
        with pytest.raises(AssertionError):
            _assert_distribution(wheel, source, expected_version="2.3.4")


@pytest.mark.parametrize(
    "core_version,flow_version,cli_output,accepted",
    [
        ("2.3.4", None, "orca_auto 2.3.4\n", True),
        ("2.3.4", "2.3.4", "orca_auto 2.3.4\n", False),
        ("2.2.0", None, "orca_auto 2.3.4\n", False),
        ("2.2.0", "2.2.0", "orca_auto 2.3.4\n", False),
        ("2.3.4", "2.2.0", "orca_auto 2.3.4\n", False),
        ("2.3.4", None, "orca_auto 2.2.0\n", False),
        ("2.3.4", "2.3.4", "orca_auto 2.2.0\n", False),
        ("2.3.4", None, "unexpected text\norca_auto 2.3.4\n", False),
        ("2.3.4", None, "orca_auto 2.3.4\n\n", False),
    ],
    ids=[
        "core-only",
        "with-workflows",
        "stale-core-only",
        "stale-matching-pair",
        "stale-workflows",
        "stale-core-cli",
        "stale-workflows-cli",
        "extra-cli-text",
        "extra-cli-newline",
    ],
)
def test_installed_probe_checks_metadata_and_cli_release_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_version: str,
    flow_version: str | None,
    cli_output: str,
    accepted: bool,
) -> None:
    core_source = tmp_path / "orca_auto"
    core_module = ModuleType("orca_auto")
    core_module.__file__ = str(core_source / "__init__.py")
    core_module.__path__ = [str(core_source)]
    flow_module = ModuleType("orca_auto.flow")
    flow_module.__file__ = str(core_source / "flow" / "__init__.py")
    core_module.__dict__["flow"] = flow_module

    def installed_version(name: str) -> str:
        if name == "orca_auto":
            return core_version
        if name == "orca_auto_workflows" and flow_version is not None:
            return flow_version
        raise metadata.PackageNotFoundError(name)

    def probe_subprocess(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "-c" in argv:
            index = argv.index("-c")
            # Execute the actual isolated probe code against synthetic installed
            # modules/metadata; no environment is installed or subprocess started.
            with monkeypatch.context() as isolated:
                isolated.setattr(sys, "argv", ["-c", *argv[index + 2 :]])
                isolated.setitem(sys.modules, "orca_auto", core_module)
                isolated.setitem(sys.modules, "orca_auto.flow", flow_module)
                isolated.setattr(metadata, "version", installed_version)
                isolated.setattr(util, "find_spec", lambda name: None)
                try:
                    exec(argv[index + 1], {})
                except AssertionError as exc:
                    return subprocess.CompletedProcess(argv, 1, "", str(exc))
        return subprocess.CompletedProcess(argv, 0, cli_output if "--version" in argv else "", "")

    monkeypatch.setattr("scripts.check_distributions.subprocess.run", probe_subprocess)
    if accepted:
        _probe(
            tmp_path / "bin" / "python",
            cwd=tmp_path,
            core_source=core_source,
            expected_version="2.3.4",
        )
    else:
        with pytest.raises((AssertionError, RuntimeError)):
            _probe(
                tmp_path / "bin" / "python",
                cwd=tmp_path,
                core_source=core_source,
                expected_version="2.3.4",
            )


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("missing", "missing source payload"),
        ("stale", "unowned source payload"),
        ("changed", "differs from source bytes"),
        ("unexpected-install-path", "unsupported installation payload"),
        ("unsafe-path", "unsafe paths"),
    ],
)
def test_checker_rejects_wrong_payload_even_with_valid_record(
    tmp_path: Path, mutation: str, expected_error: str
) -> None:
    payload = {"orca_auto/__init__.py": b"", "orca_auto/py.typed": b""}
    source = _source(tmp_path / "source", payload, "orca_auto")
    if mutation == "missing":
        del payload["orca_auto/__init__.py"]
    elif mutation == "stale":
        payload["orca_auto/stale.py"] = b""
    elif mutation == "changed":
        payload["orca_auto/__init__.py"] = b"changed = True\n"
    elif mutation == "unsafe-path":
        payload["../escape.py"] = b""
    else:
        payload["orca_auto-fake/escape.py"] = b""
    errors = check_wheel_contents(_wheel(tmp_path / "bad.whl", payload), source)
    assert any(expected_error in error for error in errors), errors


@pytest.mark.parametrize("corruption", ["hash", "missing-row", "duplicate-row", "duplicate-file"])
def test_record_corruption_is_rejected(tmp_path: Path, corruption: str) -> None:
    payload = {"orca_auto/__init__.py": b"", "orca_auto/py.typed": b""}
    source = _source(tmp_path / "source", payload, "orca_auto")
    good = _wheel(tmp_path / "good.whl", payload)
    record = "orca_auto-5.0.0.dev0.dist-info/RECORD"
    with ZipFile(good) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    lines = files[record].decode().splitlines(keepends=True)
    if corruption == "hash":
        lines[0] = lines[0].replace("sha256=", "sha256=wrong")
    elif corruption == "missing-row":
        lines.pop(0)
    elif corruption == "duplicate-row":
        lines.insert(0, lines[0])
    files[record] = "".join(lines).encode()
    bad = tmp_path / "bad.whl"
    with ZipFile(bad, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
        if corruption == "duplicate-file":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("orca_auto/__init__.py", b"")
    errors = check_wheel_contents(bad, source)
    assert errors
    assert any("RECORD" in error or "duplicate archive" in error for error in errors), errors


@pytest.mark.parametrize("member_name", ["../escaped", "/absolute", "root\\escaped"])
def test_sdist_extraction_rejects_paths_outside_its_directory(
    tmp_path: Path, member_name: str
) -> None:
    archive = tmp_path / "malformed.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo(member_name)
        member.size = 1
        output.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe sdist path"):
        _unpack_sdist(archive, tmp_path / "unpacked")


def test_sdist_rebuild_input_is_self_contained(tmp_path: Path) -> None:
    archive = tmp_path / "package.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("package-1.0/pyproject.toml")
        member.size = 1
        output.addfile(member, io.BytesIO(b"x"))
    project = _unpack_sdist(archive, tmp_path / "unpacked")
    assert project == tmp_path / "unpacked" / "package-1.0"
    assert (project / "pyproject.toml").read_bytes() == b"x"


@pytest.mark.parametrize(
    "extra_member",
    [
        None,
        "extensions/workflows/pyproject.toml",
        "extensions/workflows/src/orca_auto/flow/__init__.py",
        "src/orca_auto/flow/__init__.py",
    ],
)
def test_core_sdist_manifest_cannot_bundle_workflows(
    tmp_path: Path, extra_member: str | None
) -> None:
    archive = tmp_path / "core.tar.gz"
    names = ["pyproject.toml", "src/orca_auto/__init__.py"]
    if extra_member is not None:
        names.append(extra_member)
    with tarfile.open(archive, "w:gz") as output:
        for name in names:
            member = tarfile.TarInfo("orca_auto-5.0.0.dev0/" + name)
            member.size = 1
            output.addfile(member, io.BytesIO(b"x"))
    if extra_member is None:
        _assert_core_sdist(archive)
    else:
        with pytest.raises(AssertionError, match="core sdist includes workflows source"):
            _assert_core_sdist(archive)


@pytest.mark.parametrize(
    ("invalidation", "accepted"),
    [("checked-hash", True), ("timestamp", False), (None, False)],
)
def test_bytecode_check_rejects_runtimes_an_interpreter_writes_into(
    tmp_path: Path, invalidation: str | None, accepted: bool
) -> None:
    root = tmp_path / "runtime"
    venv.EnvBuilder(symlinks=True).create(root / ".venv")
    python = root / ".venv/bin/python"
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = root / ".venv/lib" / version / "site-packages"
    for name, text in (
        ("orca_auto/__init__.py", ""),
        ("orca_auto/cli.py", "print('fixture')\n"),
        ("yaml/__init__.py", ""),
        ("pip/__init__.py", ""),
        ("pip/__main__.py", "print('fixture')\n"),
    ):
        (site / name).parent.mkdir(exist_ok=True)
        (site / name).write_text(text)
    if invalidation is not None:
        subprocess.run(
            [str(python), "-I", "-m", "compileall", "-q", "--invalidation-mode", invalidation]
            + [str(site)],
            check=True,
        )
    for path in (root, *root.rglob("*")):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)
    try:
        if accepted:
            _assert_runtime_writes_no_bytecode(root, work=tmp_path)
        else:
            with pytest.raises(AssertionError, match="interpreter wrote"):
                _assert_runtime_writes_no_bytecode(root, work=tmp_path)
    finally:
        _unseal(root)
