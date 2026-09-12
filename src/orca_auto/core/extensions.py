"""Availability boundary for the single official workflows extension.

This is not a third-party plugin registry. Persisted engine/workflow identities
remain known to core even when their optional implementation is not installed.
"""

from __future__ import annotations

from importlib import import_module, metadata
from importlib.util import find_spec

WORKFLOWS_UNAVAILABLE = (
    "The ORCA_auto workflows extension is not installed. "
    "Use the matching workflow-enabled ORCA_auto installation for this operation."
)


def workflows_available() -> bool:
    """Probe presence, without disguising an installed extension's import errors."""
    spec = find_spec("orca_auto.flow")
    # A removed distribution can leave cache directories behind. Those are
    # namespace portions, not the official regular workflows package.
    return spec is not None and spec.origin is not None and spec.loader is not None


def validate_workflow_installation() -> None:
    """Require the two installed distributions to belong to the same release."""
    try:
        core_version = metadata.version("orca_auto")
        workflow_version = metadata.version("orca_auto_workflows")
    except metadata.PackageNotFoundError as exc:
        raise ValueError(
            "The ORCA_auto workflows extension requires installed metadata for both "
            "orca_auto and orca_auto_workflows. Install the matching distributions "
            "in this Python environment; adding a source directory is not an installation."
        ) from exc
    if core_version != workflow_version:
        raise ValueError(
            "ORCA_auto core/workflows version mismatch: "
            f"orca_auto={core_version}, orca_auto_workflows={workflow_version}. "
            "Install matching versions before using workflows."
        )


def require_workflows() -> None:
    if not workflows_available():
        raise ValueError(WORKFLOWS_UNAVAILABLE)
    # A broken installed extension must fail as broken, never as core-only.
    import_module("orca_auto.flow")
    validate_workflow_installation()
