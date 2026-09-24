"""RAM (tmpfs) scratch workspaces for engine attempts.

The package stages the durable input closure into a private workspace
below ``/dev/shm``, lets the engine run there, and publishes the outputs
back into the durable generation through a crash-recoverable journal.
Submodules are layered strictly in this order: ``_errors`` <- ``_constants``
<- ``_policy`` <- ``_reports`` <- ``_fs`` <- ``_manifest`` <- ``_staging``
<- ``_publication`` <- ``_workspace`` <- ``_inspect``. This module re-exports
the public surface; callers never import the submodules.
"""

from __future__ import annotations

from ._constants import (
    SCRATCH_REMOVABLE_STATES,
    SCRATCH_RUNTIME_HOME_DIR_NAME,
    SCRATCH_STATE_INVALID_MANIFEST,
    SCRATCH_STATE_LIVE,
    SCRATCH_STATE_STALE,
    SCRATCH_STATE_TOMBSTONE,
    SCRATCH_STATE_UNSAFE,
    SCRATCH_STATE_UNVERIFIABLE,
)
from ._errors import (
    EngineScratchCapacityError,
    EngineScratchError,
)

# Private fd-pinned readers re-exported for ``tests/core/test_pinned_readonly.py``,
# which drives them through the package name.
from ._fs import _read_stable_regular_file_at as _read_stable_regular_file_at
from ._fs import _regular_file_sha256_at as _regular_file_sha256_at
from ._inspect import (
    durable_publication_journal_status,
    inspect_scratch_root,
    remove_scratch_workspace,
)
from ._policy import EngineScratchPolicy
from ._publication import (
    ScratchPublication,
    attach_scratch_provenance_mapping_to_exception,
    attach_scratch_provenance_to_exception,
    is_transient_scratch_file,
    scratch_provenance_from_exception,
    scratch_publication_provenance,
)
from ._publication import _copy_artifact_to_staging as _copy_artifact_to_staging
from ._reports import (
    PublicationJournalStatus,
    ScratchWorkspaceRemoval,
    ScratchWorkspaceReport,
)
from ._workspace import EngineScratchWorkspace

__all__ = [
    "EngineScratchCapacityError",
    "EngineScratchError",
    "EngineScratchPolicy",
    "EngineScratchWorkspace",
    "PublicationJournalStatus",
    "SCRATCH_REMOVABLE_STATES",
    "SCRATCH_RUNTIME_HOME_DIR_NAME",
    "SCRATCH_STATE_INVALID_MANIFEST",
    "SCRATCH_STATE_LIVE",
    "SCRATCH_STATE_STALE",
    "SCRATCH_STATE_TOMBSTONE",
    "SCRATCH_STATE_UNSAFE",
    "SCRATCH_STATE_UNVERIFIABLE",
    "ScratchPublication",
    "ScratchWorkspaceRemoval",
    "ScratchWorkspaceReport",
    "attach_scratch_provenance_mapping_to_exception",
    "attach_scratch_provenance_to_exception",
    "durable_publication_journal_status",
    "inspect_scratch_root",
    "is_transient_scratch_file",
    "remove_scratch_workspace",
    "scratch_provenance_from_exception",
    "scratch_publication_provenance",
]
