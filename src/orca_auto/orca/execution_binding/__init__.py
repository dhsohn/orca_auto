"""Bind one ORCA submission to a visible, immutable execution generation.

The package is split by stage: input inspection (``_inputs``), confinement of
referenced files (``_confinement``), generation reservation (``_reservation``),
crash-recovery seeding (``_recovery``), input rewriting (``_rewrite``),
snapshot identity (``_snapshot_identity``), and the three entry points that
build (``_build``), verify (``_verify``) and clean up (``_cleanup``) a
generation. Runtime consumers import only the names exported here.
"""

from __future__ import annotations

from ._build import build_orca_execution_snapshot
from ._cleanup import cleanup_unowned_orca_execution_snapshot
from ._constants import MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES, ORCA_EXECUTION_SNAPSHOT_VERSION
from ._recovery import recovery_checkpoint_private_name, recovery_checkpoint_source_names
from ._snapshot_identity import (
    orca_execution_provenance,
    orca_execution_snapshot_generation_dir,
    verify_orca_snapshot_executable,
)
from ._verify import orca_execution_started_evidence, verify_orca_execution_snapshot

__all__ = [
    "MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES",
    "ORCA_EXECUTION_SNAPSHOT_VERSION",
    "build_orca_execution_snapshot",
    "cleanup_unowned_orca_execution_snapshot",
    "orca_execution_provenance",
    "orca_execution_snapshot_generation_dir",
    "orca_execution_started_evidence",
    "recovery_checkpoint_private_name",
    "recovery_checkpoint_source_names",
    "verify_orca_execution_snapshot",
    "verify_orca_snapshot_executable",
]
