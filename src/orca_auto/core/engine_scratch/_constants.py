"""Names, prefixes, patterns, limits and operator-facing states shared by the
scratch submodules: the manifest and lock file names, the publication temp,
backup and tombstone prefixes with their matching regexes, the reserved
durable names, copy and walk budgets, and the ``SCRATCH_STATE_*`` values.
This module holds data only; ``_SCRATCH_ROOT_PARENT`` lives in ``_policy``
because it is looked up at call time by the root preparation there.
"""

from __future__ import annotations

import re

from orca_auto.core.artifacts import (
    RUN_REPORT_HTML_FILE,
    RUN_REPORT_JSON_FILE,
    RUN_STATE_FILE,
    SI_BLOCK_MD_FILE,
)
from orca_auto.core.utils.process_tracking import RUN_LOCK_FILE_NAME

SCRATCH_MANIFEST_FILE_NAME = ".orca_auto_scratch.json"
SCRATCH_RUNTIME_HOME_DIR_NAME = ".orca_auto_scratch_runtime_home"
SCRATCH_WORKSPACE_PREFIX = "attempt-"


_SCRATCH_ROOT_LOCK_FILE_NAME = ".orca_auto_scratch.lock"
_PUBLICATION_JOURNAL_FILE_NAME = ".orca_auto_scratch_publication.json"
_PUBLICATION_TEMP_PREFIX = ".orca_auto_publish."
_PUBLICATION_BACKUP_PREFIX = ".orca_auto_backup."
_PUBLICATION_META_TEMP_PREFIX = ".orca_auto_publish_meta."
_STAGING_TEMP_PREFIX = ".orca_auto_stage."
_PUBLICATION_TEMP_NAME_RE = re.compile(r"^\.orca_auto_publish\.[0-9a-f]{32}\.tmp$")
_PUBLICATION_BACKUP_NAME_RE = re.compile(r"^\.orca_auto_backup\.[0-9a-f]{32}$")
_CLEANUP_TOMBSTONE_PREFIX = ".orca_auto_cleanup."
_CLEANUP_TOMBSTONE_NAME_RE = re.compile(rf"^{re.escape(_CLEANUP_TOMBSTONE_PREFIX)}[0-9a-f]{{32}}$")
_TRANSIENT_FILE_RE = re.compile(r"(?:^|\.)tmp(?:\.|$)", re.IGNORECASE)
# v2 adds max_task_memory_bytes so concurrent workspaces can be summed into the
# launch guard; older manifests are treated as unresolved ownership.
_WORKSPACE_MANIFEST_SCHEMA_VERSION = 2
# Concurrent attempts wait on the root lock while a peer stages inputs or
# hashes committed publication targets; the default 10 s would fail an
# admissible attempt instead of queueing it.
_SCRATCH_ROOT_LOCK_TIMEOUT_SECONDS = 300.0
_SCRATCH_CONTROL_FILE_NAMES = frozenset(
    {
        SCRATCH_MANIFEST_FILE_NAME,
        SCRATCH_RUNTIME_HOME_DIR_NAME,
    }
)
_DURABLE_RESERVED_FILE_NAMES = frozenset(
    {
        RUN_STATE_FILE,
        RUN_REPORT_JSON_FILE,
        RUN_REPORT_HTML_FILE,
        SI_BLOCK_MD_FILE,
        RUN_LOCK_FILE_NAME,
        ".job_state.mutation.lock",
        _PUBLICATION_JOURNAL_FILE_NAME,
    }
)
_COPY_CHUNK_BYTES = 1024 * 1024
_MANIFEST_MAX_BYTES = 64 * 1024
# Inspection walks a workspace with a bounded entry budget so a runaway engine
# output tree cannot turn `scratch list` into a full tmpfs scan.
_INSPECT_SIZE_WALK_MAX_ENTRIES = 20_000

# Operator-facing workspace states. Only ``live`` counts toward the launch
# guard; every other state blocks new scratch launches until removed.
SCRATCH_STATE_LIVE = "live"
SCRATCH_STATE_STALE = "stale"
SCRATCH_STATE_UNVERIFIABLE = "unverifiable"
SCRATCH_STATE_INVALID_MANIFEST = "invalid-manifest"
# An ``attempt-*`` entry that is not a directory (or is a symlink); the sweep
# refuses the whole root and this command cannot remove it either.
SCRATCH_STATE_UNSAFE = "unsafe"
# A rename-for-deletion left by an interrupted cleanup; the next sweep removes it.
SCRATCH_STATE_TOMBSTONE = "tombstone"
SCRATCH_REMOVABLE_STATES: tuple[str, ...] = (
    SCRATCH_STATE_STALE,
    SCRATCH_STATE_INVALID_MANIFEST,
    SCRATCH_STATE_UNVERIFIABLE,
)
