"""Source-owned validator for ORCA_auto ``factory/machine-observation`` v1 files.

Validation needs ``jsonschema``; it is imported only when a document is checked,
and its absence raises ``ImportError`` rather than skipping the check. No ORCA_auto
runtime path imports this package.
"""

from orca_auto.machine_contracts.validator import (
    UPSTREAM_COMMIT,
    ContractError,
    validate_document,
    validate_machine_path,
    validate_path,
)

__all__ = [
    "UPSTREAM_COMMIT",
    "ContractError",
    "validate_document",
    "validate_machine_path",
    "validate_path",
]
