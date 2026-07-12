from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.messaging import Severity

from ._engine_delivery import EngineLineSender, send_job_event


@dataclass(frozen=True)
class EngineNotifier:
    label: str
    engine: str
    send_fn: EngineLineSender

    def send_job_event(
        self,
        cfg: Any,
        *,
        job_dir: Path,
        headline: str,
        severity: Severity,
        fields: list[tuple[str, object]],
        extra_lines: list[str] | None = None,
    ) -> bool:
        return send_job_event(
            cfg,
            label=self.label,
            engine=self.engine,
            job_dir=job_dir,
            headline=headline,
            severity=severity,
            fields=fields,
            send_fn=self.send_fn,
            extra_lines=extra_lines,
        )


def build_engine_notifier(
    *,
    label: str,
    engine: str,
    send_fn: EngineLineSender,
) -> EngineNotifier:
    return EngineNotifier(label=label, engine=engine, send_fn=send_fn)


__all__ = [
    "EngineNotifier",
    "build_engine_notifier",
]
