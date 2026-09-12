#!/usr/bin/env python3
"""Check distribution payload ownership and the integrity of wheel RECORDs."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import sys
from collections import Counter
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SOURCE_ROOT = _REPOSITORY_ROOT / "src" / "orca_auto"
_WORKFLOWS_SOURCE_ROOT = (
    _REPOSITORY_ROOT / "extensions" / "workflows" / "src" / "orca_auto" / "flow"
)


def _source_payload_files(source_root: Path, package: str) -> set[str]:
    return {
        f"{package}/{path.relative_to(source_root).as_posix()}"
        for path in source_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _record_errors(wheel: ZipFile, names: list[str]) -> list[str]:
    records = [name for name in names if name.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        return ["wheel must contain exactly one distribution RECORD"]
    record = records[0]
    errors: list[str] = []
    try:
        rows = list(csv.reader(io.StringIO(wheel.read(record).decode("utf-8"))))
    except (UnicodeError, csv.Error) as exc:
        return [f"cannot parse wheel RECORD: {exc}"]
    if any(len(row) != 3 for row in rows):
        return ["wheel RECORD rows must have three fields"]
    paths = [row[0] for row in rows]
    if len(set(paths)) != len(paths):
        errors.append("wheel RECORD contains duplicate paths")
    if set(paths) != set(names):
        errors.append("wheel RECORD does not describe exactly every archive file")
    for name, digest, size in rows:
        if name not in names:
            continue
        if name == record:
            if digest or size:
                errors.append("wheel RECORD must not hash itself")
            continue
        contents = wheel.read(name)
        expected_digest = base64.urlsafe_b64encode(hashlib.sha256(contents).digest()).rstrip(b"=")
        if digest != "sha256=" + expected_digest.decode("ascii") or size != str(len(contents)):
            errors.append(f"wheel RECORD hash/size mismatch: {name}")
    return errors


def check_wheel_contents(
    wheel_path: Path,
    source_root: Path,
    *,
    package: str = "orca_auto",
    distribution: str = "orca_auto",
) -> list[str]:
    """Return validation errors without mutating the source or build tree."""

    errors: list[str] = []
    metadata_directory: str | None = None
    if not source_root.is_dir():
        return [f"source package directory does not exist: {source_root}"]
    source_marker = source_root / "py.typed"
    expected_payload = _source_payload_files(source_root, package)
    if package == "orca_auto" and not source_marker.is_file():
        errors.append(f"source typing marker does not exist: {source_marker}")
    try:
        with ZipFile(wheel_path) as wheel:
            names = [entry.filename for entry in wheel.infolist() if not entry.is_dir()]
            errors.extend(_record_errors(wheel, names))
            for name in expected_payload & set(names):
                relative = name.removeprefix(package + "/")
                if wheel.read(name) != (source_root / relative).read_bytes():
                    errors.append(f"wheel payload differs from source bytes: {name}")
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                errors.append("wheel must contain exactly one distribution METADATA")
            else:
                metadata_directory = metadata_names[0].split("/", 1)[0]
                metadata = BytesParser().parsebytes(wheel.read(metadata_names[0]))
                if str(metadata.get("Name", "")).replace("-", "_") != distribution:
                    errors.append(f"unexpected wheel distribution name; expected {distribution}")
            if distribution == "orca_auto_workflows" and any(
                name.endswith(".dist-info/entry_points.txt") for name in names
            ):
                errors.append("workflows must not own console entry points")
    except (BadZipFile, OSError) as exc:
        return [f"cannot read wheel {wheel_path}: {type(exc).__name__}: {exc}"]

    unsafe_names = [
        name
        for name in names
        if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or "\\" in name
    ]
    if unsafe_names:
        errors.append("wheel contains unsafe paths: " + ", ".join(sorted(unsafe_names)))
    actual_payload = {name for name in names if name.startswith("orca_auto/")}
    duplicate_entries = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicate_entries:
        errors.append("wheel contains duplicate archive entries: " + ", ".join(duplicate_entries))
    missing = sorted(expected_payload - actual_payload)
    extra = sorted(actual_payload - expected_payload)
    if missing:
        errors.append("wheel is missing source payload: " + ", ".join(missing))
    if extra:
        errors.append("wheel contains unowned source payload: " + ", ".join(extra))
    unknown = [
        name
        for name in names
        if not name.startswith("orca_auto/") and name.split("/", 1)[0] != metadata_directory
    ]
    if unknown:
        errors.append("wheel contains unsupported installation payload: " + ", ".join(unknown))
    return errors


def check_disjoint_ownership(core_wheel: Path, workflows_wheel: Path) -> list[str]:
    with ZipFile(core_wheel) as core, ZipFile(workflows_wheel) as workflows:
        core_files = {entry.filename for entry in core.infolist() if not entry.is_dir()}
        workflow_files = {entry.filename for entry in workflows.infolist() if not entry.is_dir()}
        shared = core_files & workflow_files
    return (
        ["distribution wheels share installed files: " + ", ".join(sorted(shared))]
        if shared
        else []
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="wheel file to inspect")
    parser.add_argument("--distribution", choices=["core", "workflows"], default="core")
    parser.add_argument(
        "--source-root",
        type=Path,
        help="override the selected distribution's source package directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    workflows = args.distribution == "workflows"
    source_root = args.source_root or (
        _WORKFLOWS_SOURCE_ROOT if workflows else _DEFAULT_SOURCE_ROOT
    )
    errors = check_wheel_contents(
        args.wheel,
        source_root,
        package="orca_auto/flow" if workflows else "orca_auto",
        distribution="orca_auto_workflows" if workflows else "orca_auto",
    )
    if errors:
        for error in errors:
            print(f"wheel-content-error: {error}", file=sys.stderr)
        return 1
    print(f"wheel contents and RECORD match {args.distribution} source ownership")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
