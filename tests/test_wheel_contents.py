"""Regression tests for exact source-to-wheel Python package inventory."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.check_wheel_contents import check_wheel_contents

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "orca_auto"


def _source_python_files() -> set[str]:
    return {
        f"orca_auto/{path.relative_to(_SOURCE_ROOT).as_posix()}"
        for path in _SOURCE_ROOT.rglob("*.py")
        if path.is_file()
    }


def _write_wheel(path: Path, names: set[str]) -> Path:
    with ZipFile(path, "w") as wheel:
        for name in sorted(names):
            wheel.writestr(name, "")
    return path


def test_wheel_inventory_accepts_exact_source_modules(tmp_path: Path) -> None:
    wheel = _write_wheel(
        tmp_path / "exact.whl",
        _source_python_files() | {"orca_auto/py.typed"},
    )

    assert check_wheel_contents(wheel, _SOURCE_ROOT) == []


def test_wheel_inventory_rejects_stale_module(tmp_path: Path) -> None:
    wheel = _write_wheel(
        tmp_path / "stale.whl",
        _source_python_files() | {"orca_auto/py.typed", "orca_auto/flow/workflow/si.py"},
    )

    errors = check_wheel_contents(wheel, _SOURCE_ROOT)

    assert any("stale/unknown" in error and "workflow/si.py" in error for error in errors)


def test_wheel_inventory_rejects_missing_module(tmp_path: Path) -> None:
    source_files = _source_python_files()
    missing = "orca_auto/flow/workflow/si/collection.py"
    wheel = _write_wheel(
        tmp_path / "missing.whl",
        (source_files - {missing}) | {"orca_auto/py.typed"},
    )

    errors = check_wheel_contents(wheel, _SOURCE_ROOT)

    assert any("missing source" in error and missing in error for error in errors)


def test_wheel_inventory_rejects_nested_typing_marker(tmp_path: Path) -> None:
    wheel = _write_wheel(
        tmp_path / "marker.whl",
        _source_python_files() | {"orca_auto/py.typed", "orca_auto/core/py.typed"},
    )

    errors = check_wheel_contents(wheel, _SOURCE_ROOT)

    assert any("typing markers" in error and "core/py.typed" in error for error in errors)


@pytest.mark.parametrize(
    "duplicate_entry",
    ["orca_auto/flow/workflow/si/collection.py", "orca_auto/py.typed"],
)
def test_wheel_inventory_rejects_duplicate_package_entries(
    tmp_path: Path,
    duplicate_entry: str,
) -> None:
    wheel_path = _write_wheel(
        tmp_path / "duplicate.whl",
        _source_python_files() | {"orca_auto/py.typed"},
    )
    with pytest.warns(UserWarning, match="Duplicate name"):
        with ZipFile(wheel_path, "a") as wheel:
            wheel.writestr(duplicate_entry, "shadowed content")

    errors = check_wheel_contents(wheel_path, _SOURCE_ROOT)

    assert any(
        "duplicate Python/typing entries" in error and duplicate_entry in error for error in errors
    )
