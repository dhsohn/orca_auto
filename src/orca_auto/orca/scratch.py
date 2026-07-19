from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from orca_auto.core.engine_scratch import (
    EngineScratchError,
    EngineScratchPolicy,
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


__all__ = [
    "OrcaScratchPolicy",
]
