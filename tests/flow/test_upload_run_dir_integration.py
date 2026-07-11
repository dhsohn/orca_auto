"""End-to-end smoke: uploaded archives extract into run-dirs the real CLI
submission path can classify.

These guard against the allowlist or extraction diverging from what
``_detect_run_dir_app`` (the shared submission entrypoint) accepts, without
touching the live queue.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from orca_auto.cli_handlers import _detect_run_dir_app
from orca_auto.core.ingest import UploadPolicy, extract_archive, inspect_archive

_WATER_INP = b"! HF STO-3G Opt\n* xyz 0 1\nO 0 0 0\nH 0 0.75 0.58\nH 0 -0.75 0.58\n*\n"
_XYZ = b"1\n\nH 0.0 0.0 0.0\n"
_FLOW = b"kind: scan_ts\nengine: orca\n"


def _policy() -> UploadPolicy:
    return UploadPolicy(enabled=True)


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def _detect(job_dir: Path) -> str:
    return _detect_run_dir_app(argparse.Namespace(path=str(job_dir)))


def test_orca_run_dir_roundtrip(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "water_opt.zip",
        {"water_opt/water_opt.inp": _WATER_INP, "water_opt/geo.xyz": _XYZ},
    )
    assert inspect_archive(archive, _policy()).engine_hint == "orca"

    job_dir = extract_archive(archive, tmp_path / "runs", "water_opt", _policy())
    assert (job_dir / "water_opt.inp").read_bytes() == _WATER_INP
    # The shared submission path must recognize what we wrote.
    assert _detect(job_dir) == "orca"


def test_workflow_run_dir_roundtrip(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "rxn.zip",
        {
            "rxn/flow.yaml": _FLOW,
            "rxn/reactant.xyz": _XYZ,
            "rxn/product.xyz": _XYZ,
        },
    )
    assert inspect_archive(archive, _policy()).engine_hint == "workflow"

    job_dir = extract_archive(archive, tmp_path / "runs", "rxn", _policy())
    assert (job_dir / "flow.yaml").exists()
    assert _detect(job_dir) == "workflow"
