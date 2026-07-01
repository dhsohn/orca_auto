from __future__ import annotations

from enum import Enum


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"


class AnalyzerStatus(str, Enum):
    COMPLETED = "completed"
    ERROR_SCF = "error_scf"
    ERROR_SCFGRAD_ABORT = "error_scfgrad_abort"
    ERROR_MULTIPLICITY_IMPOSSIBLE = "error_multiplicity_impossible"
    ERROR_DISK_IO = "error_disk_io"
    ERROR_MEMORY = "error_memory"
    ERROR_GEOMETRY = "error_geometry"
    GEOM_NOT_CONVERGED = "geom_not_converged"
    TS_NOT_FOUND = "ts_not_found"
    INCOMPLETE = "incomplete"
    UNKNOWN_FAILURE = "unknown_failure"
