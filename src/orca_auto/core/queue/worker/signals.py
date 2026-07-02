from __future__ import annotations

import logging
import signal
from collections.abc import Callable

from ..processes import (
    ShutdownSignalDeps,
)
from ..processes import (
    install_shutdown_signal_handlers as _install_shutdown_signal_handlers,
)

LOGGER = logging.getLogger("orca_auto.core.queue.worker.process")


def install_shutdown_signal_handlers(request_shutdown: Callable[[], None]) -> None:
    _install_shutdown_signal_handlers(
        request_shutdown,
        deps=ShutdownSignalDeps(
            signal_fn=signal.signal,
            sigterm=signal.SIGTERM,
            sigint=signal.SIGINT,
            logger=LOGGER,
        ),
    )


__all__ = ["install_shutdown_signal_handlers"]
