from __future__ import annotations

from pathlib import Path

from orca_auto.core.config.files import default_config_path_from_repo_root

CONFIG_ENV_VAR = "ORCA_AUTO_CONFIG"


def default_config_path() -> str:
    repo_root = Path(__file__).resolve().parents[4]
    return default_config_path_from_repo_root(repo_root, env_var=CONFIG_ENV_VAR)
