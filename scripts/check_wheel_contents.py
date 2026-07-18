#!/usr/bin/env python3
"""Verify that a wheel contains exactly the Python modules in the source tree."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from zipfile import BadZipFile, ZipFile

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SOURCE_ROOT = _REPOSITORY_ROOT / "src" / "orca_auto"


def _source_python_files(source_root: Path) -> set[str]:
    package = source_root.name
    return {
        f"{package}/{path.relative_to(source_root).as_posix()}"
        for path in source_root.rglob("*.py")
        if path.is_file()
    }


def _wheel_python_files(names: Iterable[str], package: str) -> set[str]:
    prefix = f"{package}/"
    return {name for name in names if name.startswith(prefix) and name.endswith(".py")}


def check_wheel_contents(wheel_path: Path, source_root: Path) -> list[str]:
    """Return validation errors without mutating the source or build tree."""

    errors: list[str] = []
    if not source_root.is_dir():
        return [f"source package directory does not exist: {source_root}"]
    package = source_root.name
    source_marker = source_root / "py.typed"
    if not source_marker.is_file():
        errors.append(f"source typing marker does not exist: {source_marker}")
    try:
        with ZipFile(wheel_path) as wheel:
            names = wheel.namelist()
    except (BadZipFile, OSError) as exc:
        return [f"cannot read wheel {wheel_path}: {type(exc).__name__}: {exc}"]

    expected_python = _source_python_files(source_root)
    actual_python = _wheel_python_files(names, package)
    duplicate_entries = sorted(
        name
        for name, count in Counter(names).items()
        if count > 1
        and name.startswith(f"{package}/")
        and (name.endswith(".py") or name.endswith("/py.typed"))
    )
    if duplicate_entries:
        errors.append(
            "wheel contains duplicate Python/typing entries: " + ", ".join(duplicate_entries)
        )
    missing = sorted(expected_python - actual_python)
    extra = sorted(actual_python - expected_python)
    if missing:
        errors.append("wheel is missing source Python modules: " + ", ".join(missing))
    if extra:
        errors.append("wheel contains stale/unknown Python modules: " + ", ".join(extra))

    expected_marker = f"{package}/py.typed"
    actual_markers = sorted(
        name for name in names if name.startswith(f"{package}/") and name.endswith("/py.typed")
    )
    if actual_markers != [expected_marker]:
        errors.append(
            f"wheel typing markers must be exactly [{expected_marker}], got {actual_markers}"
        )
    return errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="wheel file to inspect")
    parser.add_argument(
        "--source-root",
        type=Path,
        default=_DEFAULT_SOURCE_ROOT,
        help="source package directory (default: repository src/orca_auto)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    errors = check_wheel_contents(args.wheel, args.source_root)
    if errors:
        for error in errors:
            print(f"wheel-content-error: {error}", file=sys.stderr)
        return 1
    module_count = len(_source_python_files(args.source_root))
    print(f"wheel contents match source: {module_count} Python modules, one root py.typed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
