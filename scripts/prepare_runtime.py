"""Prepare an offline wheel runtime without installing units or changing live services.

Run ``python -m scripts.prepare_runtime --help`` from the source checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import venv
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from orca_auto.core.runtime_bundle import (
    RUNTIME_MANIFEST_NAME,
    content_sha256,
    runtime_build_id,
    runtime_inventory,
    verify_runtime_bundle,
)
from orca_auto.systemd_plan import SYSTEMD_UNIT_NAMES


def _wheel_identity(path: Path) -> dict[str, str]:
    with ZipFile(path) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError(f"wheel must contain exactly one distribution: {path.name}")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    name = re.sub(r"[-_.]+", "-", str(metadata.get("Name", ""))).lower()
    version = str(metadata.get("Version", ""))
    if not name or not version or not re.fullmatch(r"[A-Za-z0-9.+_-]+", version):
        raise ValueError(f"invalid distribution identity: {path.name}")
    return {"name": name, "version": version, "filename": path.name, "sha256": content_sha256(path)}


def _run(python: Path, *arguments: str, cwd: Path) -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "ORCA_AUTO_", "PIP_"))
    }
    result = subprocess.run(
        [str(python), "-I", "-B", *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise ValueError(
            f"runtime preparation failed: {result.stderr[-4000:]} {result.stdout[-4000:]}"
        )


def prepare_runtime(*, wheels: list[Path], releases_root: Path, templates: Path) -> Path:
    wheels = [path.resolve(strict=True) for path in wheels]
    identities = sorted((_wheel_identity(path) for path in wheels), key=lambda item: item["name"])
    names = [item["name"] for item in identities]
    if len(set(names)) != len(names) or names.count("orca-auto") != 1:
        raise ValueError("supply exactly one orca_auto wheel and at most one wheel per dependency")
    versions = {item["name"]: item["version"] for item in identities}
    version = versions["orca-auto"]
    if "orca-auto-workflows" in versions:
        raise ValueError("workflow distributions are no longer supported")
    templates = templates.resolve(strict=True)
    unit_hashes = {name: content_sha256(templates / name) for name in SYSTEMD_UNIT_NAMES}
    base_python = Path(str(getattr(sys, "_base_executable", sys.executable))).resolve()
    identity: dict[str, Any] = {
        "version": version,
        "wheels": identities,
        "systemd": unit_hashes,
        "python": sys.version,
        "platform": platform.platform(),
        "base_python": str(base_python),
        "base_python_sha256": content_sha256(base_python),
    }
    build_id = runtime_build_id(identity)
    releases_root = releases_root.expanduser().resolve()
    releases_root.mkdir(parents=True, exist_ok=True)
    root = releases_root / f"{version}-{build_id[:16]}"
    if root.exists():
        manifest = verify_runtime_bundle(root)
        if manifest["build_id"] != build_id:
            raise ValueError("runtime destination contains another build")
        return root
    root.mkdir()
    manifest_path = root / RUNTIME_MANIFEST_NAME
    manifest_path.write_text(json.dumps({"schema_version": 1, "state": "preparing"}) + "\n")
    # An interrupted preparation remains visibly incomplete. Never replace an
    # existing runtime: its path may still be used by a long-lived process.
    (root / "wheels").mkdir()
    for wheel in wheels:
        shutil.copyfile(wheel, root / "wheels" / wheel.name)
    for item in identities:
        if content_sha256(root / "wheels" / item["filename"]) != item["sha256"]:
            raise ValueError("wheel changed during runtime preparation")
    (root / "systemd").mkdir()
    for name, expected in unit_hashes.items():
        shutil.copyfile(templates / name, root / "systemd" / name)
        if content_sha256(root / "systemd" / name) != expected:
            raise ValueError("unit template changed during runtime preparation")
    environment = root / ".venv"
    venv.EnvBuilder(with_pip=True, symlinks=False).create(environment)
    python = environment / "bin" / "python"
    _run(
        python,
        "-m",
        "pip",
        "--isolated",
        "install",
        "--no-index",
        "--no-deps",
        "--no-compile",
        "--disable-pip-version-check",
        *(str(root / "wheels" / item["filename"]) for item in identities),
        cwd=root,
    )
    _run(python, "-m", "pip", "--isolated", "check", cwd=root)
    _run(
        python,
        "-c",
        "import orca_auto, sys; from pathlib import Path; assert Path(orca_auto.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())",
        cwd=root,
    )
    for path in root.rglob("*"):
        if path != manifest_path and not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)
    manifest = {
        "schema_version": 1,
        "state": "ready",
        "build_id": build_id,
        "runtime_root": str(root),
        "identity": identity,
        "files": runtime_inventory(root),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest_path.chmod(0o444)
    root.chmod(0o555)
    verify_runtime_bundle(root)
    return root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel",
        type=Path,
        action="append",
        required=True,
        help="Repeat for core and every runtime dependency wheel; installation is offline",
    )
    parser.add_argument("--releases-root", type=Path, required=True)
    parser.add_argument(
        "--templates", type=Path, default=Path(__file__).resolve().parents[1] / "systemd"
    )
    args = parser.parse_args()
    try:
        root = prepare_runtime(
            wheels=args.wheel, releases_root=args.releases_root, templates=args.templates
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"{exc}\n")
    print(
        json.dumps({"runtime_root": str(root), "build_id": verify_runtime_bundle(root)["build_id"]})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
