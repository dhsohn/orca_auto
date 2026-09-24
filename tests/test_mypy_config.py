from __future__ import annotations

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _missing_concrete_override_modules(repo_root: Path) -> list[str]:
    with (repo_root / "pyproject.toml").open("rb") as handle:
        overrides = tomllib.load(handle)["tool"]["mypy"]["overrides"]

    missing: list[str] = []
    for override in overrides:
        for module in override["module"]:
            if "*" in module:
                continue
            parts = module.split(".")
            module_path = repo_root / "src" / Path(*parts)
            if (
                not module_path.with_suffix(".py").is_file()
                and not (module_path / "__init__.py").is_file()
            ):
                missing.append(module)
    return missing


def test_concrete_mypy_override_modules_exist_in_source_inventory() -> None:
    assert _missing_concrete_override_modules(_REPO_ROOT) == []
