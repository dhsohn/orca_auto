from __future__ import annotations

import os
from pathlib import Path

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR, ORCA_AUTO_REPO_ROOT_ENV_VAR
from orca_auto.core.config.files import (
    discover_shared_config_path,
    shared_workflow_root_from_config,
)
from orca_auto.core.utils import mapping_or_empty, normalize_text

from ._model import ActivitySourceRequest, ResolvedActivitySources

coerce_mapping = mapping_or_empty


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def discover_workflow_root(explicit: str | Path | None) -> str | None:
    explicit_text = normalize_text(explicit)
    if explicit_text:
        return str(Path(explicit_text).expanduser().resolve())
    return shared_workflow_root_from_config(discover_shared_config(None))


def discover_shared_config(explicit: str | None) -> str | None:
    return discover_shared_config_path(
        explicit,
        project_root(),
        env_var=ORCA_AUTO_CONFIG_ENV_VAR,
    )


def discover_orca_config(explicit: str | None) -> str | None:
    return discover_shared_config(explicit)


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
    resolved_shared_config = discover_shared_config(shared_hint)
    resolved_crest_config = (
        discover_shared_config(request.crest_config)
        if normalize_text(request.crest_config)
        else resolved_shared_config
    )
    resolved_xtb_config = (
        discover_shared_config(request.xtb_config)
        if normalize_text(request.xtb_config)
        else resolved_shared_config
    )
    resolved_orca_config = (
        discover_orca_config(request.orca_config)
        if normalize_text(request.orca_config)
        else resolved_shared_config
    )
    return ResolvedActivitySources(
        workflow_root=resolved_workflow_root,
        crest_config=resolved_crest_config,
        xtb_config=resolved_xtb_config,
        orca_config=resolved_orca_config,
    )


def discover_orca_repo_root(explicit: str | None) -> str | None:
    explicit_text = normalize_text(explicit)
    if explicit_text:
        return str(Path(explicit_text).expanduser().resolve())
    env_text = normalize_text(os.getenv(ORCA_AUTO_REPO_ROOT_ENV_VAR))
    if env_text:
        return str(Path(env_text).expanduser().resolve())
    return None
