from __future__ import annotations

from .engine_catalog import get_engine_catalog_entry

ORCA_AUTO_CLI_MODULE = "orca_auto.cli"
ORCA_AUTO_CLI_COMMAND = f"python -m {ORCA_AUTO_CLI_MODULE}"
ORCA_AUTO_WORKFLOW_WORKER_MODULE = "orca_auto.flow.cli.workflow"

ORCA_AUTO_ORCA_APP_NAME = get_engine_catalog_entry("orca").app_id

ORCA_AUTO_ORCA_SOURCE = get_engine_catalog_entry("orca").source_id

ORCA_AUTO_ORCA_SUBMITTER = ORCA_AUTO_ORCA_APP_NAME
ORCA_SUBMITTERS = frozenset({ORCA_AUTO_ORCA_SUBMITTER})

ORCA_AUTO_CONFIG_ENV_VAR = "ORCA_AUTO_CONFIG"


def is_orca_submitter(value: object | None) -> bool:
    return str(value or "").strip() in ORCA_SUBMITTERS
