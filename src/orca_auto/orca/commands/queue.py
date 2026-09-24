"""Parent queue worker entry point: ``python -m orca_auto.orca.commands.queue --config X``."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from ..config import load_config
from ..engine import read_worker_pid
from ..queue.worker import OrcaQueueWorker

logger = logging.getLogger(__name__)

QUEUE_WORKER_MODULE = "orca_auto.orca.commands.queue"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"python -m {QUEUE_WORKER_MODULE}")
    parser.add_argument("--config", required=True)
    return parser


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


__all__ = ["QUEUE_WORKER_MODULE", "build_parser", "cmd_queue_worker", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
