from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChildWorkerShutdownController:
    requested: bool = False

    def request(self) -> None:
        self.requested = True

    def is_requested(self) -> bool:
        return self.requested


__all__ = ["ChildWorkerShutdownController"]
