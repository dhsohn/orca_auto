"""CLI commands for the queue subsystem."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from orca_auto.core.engines.queue_worker import build_engine_queue_worker_parser

from ..config import load_config
from ..engine import read_worker_pid
from ..queue.worker import OrcaQueueWorker

logger = logging.getLogger(__name__)


# -- Subcommands ----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    return build_engine_queue_worker_parser("python -m orca_auto.orca.commands.queue")


def cmd_queue_worker(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
    existing_pid = read_worker_pid(allowed_root)
    if existing_pid is not None:
        logger.error(
            "Worker already running (pid=%d). Check the active systemd service.",
            existing_pid,
        )
        return 1
    worker = OrcaQueueWorker(
        cfg,
        str(args.config),
        max_concurrent=max(1, int(cfg.runtime.max_concurrent)),
    )
    return worker.run()


def main(argv: list[str] | None = None) -> int:
    return cmd_queue_worker(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
