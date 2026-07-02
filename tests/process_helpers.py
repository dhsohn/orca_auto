from __future__ import annotations

import signal
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch


@dataclass
class FakeManagedProcess:
    pid: int = 12345
    poll_result: int | None = None
    wait_side_effects: list[Any] = field(default_factory=list)
    terminate_error: Exception | None = None
    kill_error: Exception | None = None
    terminate_calls: int = 0
    kill_calls: int = 0
    wait_calls: list[float | None] = field(default_factory=list)
    sent_signals: list[int] = field(default_factory=list)

    def poll(self) -> int | None:
        return self.poll_result

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error

    def kill(self) -> None:
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error

    def send_signal(self, signum: int) -> None:
        self.sent_signals.append(signum)

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if not self.wait_side_effects:
            return 0
        effect = self.wait_side_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return int(effect)


@dataclass
class SignalRegistry:
    default_handler: Any = signal.SIG_DFL
    handlers: dict[int, Any] = field(default_factory=dict)

    def signal(self, sig: int, handler: Any) -> Any:
        previous = self.getsignal(sig)
        self.handlers[sig] = handler
        return previous

    def getsignal(self, sig: int) -> Any:
        return self.handlers.get(sig, self.default_handler)

    def fire(self, sig: int, frame: object | None = None) -> None:
        handler = self.getsignal(sig)
        if callable(handler):
            handler(sig, frame)


def missing_process_group(*_args: Any, **_kwargs: Any) -> None:
    raise ProcessLookupError("missing process group")


def recording_killpg(
    *,
    side_effects: Sequence[Any] = (),
) -> tuple[Callable[[int, int], None], list[tuple[int, int]]]:
    calls: list[tuple[int, int]] = []
    pending_effects = list(side_effects)

    def killpg(pid: int, signum: int) -> None:
        calls.append((pid, signum))
        if not pending_effects:
            return
        effect = pending_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        if callable(effect):
            effect(pid, signum)

    return killpg, calls


@contextmanager
def preserved_signal_handlers(*signals: signal.Signals) -> Iterator[None]:
    saved = {sig: signal.getsignal(sig) for sig in signals}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def patch_missing_process_group(target: str) -> Any:
    return patch(target, side_effect=missing_process_group)
