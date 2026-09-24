from __future__ import annotations

from pathlib import Path
from typing import Any


def runtime_roots_for_cfg(cfg: Any) -> tuple[Path, ...]:
    return (Path(cfg.runtime.allowed_root).expanduser().resolve(),)


__all__ = ["runtime_roots_for_cfg"]
