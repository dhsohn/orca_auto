"""Run all import contracts over both source roots in this checkout.

Grimp reads ModuleSpec locations without executing pkgutil namespace initializers.
Supply the two explicit source locations for static analysis only; runtime import
discovery still depends exclusively on which distributions were installed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def configure_source_roots(repo: Path) -> None:
    package_roots = [repo / "src" / "orca_auto", repo / "extensions/workflows/src/orca_auto"]
    for required in (package_roots[0] / "__init__.py", package_roots[1] / "flow/__init__.py"):
        if not required.is_file():
            raise SystemExit(f"Missing source package for import contracts: {required}")
    spec = importlib.util.spec_from_file_location(
        "orca_auto",
        package_roots[0] / "__init__.py",
        submodule_search_locations=[str(path) for path in package_roots],
    )
    assert spec is not None
    # A static root needs only its spec, not initialization or installed metadata.
    # Explicit locations prevent a shared editable venv from scanning another tree.
    sys.modules["orca_auto"] = importlib.util.module_from_spec(spec)


if __name__ == "__main__":
    configure_source_roots(Path(__file__).resolve().parents[1])
    from importlinter.cli import lint_imports_command

    lint_imports_command()
