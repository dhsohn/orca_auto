from __future__ import annotations

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_NAME = "Domain packages never import the top-level CLI layer"
# Consumed on both sides of the boundary: the CLI prints the version and the
# domain packages stamp it into machine.json and job state.
_SHARED_TOP_LEVEL_MODULES = frozenset({"orca_auto._version"})
_DOMAIN_PACKAGES = frozenset({"core", "orca"})


def _forbidden_top_level_modules(repo_root: Path) -> set[str]:
    with (repo_root / "pyproject.toml").open("rb") as handle:
        contracts = tomllib.load(handle)["tool"]["importlinter"]["contracts"]
    matching = [contract for contract in contracts if contract["name"] == _CONTRACT_NAME]
    assert len(matching) == 1, matching
    return set(matching[0]["forbidden_modules"])


def _top_level_modules(repo_root: Path) -> set[str]:
    package = repo_root / "src" / "orca_auto"
    modules = {
        f"orca_auto.{path.stem}" for path in package.glob("*.py") if path.name != "__init__.py"
    }
    # Outer command composition may be a package (activity/) rather than a
    # single .py file. It must be protected by the same domain-import contract.
    modules.update(
        f"orca_auto.{path.name}"
        for path in package.iterdir()
        if path.is_dir() and path.name not in _DOMAIN_PACKAGES and (path / "__init__.py").is_file()
    )
    return modules


def test_cli_layer_contract_names_every_top_level_module() -> None:
    forbidden = _forbidden_top_level_modules(_REPO_ROOT)
    expected = _top_level_modules(_REPO_ROOT) - _SHARED_TOP_LEVEL_MODULES
    assert forbidden == expected
