from __future__ import annotations

import tomllib
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO_ROOT / "src" / "orca_auto"
_MACHINE_CONTRACT_DATA = {
    "machine_contracts/LICENSE",
    "machine_contracts/PROVENANCE.md",
    "machine_contracts/schemas/machine-observation-v1.schema.json",
    "machine_contracts/schemas/payloads/chemistry-results-bundle-v1.schema.json",
}


def _declared_patterns() -> list[str]:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as handle:
        package_data = tomllib.load(handle)["tool"]["setuptools"]["package-data"]
    return list(package_data["orca_auto"])


def _matches(name: str, pattern: str) -> bool:
    # setuptools globs per path segment: ``*`` never crosses a ``/``.
    parts, globs = PurePosixPath(name).parts, PurePosixPath(pattern).parts
    return len(parts) == len(globs) and all(map(fnmatchcase, parts, globs))


def _source_data_files() -> set[str]:
    return {
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".py", ".pyc"}
    }


def test_machine_contract_resources_are_source_files() -> None:
    assert _MACHINE_CONTRACT_DATA <= _source_data_files()


def test_package_data_ships_every_non_python_source_file_in_wheel_and_sdist() -> None:
    # setuptools copies declared package data into both the wheel and the sdist;
    # an undeclared file is missing from both, and the installed validator with it.
    patterns = _declared_patterns()
    undeclared = sorted(
        name
        for name in _source_data_files()
        if not any(_matches(name, pattern) for pattern in patterns)
    )
    assert undeclared == []


def test_manifest_does_not_drop_machine_contract_resources() -> None:
    for line in (_REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
        command, *arguments = line.split() or [""]
        if command in {"exclude", "global-exclude", "recursive-exclude", "prune"}:
            assert not any(
                "machine_contracts" in argument or argument.endswith(".json")
                for argument in arguments
            ), line
