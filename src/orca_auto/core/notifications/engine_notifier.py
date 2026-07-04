from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._engine_delivery import send_job_event


@dataclass(frozen=True)
class EngineNotifier:
    label: str
    engine: str
    send_fn: Callable[[Any, list[str]], bool]

    def send_job_event(
        self,
        cfg: Any,
        *,
        job_dir: Path,
        headline: str,
        fields: list[tuple[str, object]],
        extra_lines: list[str] | None = None,
    ) -> bool:
        return send_job_event(
            cfg,
            label=self.label,
            engine=self.engine,
            job_dir=job_dir,
            headline=headline,
            fields=fields,
            send_fn=self.send_fn,
            extra_lines=extra_lines,
        )


def build_engine_notifier(
    *,
    label: str,
    engine: str,
    send_fn: Callable[[Any, list[str]], bool],
) -> EngineNotifier:
    return EngineNotifier(label=label, engine=engine, send_fn=send_fn)


__all__ = [
    "EngineNotifier",
    "build_engine_notifier",
]
