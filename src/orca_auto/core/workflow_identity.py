"""Persisted workflow identity markers, understood even without workflows code."""

from __future__ import annotations

from pathlib import Path

FLOW_MANIFEST_FILENAMES = ("flow.yaml",)


def is_workflow_run_dir(path: str | Path) -> bool:
    target = Path(path)
    return any((target / name).is_file() for name in ("workflow.json", *FLOW_MANIFEST_FILENAMES))
