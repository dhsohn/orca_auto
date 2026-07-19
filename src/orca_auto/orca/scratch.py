from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from orca_auto.core.engine_scratch import (
    EngineScratchError,
    EngineScratchPolicy,
    EngineScratchWorkspace,
    ScratchPublication,
    attach_scratch_provenance_mapping_to_exception,
    attach_scratch_provenance_to_exception,
    scratch_provenance_from_exception,
    scratch_publication_provenance,
)

from .input_blocks import scan_orca_file_references


def _orca_dependency_names(payload: bytes) -> Sequence[str]:
    try:
        lines = payload.decode("utf-8", errors="strict").splitlines()
    except UnicodeError as exc:
        raise EngineScratchError("ORCA input is not UTF-8 text") from exc
    return tuple(reference.value for reference in scan_orca_file_references(lines))


class OrcaScratchPolicy(EngineScratchPolicy):
    def __init__(
        self,
        *,
        root: Path,
        min_free_bytes: int,
        max_task_memory_bytes: int,
    ) -> None:
        super().__init__(
            root=root,
            min_free_bytes=min_free_bytes,
            max_task_memory_bytes=max_task_memory_bytes,
            dependency_names_from_primary=_orca_dependency_names,
            normalize_primary_newline=True,
        )


OrcaScratchError = EngineScratchError
OrcaScratchWorkspace = EngineScratchWorkspace


def is_transient_orca_scratch_file(name: str) -> bool:
    from orca_auto.core.engine_scratch import is_transient_orca_scratch_file as classify

    return classify(name)


__all__ = [
    "OrcaScratchError",
    "OrcaScratchPolicy",
    "OrcaScratchWorkspace",
    "ScratchPublication",
    "attach_scratch_provenance_mapping_to_exception",
    "attach_scratch_provenance_to_exception",
    "is_transient_orca_scratch_file",
    "scratch_provenance_from_exception",
    "scratch_publication_provenance",
]
