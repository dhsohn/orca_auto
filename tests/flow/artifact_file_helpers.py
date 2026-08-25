"""Shared on-disk artifact writers for flow adapter/orchestration tests."""

from __future__ import annotations

import json
from pathlib import Path


def _write_xyz_ensemble(path: Path, comments: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for comment in comments:
        lines.extend(
            [
                "2",
                comment,
                "H 0 0 0",
                "H 0 0 0.74",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    if path.name == "queue.json" and isinstance(payload, list):
        payload = [
            {
                "app_name": "orca_auto_orca",
                "engine": "orca",
                "task_kind": "orca_run_inp",
                **item,
            }
            if isinstance(item, dict)
            else item
            for item in payload
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
