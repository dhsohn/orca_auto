from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.paths.validation import validated_absolute_linux_path_text

from .schema import as_nonempty_str, explicit_positive_int


@dataclass(frozen=True)
class ScratchConfig:
    root: str = ""
    min_free_gb: int = 8

    @property
    def enabled(self) -> bool:
        return bool(self.root)


def scratch_config_from_runtime_mapping(runtime_raw: dict[str, Any]) -> ScratchConfig:
    if "scratch_root" not in runtime_raw:
        if "scratch_min_free_gb" in runtime_raw:
            raise ValueError("orca.runtime.scratch_min_free_gb requires orca.runtime.scratch_root")
        return ScratchConfig()
    root_raw = runtime_raw.get("scratch_root")
    if not isinstance(root_raw, str) or not root_raw.strip():
        raise ValueError("orca.runtime.scratch_root must be a non-empty string when configured")
    root_raw = as_nonempty_str(root_raw, "")
    root = validated_absolute_linux_path_text(
        root_raw,
        field_name="orca.runtime.scratch_root",
    )
    resolved = Path(root).expanduser().resolve(strict=False)
    shm_root = Path("/dev/shm").resolve()
    if resolved == shm_root or not resolved.is_relative_to(shm_root):
        raise ValueError("orca.runtime.scratch_root must be a dedicated directory below /dev/shm")
    min_free_gb = explicit_positive_int(
        runtime_raw.get("scratch_min_free_gb", ScratchConfig.min_free_gb),
        field_name="orca.runtime.scratch_min_free_gb",
    )
    return ScratchConfig(root=str(resolved), min_free_gb=min_free_gb)


__all__ = ["ScratchConfig", "scratch_config_from_runtime_mapping"]
