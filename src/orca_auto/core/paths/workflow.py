from __future__ import annotations

import json
from pathlib import Path

from orca_auto.core.engine_catalog import engine_catalog
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.utils import coerce_list, coerce_mapping, normalize_text

WORKFLOW_FILE_NAME = "workflow.json"
WORKFLOW_STAGE_DIRNAMES: dict[str, str] = {
    entry.engine_id: str(entry.workflow_stage_dirname)
    for entry in sorted(
        (entry for entry in engine_catalog() if entry.workflow_stage_dirname),
        key=lambda entry: str(entry.workflow_stage_dirname),
    )
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
    persisted_id = validate_workflow_id_path_segment(workflow_id)
    if persisted_id != workspace_name:
        raise ValueError(
            f"workflow directory name {workspace_name!r} does not match persisted "
            f"workflow_id {persisted_id!r}. Renaming an existing workflow directory is not "
            "supported; restore its original name or create a new workflow."
        )
    validate_workflow_id_path_segment(workspace_name)
    return persisted_id


WORKFLOW_SCAFFOLD_MANIFEST_NAME = "flow.yaml"


def workflow_root_dir(workflow_root: str | Path) -> Path:
    return Path(workflow_root).expanduser().resolve()


def directory_is_workflow_scaffold(path: Path) -> bool:
    """Whether *path* is a workflow scaffold (carries a ``flow.yaml``)."""

    return (path / WORKFLOW_SCAFFOLD_MANIFEST_NAME).is_file()


def workflow_root_for_workspace(workspace_dir: str | Path) -> Path:
    """Derive the workflow root a workspace belongs to, without config.

    A generation workspace minted inside a scaffold (identified by the
    scaffold's ``flow.yaml``) belongs to the scaffold's parent; a direct
    root child (direct API submissions, which also mint generation names)
    belongs to its own parent.
    """

    workspace = Path(workspace_dir).expanduser().resolve()
    parent = workspace.parent
    if is_visible_generation_name(workspace.name) and directory_is_workflow_scaffold(parent):
        return parent.parent
    return parent


def is_workflow_workspace_location(workspace_dir: Path, workflow_root: Path) -> bool:
    """Whether *workspace_dir* is a trusted workspace location under *root*.

    Valid locations are a direct child of root (direct API submissions) or a
    generation-named directory inside a direct-child scaffold. The scaffold
    requirement (``flow.yaml``) keeps ORCA execution generations inside
    standalone job dirs — attacker-influenced output surfaces — from ever
    being trusted as workflow workspaces.
    """

    if workspace_dir.parent == workflow_root:
        return True
    parent = workspace_dir.parent
    return (
        parent.parent == workflow_root
        and not parent.name.startswith(".")
        and is_visible_generation_name(workspace_dir.name)
        and directory_is_workflow_scaffold(parent)
    )


def iter_workflow_workspace_candidate_dirs(workflow_root: str | Path) -> list[Path]:
    """Directories under *root* that may hold a workflow workspace.

    A workspace is a generation directory (``YYYYMMDD-HHMMSS-<8hex>``) inside
    a submitted scaffold — ``root/<scaffold>/<generation>`` — or, for direct
    API submissions without a scaffold directory, a direct child of root.
    Dot-directories (upload staging, creation staging, locks) are never
    scanned, and nothing below a workspace itself is.
    """

    root = workflow_root_dir(workflow_root)
    if not root.exists():
        return []
    candidates: list[Path] = []
    for item in root.iterdir():
        if not item.is_dir() or item.is_symlink() or item.name.startswith("."):
            continue
        candidates.append(item)
        if (item / WORKFLOW_FILE_NAME).exists():
            continue
        # Only scaffolds host generation workspaces; standalone ORCA job dirs
        # (whose execution generations share the name shape) are never scanned.
        if not directory_is_workflow_scaffold(item):
            continue
        try:
            children = sorted(item.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and not child.is_symlink() and is_visible_generation_name(child.name):
                candidates.append(child)
    return candidates


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
    return (primary,)


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
    stage_dirnames = workflow_stage_dirnames_for_engine(engine_text)
    # A workspace is either a direct child of root (direct API submissions)
    # or a generation directory inside a scaffold: root/<scaffold>/<gen>.
    for workspace_depth in (1, 2):
        if len(parts) <= workspace_depth:
            continue
        if workspace_depth == 2:
            if not is_visible_generation_name(parts[1]):
                continue
            # A generation-shaped name alone must not grant workflow paths:
            # standalone ORCA execution generations share the name shape. The
            # parent must be a scaffold, or the workspace must already carry
            # its committed manifest (which also covers a scaffold whose
            # mutable flow.yaml was removed after submission).
            scaffold_dir = workspaces_root / parts[0]
            workspace_dir = scaffold_dir / parts[1]
            if not directory_is_workflow_scaffold(scaffold_dir) and not (
                (workspace_dir / WORKFLOW_FILE_NAME).is_file()
            ):
                continue
        for stage_dirname in stage_dirnames:
            if parts[workspace_depth] == stage_dirname:
                return workflow_workspace_internal_engine_paths(
                    workspaces_root.joinpath(*parts[:workspace_depth]),
                    engine=engine_text,
                    stage_dirname=stage_dirname,
                )
    return None


def path_is_inside_workflow_workspace(path: str | Path, root: str | Path) -> bool:
    """True when *path* sits inside a workflow workspace under *root*.

    With workflow workspaces living under the same runs root as standalone
    ORCA jobs, standalone filesystem scans (reindex, run snapshots) must skip
    anything owned by a workflow: a directory is inside a
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
    except (ValueError, TypeError, OSError):
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
    for item in iter_workflow_workspace_candidate_dirs(root):
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
    "WORKFLOW_SCAFFOLD_MANIFEST_NAME",
    "WORKFLOW_STAGE_DIRNAMES",
    "directory_is_workflow_scaffold",
    "is_workflow_workspace_location",
    "iter_workflow_runtime_workspaces",
    "iter_workflow_workspace_candidate_dirs",
    "path_is_inside_workflow_workspace",
    "validate_workflow_id_path_segment",
    "validate_workflow_workspace_identity",
    "workflow_root_dir",
    "workflow_root_for_workspace",
    "workflow_stage_dirnames_for_engine",
    "workflow_workspace_internal_engine_paths",
    "workflow_workspace_internal_engine_paths_from_path",
]
