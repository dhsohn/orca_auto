from __future__ import annotations

from pathlib import Path

from orca_auto.core.activity import ActivitySourceRequest, ResolvedActivitySources
from orca_auto.core.config import discovery
from orca_auto.core.config.files import shared_workflow_root_from_config
from orca_auto.core.engine_catalog import activity_engine_entries
from orca_auto.core.engine_runtime import engine_runtime_paths
from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.extensions import workflows_available
from orca_auto.core.paths.workflow import (
    WORKFLOW_FILE_NAME,
    iter_workflow_runtime_workspaces,
    iter_workflow_workspace_candidate_dirs,
)
from orca_auto.core.queue import list_queue
from orca_auto.core.utils import normalize_text
from orca_auto.core.workflow_identity import FLOW_MANIFEST_FILENAMES


def require_activity_support(resolved: ResolvedActivitySources) -> None:
    """Refuse an incomplete view or mutation of workflow-owned durable state."""
    if workflows_available():
        return
    roots = set()
    if normalize_text(resolved.workflow_root):
        roots.add(Path(str(resolved.workflow_root)).expanduser().resolve())
    for entry in activity_engine_entries():
        config_path = normalize_text(resolved.config_for_engine(entry.engine_id))
        if config_path:
            roots.add(engine_runtime_paths(config_path, engine=entry.engine_id)["allowed_root"])
    for root in sorted(roots):
        registry = root / "workflow_registry.json"
        try:
            registry.lstat()
        except FileNotFoundError:
            workflow_state = bool(iter_workflow_runtime_workspaces(root))
        else:
            workflow_state = True
        if not workflow_state:
            for candidate in iter_workflow_workspace_candidate_dirs(root):
                for name in (WORKFLOW_FILE_NAME, *FLOW_MANIFEST_FILENAMES):
                    try:
                        (candidate / name).lstat()
                    except FileNotFoundError:
                        continue
                    workflow_state = True
                    break
                if workflow_state:
                    break
        if not workflow_state:
            workflow_state = any(
                normalize_text(entry.metadata.get("workflow_id"))
                or not entry_matches_engine_identity(entry, "orca")
                for entry in list_queue(root)
            )
        if workflow_state:
            raise ValueError(
                f"Workflow state exists under {root}; restore the matching ORCA_auto "
                "workflows extension before inspecting or changing this queue."
            )


def discover_workflow_root(explicit: str | Path | None) -> str | None:
    explicit_text = normalize_text(explicit)
    if explicit_text:
        return str(Path(explicit_text).expanduser().resolve())
    return shared_workflow_root_from_config(discovery.resolve_shared_config_path(None))


def shared_config_hint(*configs: str | None) -> str | None:
    for config in configs:
        text = normalize_text(config)
        if text:
            return text
    return None


def resolve_activity_source_request(
    request: ActivitySourceRequest,
) -> ResolvedActivitySources:
    shared_hint = shared_config_hint(
        request.shared_config,
        request.orca_config,
        request.crest_config,
        request.xtb_config,
    )
    explicit_workflow_root = normalize_text(request.workflow_root)
    resolved_workflow_root: str | None
    if explicit_workflow_root:
        resolved_workflow_root = str(Path(explicit_workflow_root).expanduser().resolve())
    elif shared_hint:
        resolved_workflow_root = shared_workflow_root_from_config(shared_hint)
    else:
        resolved_workflow_root = discover_workflow_root(None)
    resolved_shared_config = discovery.resolve_shared_config_path(shared_hint)
    resolved_crest_config = (
        discovery.resolve_shared_config_path(request.crest_config)
        if normalize_text(request.crest_config)
        else resolved_shared_config
    )
    resolved_xtb_config = (
        discovery.resolve_shared_config_path(request.xtb_config)
        if normalize_text(request.xtb_config)
        else resolved_shared_config
    )
    resolved_orca_config = (
        discovery.resolve_shared_config_path(request.orca_config)
        if normalize_text(request.orca_config)
        else resolved_shared_config
    )
    engine_configs = {
        entry.engine_id: resolved_shared_config for entry in activity_engine_entries()
    }
    engine_configs.update(
        {
            "crest": resolved_crest_config,
            "xtb": resolved_xtb_config,
            "orca": resolved_orca_config,
        }
    )
    return ResolvedActivitySources(
        workflow_root=resolved_workflow_root,
        crest_config=resolved_crest_config,
        xtb_config=resolved_xtb_config,
        orca_config=resolved_orca_config,
        engine_configs=engine_configs,
    )
