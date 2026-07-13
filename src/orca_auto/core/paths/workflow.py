from __future__ import annotations

import json
from pathlib import Path

from orca_auto.core.engine_catalog import engine_catalog
from orca_auto.core.utils import coerce_list, coerce_mapping, normalize_text

WORKFLOW_FILE_NAME = "workflow.json"
WORKFLOW_STAGE_DIRNAMES = {
    entry.engine_id: entry.workflow_stage_dirname
    for entry in sorted(engine_catalog(), key=lambda entry: entry.workflow_stage_dirname)
}
WORKFLOW_STAGE_DIRNAME_ALIASES = {
    entry.engine_id: entry.workflow_stage_aliases
    for entry in engine_catalog()
    if entry.workflow_stage_aliases
}


def _parenthesis_free_workflow_id(workflow_id: str) -> str:
    suggestion = workflow_id.replace("(", "_").replace(")", "")
    suggestion = suggestion.strip()
    if not suggestion.strip("._-"):
        return "workflow"
    return suggestion


def validate_workflow_id_path_segment(value: object) -> str:
    workflow_id = normalize_text(value)
    if not workflow_id:
        raise ValueError("workflow_id is required")
    if "(" in workflow_id or ")" in workflow_id:
        suggestion = _parenthesis_free_workflow_id(workflow_id)
        raise ValueError(
            "workflow_id cannot contain parentheses '(' or ')': "
            f"{workflow_id!r}. Use a name such as {suggestion!r}."
        )
    if (
        workflow_id in {".", ".."}
        or "/" in workflow_id
        or "\\" in workflow_id
        or Path(workflow_id).is_absolute()
    ):
        raise ValueError(
            f"workflow_id must be a single path segment under workflow_root: {workflow_id!r}"
        )
    return workflow_id


def validate_workflow_workspace_identity(
    workspace_dir: str | Path,
    workflow_id: object,
) -> str:
    workspace = Path(workspace_dir).expanduser().resolve()
    workspace_name = normalize_text(workspace.name)
    persisted_id = normalize_text(workflow_id) or workspace_name
    if persisted_id != workspace_name:
        raise ValueError(
            f"workflow directory name {workspace_name!r} does not match persisted "
            f"workflow_id {persisted_id!r}. Renaming an existing workflow directory is not "
            "supported; restore its original name or create a new workflow."
        )
    validate_workflow_id_path_segment(workspace_name)
    return validate_workflow_id_path_segment(persisted_id)


def workflow_root_dir(workflow_root: str | Path) -> Path:
    return Path(workflow_root).expanduser().resolve()


def workflow_workspace_internal_engine_paths(
    workspace_dir: str | Path,
    *,
    engine: str,
    stage_dirname: str | None = None,
) -> dict[str, Path]:
    engine_text = normalize_text(engine).lower()
    if not engine_text:
        raise ValueError("workflow engine is required")
    workspace = Path(workspace_dir).expanduser().resolve()
    stage_name = (
        normalize_text(stage_dirname)
        or WORKFLOW_STAGE_DIRNAMES.get(engine_text)
        or f"stage_{engine_text}"
    )
    stage_base = workspace / stage_name
    return {
        "allowed_root": stage_base,
    }


def workflow_stage_dirnames_for_engine(engine: str) -> tuple[str, ...]:
    engine_text = normalize_text(engine).lower()
    if not engine_text:
        return ()
    primary = WORKFLOW_STAGE_DIRNAMES.get(engine_text) or f"stage_{engine_text}"
    aliases = WORKFLOW_STAGE_DIRNAME_ALIASES.get(engine_text, ())
    return tuple(dict.fromkeys((primary, *aliases)))


