from __future__ import annotations

ORCA_AUTO_CLI_MODULE = "orca_auto.cli"
ORCA_AUTO_CLI_COMMAND = f"python -m {ORCA_AUTO_CLI_MODULE}"
ORCA_AUTO_WORKFLOW_WORKER_MODULE = "orca_auto.flow.cli.workflow"

ORCA_AUTO_ORCA_APP_NAME = "orca_auto_orca"

ORCA_AUTO_ORCA_SOURCE = "orca_auto_orca"

ORCA_AUTO_ORCA_SUBMITTER = "orca_auto_orca"
ORCA_SUBMITTERS = frozenset({ORCA_AUTO_ORCA_SUBMITTER})

ORCA_AUTO_CONFIG_ENV_VAR = "ORCA_AUTO_CONFIG"

ORCA_AUTO_REPO_ROOT_ENV_VAR = "ORCA_AUTO_REPO_ROOT"


def is_orca_submitter(value: object | None) -> bool:
    return str(value or "").strip() in ORCA_SUBMITTERS
