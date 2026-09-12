from __future__ import annotations

import base64
import csv
import hashlib
import io
import tarfile
from importlib import import_module
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.check_distributions import _assert_core_sdist, _copy_project, _unpack_sdist
from scripts.check_wheel_contents import check_disjoint_ownership, check_wheel_contents


def _wheel(path: Path, payload: dict[str, bytes], *, name: str = "orca_auto") -> Path:
    metadata = f"{name}-5.0.0.dev0.dist-info"
    files = {
        **payload,
        f"{metadata}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 5.0.0.dev0\n".encode(),
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


def test_each_distribution_owns_only_its_source_payload(tmp_path: Path) -> None:
    core_payload = {"orca_auto/__init__.py": b"", "orca_auto/py.typed": b""}
    flow_payload = {"orca_auto/flow/__init__.py": b"", "orca_auto/flow/py.typed": b""}
    core_source = _source(tmp_path / "core", core_payload, "orca_auto")
    flow_source = _source(tmp_path / "flow", flow_payload, "orca_auto/flow")
    core = _wheel(tmp_path / "core.whl", core_payload)
    flow = _wheel(tmp_path / "flow.whl", flow_payload, name="orca_auto_workflows")
    assert check_wheel_contents(core, core_source) == []
    assert (
        check_wheel_contents(
            flow, flow_source, package="orca_auto/flow", distribution="orca_auto_workflows"
        )
        == []
    )
    assert check_disjoint_ownership(core, flow) == []


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


def test_workflows_cannot_own_parent_package_or_console_entrypoint(tmp_path: Path) -> None:
    flow_payload = {"orca_auto/flow/__init__.py": b""}
    source = _source(tmp_path / "flow", flow_payload, "orca_auto/flow")
    wheel = _wheel(
        tmp_path / "flow.whl",
        {
            **flow_payload,
            "orca_auto/__init__.py": b"",
            "orca_auto/py.typed": b"",
            "orca_auto_workflows-5.0.0.dev0.dist-info/entry_points.txt": b"[console_scripts]\n",
        },
        name="orca_auto_workflows",
    )
    errors = check_wheel_contents(
        wheel, source, package="orca_auto/flow", distribution="orca_auto_workflows"
    )
    assert any("unowned source payload" in error for error in errors)
    assert any("console entry points" in error for error in errors)


def test_cross_distribution_file_overlap_is_rejected(tmp_path: Path) -> None:
    core = _wheel(tmp_path / "core.whl", {"orca_auto/__init__.py": b""})
    flow = _wheel(tmp_path / "flow.whl", {"orca_auto/__init__.py": b""}, name="orca_auto_workflows")
    assert check_disjoint_ownership(core, flow) == [
        "distribution wheels share installed files: orca_auto/__init__.py"
    ]


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


def test_root_build_fixture_retains_nested_project_and_broad_discovery_hazard(
    tmp_path: Path,
) -> None:
    find_namespace_packages = import_module("setuptools").find_namespace_packages
    repo = tmp_path / "repository"
    core = repo / "src" / "orca_auto"
    extension = repo / "extensions" / "workflows"
    flow = extension / "src" / "orca_auto" / "flow"
    core.mkdir(parents=True)
    flow.mkdir(parents=True)
    (core / "__init__.py").write_text("", encoding="utf-8")
    (core / "py.typed").write_text("", encoding="utf-8")
    (flow / "__init__.py").write_text("", encoding="utf-8")
    for project in (repo, extension):
        (project / "pyproject.toml").write_text(
            '[project]\nversion = "5.0.0.dev0"\n', encoding="utf-8"
        )
        (project / "MANIFEST.in").write_text("recursive-include src *.py\n", encoding="utf-8")
    staged = tmp_path / "staged"
    _copy_project(repo, staged, include_workflows=True)
    staged_extension = staged / "extensions" / "workflows"
    assert (staged_extension / "pyproject.toml").read_bytes() == (
        extension / "pyproject.toml"
    ).read_bytes()
    assert (staged_extension / "MANIFEST.in").read_bytes() == (
        extension / "MANIFEST.in"
    ).read_bytes()
    assert (staged_extension / "src" / "orca_auto" / "flow" / "__init__.py").is_file()
    assert find_namespace_packages(where=str(staged / "src")) == ["orca_auto"]
    wrong_discovery = find_namespace_packages(where=str(staged))
    assert "extensions.workflows.src.orca_auto.flow" in wrong_discovery
    # The same wrong root discovery produces payload outside core ownership,
    # which the wheel checker must reject rather than hiding it in the fixture.
    payload = {
        "orca_auto/__init__.py": b"",
        "orca_auto/py.typed": b"",
        "extensions/workflows/src/orca_auto/flow/__init__.py": b"",
    }
    errors = check_wheel_contents(
        _wheel(tmp_path / "wrong-discovery.whl", payload), staged / "src" / "orca_auto"
    )
    assert any("unsupported installation payload" in error for error in errors), errors


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
