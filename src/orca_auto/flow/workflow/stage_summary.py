"""Shared, read-only summaries of durable workflow stage payloads."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.flow.orca_stage_evidence import stage_metadata as _stage_metadata


def _text(value: Any) -> str:
    return str(value or "").strip()


def _stage_artifacts(stage: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    artifacts = stage.get("output_artifacts")
    if not isinstance(artifacts, list):
        return []
    return [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and _text(artifact.get("kind")) == kind
    ]


def stage_task_kind(stage: Mapping[str, Any]) -> str:
    task = stage.get("task")
    if not isinstance(task, dict):
        return ""
    return _text(task.get("task_kind"))


def count_xyz_frames(path: Path) -> int | None:
    """Frames in a concatenated-XYZ file; ``None`` when unreadable/malformed."""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            first = handle.readline().strip()
            if not first.isdigit():
                return None
            atoms = int(first)
            if atoms <= 0:
                return None
            total_lines = 1 + sum(1 for _ in handle)
    except OSError:
        return None
    frame_lines = atoms + 2
    return total_lines // frame_lines if total_lines >= frame_lines else None


def crest_refused_ensemble_names(stage: Mapping[str, Any]) -> tuple[str, ...]:
    """Ensemble files the CREST child refused for the handoff, in stored order.

    The refusal rows are the only record of a named ensemble that existed but
    did not arrive: ``output_artifacts`` shows what did arrive and the retained
    frame count can stay flat across the loss, so neither surface can name it.
    """
    rows = _stage_metadata(stage).get("crest_rejected_retained_outputs")
    if not isinstance(rows, list):
        return ()
    names: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = _text(row.get("name"))
        if name:
            names.append(name)
    return tuple(names)


def crest_stage_detail(stage: Mapping[str, Any]) -> tuple[str, int | None]:
    metadata = _stage_metadata(stage)
    conformers_path = None
    for artifact in _stage_artifacts(stage, "crest_conformer"):
        path_text = _text(artifact.get("path"))
        if path_text.endswith("crest_conformers.xyz"):
            conformers_path = Path(path_text)
            break
    frames = count_xyz_frames(conformers_path) if conformers_path is not None else None
    parts = []
    role = _text(metadata.get("input_role"))
    if role:
        parts.append(role)
    mode = _text(metadata.get("mode"))
    if mode:
        parts.append(f"mode {mode}")
    if frames is not None:
        parts.append(f"{frames} conformers")
    refused = crest_refused_ensemble_names(stage)
    if refused:
        parts.append(f"refused {', '.join(refused)}")
    return " · ".join(parts), frames


def xtb_stage_detail(stage: Mapping[str, Any]) -> tuple[str, int]:
    metadata = _stage_metadata(stage)
    candidates = _stage_artifacts(stage, "xtb_candidate")
    kinds = [_text((artifact.get("metadata") or {}).get("kind")) for artifact in candidates]
    parts = []
    reaction_key = _text(metadata.get("reaction_key"))
    if reaction_key:
        parts.append(reaction_key)
    if candidates:
        kind_text = ", ".join(kind for kind in kinds if kind)
        parts.append(f"{len(candidates)} candidates" + (f" ({kind_text})" if kind_text else ""))
    return " · ".join(parts), len(candidates)


__all__ = [
    "count_xyz_frames",
    "crest_refused_ensemble_names",
    "crest_stage_detail",
    "stage_task_kind",
    "xtb_stage_detail",
]
