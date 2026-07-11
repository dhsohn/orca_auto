"""Provider-neutral settings used by the interactive bot application."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orca_auto.core.app_ids import ORCA_AUTO_REPO_ROOT_ENV_VAR
from orca_auto.core.config.files import shared_workflow_root_from_config

from ..activity import _sources as _activity_sources


@dataclass(frozen=True)
class BotSettings:
    """Filesystem/config context required by the shared activity commands.

    Messenger credentials deliberately do not live here.  A native adapter owns
    those and passes only normalized commands/actions into the application.
    """

    workflow_root: str | None
    crest_config: str | None
    xtb_config: str | None
    orca_config: str | None
    orca_repo_root: str | None
    runs_root: str | None = None


def _env_text(getenv: Callable[..., str | None], name: str) -> str:
    return str(getenv(name, "") or "").strip()


def settings_from_config(
    config_path: str | None = None,
    *,
    activity_sources: Any = _activity_sources,
    getenv: Callable[..., str | None] = os.getenv,
    workflow_root_from_config: Callable[
        [str | None], str | None
    ] = shared_workflow_root_from_config,
) -> BotSettings:
    """Resolve shared application paths without selecting a messenger provider."""

    shared_config = activity_sources.discover_shared_config(config_path)
    # ``shared_workflow_root_from_config`` resolves the top-level ``runs_root``
    # key (the historical name predates the single-root refactor), so it is both
    # the workflow root and the trusted extraction root for uploaded run-dirs.
    runs_root = workflow_root_from_config(shared_config)
    workflow_root = runs_root or activity_sources.discover_workflow_root(None)
    return BotSettings(
        workflow_root=workflow_root,
        crest_config=shared_config,
        xtb_config=shared_config,
        orca_config=shared_config,
        orca_repo_root=_env_text(getenv, ORCA_AUTO_REPO_ROOT_ENV_VAR) or None,
        runs_root=runs_root,
    )


__all__ = ["BotSettings", "settings_from_config"]
