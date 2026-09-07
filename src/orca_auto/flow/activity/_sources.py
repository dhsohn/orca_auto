from __future__ import annotations

from pathlib import Path

from orca_auto.core.config import discovery
from orca_auto.core.config.files import shared_workflow_root_from_config
from orca_auto.core.engine_catalog import activity_engine_entries
from orca_auto.core.utils import mapping_or_empty, normalize_text

from ._model import ActivitySourceRequest, ResolvedActivitySources

coerce_mapping = mapping_or_empty


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
