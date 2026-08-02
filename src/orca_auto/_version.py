from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path


def package_version() -> str:
    try:
        return metadata.version("orca_auto")
    except metadata.PackageNotFoundError:
        return "0.0.0+unknown"


def _source_pyproject_version(source_root: Path) -> str | None:
    try:
        with open(source_root / "pyproject.toml", "rb") as fh:
            project = tomllib.load(fh)["project"]
        if project["name"] != "orca_auto":
            return None
        return str(project["version"])
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return None


def installed_version_drift(source_root: Path | None = None) -> tuple[str, str] | None:
    """Report `(installed, source)` when the two disagree, else `None`.

    An editable install freezes its distribution metadata at install time while
    the interpreter keeps importing the checkout, so a live deployment can run
    one version's code and declare another until someone reruns the install.
    Returns `None` when there is no source `pyproject.toml` to compare against,
    which is the normal shape of a wheel install rather than a drift verdict.
    """
    root = source_root if source_root is not None else Path(__file__).resolve().parents[2]
    source = _source_pyproject_version(root)
    if source is None:
        return None
    installed = package_version()
    return None if installed == source else (installed, source)


__version__ = package_version()

__all__ = ["__version__", "installed_version_drift", "package_version"]