def workflow_workspace_internal_engine_paths_from_path(
    path: str | Path,
    *,
    workflow_root: str | Path,
    engine: str,
) -> dict[str, Path] | None:
    engine_text = normalize_text(engine).lower()
    if not engine_text:
        return None

    try:
        resolved_path = Path(path).expanduser().resolve()
    except OSError:
        return None

    workspaces_root = workflow_root_dir(workflow_root)
    try:
        relative = resolved_path.relative_to(workspaces_root)
    except ValueError:
        return None

    parts = relative.parts
    if len(parts) < 2:
        return None

    for stage_dirname in workflow_stage_dirnames_for_engine(engine_text):
        if parts[1] == stage_dirname:
            return workflow_workspace_internal_engine_paths(
                workspaces_root / parts[0],
                engine=engine_text,
                stage_dirname=stage_dirname,
            )
    return None


def path_is_inside_workflow_workspace(path: str | Path, root: str | Path) -> bool:
    """True when *path* sits inside a workflow workspace under *root*.

    With workflow workspaces living under the same runs root as standalone
    ORCA jobs, standalone filesystem scans (reindex, run snapshots, discovery
    alerts) must skip anything owned by a workflow: a directory is inside a
    workspace when any ancestor at or below *root* (excluding *root* itself)
    carries a ``workflow.json``.
    """
    try:
        resolved_root = Path(root).expanduser().resolve()
        resolved = Path(path).expanduser().resolve()
        relative = resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return False
    current = resolved_root
    for part in relative.parts:
        current = current / part
        if (current / WORKFLOW_FILE_NAME).is_file():
            return True
    return False


def _workflow_payload_has_engine_stage(workspace_dir: Path, engine: str) -> bool:
    engine_text = normalize_text(engine).lower()
    if not engine_text:
        return False
    try:
        raw = json.loads((workspace_dir / WORKFLOW_FILE_NAME).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError, OSError):
        return False
    payload = coerce_mapping(raw)
    for raw_stage in coerce_list(payload.get("stages")):
        stage = coerce_mapping(raw_stage)
        task = coerce_mapping(stage.get("task"))
        if normalize_text(task.get("engine")).lower() == engine_text:
            return True
    return False


def iter_workflow_runtime_workspaces(
    workflow_root: str | Path,
    *,
    engine: str | None = None,
) -> list[Path]:
    root = workflow_root_dir(workflow_root)
    if not root.exists():
        return []

    engine_text = normalize_text(engine).lower()
    candidates: list[Path] = []
    for item in root.iterdir():
        if not item.is_dir():
            continue
        if (item / WORKFLOW_FILE_NAME).exists() and not engine_text:
            candidates.append(item)
            continue
        if engine_text:
            if _workflow_payload_has_engine_stage(item, engine_text):
                candidates.append(item)
                continue
            for stage_dirname in workflow_stage_dirnames_for_engine(engine_text):
                runtime_paths = workflow_workspace_internal_engine_paths(
                    item,
                    engine=engine_text,
                    stage_dirname=stage_dirname,
                )
                if runtime_paths["allowed_root"].exists():
                    candidates.append(item)
                    break
            continue
        stage_roots = [
            item / stage_dirname
            for engine_name in WORKFLOW_STAGE_DIRNAMES
            for stage_dirname in workflow_stage_dirnames_for_engine(engine_name)
        ]
        if any(stage_root.exists() for stage_root in stage_roots):
            candidates.append(item)
    return sorted(candidates, key=lambda item: item.name, reverse=True)


__all__ = [
    "WORKFLOW_FILE_NAME",
    "WORKFLOW_STAGE_DIRNAME_ALIASES",
    "WORKFLOW_STAGE_DIRNAMES",
    "iter_workflow_runtime_workspaces",
    "path_is_inside_workflow_workspace",
    "validate_workflow_id_path_segment",
    "validate_workflow_workspace_identity",
    "workflow_root_dir",
    "workflow_stage_dirnames_for_engine",
    "workflow_workspace_internal_engine_paths",
    "workflow_workspace_internal_engine_paths_from_path",
]
