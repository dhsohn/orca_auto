from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.engine_catalog import get_engine_catalog_entry


def runtime_roots_for_cfg(cfg: Any, *, engine: str) -> tuple[Path, ...]:
    get_engine_catalog_entry(engine)
    return (Path(cfg.runtime.allowed_root).expanduser().resolve(),)


__all__ = ["runtime_roots_for_cfg"]
