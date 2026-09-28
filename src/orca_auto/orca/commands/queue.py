"""Parent queue worker entry point: ``python -m orca_auto.orca.commands.queue --config X``."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from orca_auto.core.queue.worker.pid_file import read_worker_pid_file

from ..cli_logging import configure_logging
from ..config import AppConfig, load_config
from ..queue.worker import OrcaQueueWorker

logger = logging.getLogger(__name__)

QUEUE_WORKER_MODULE = "orca_auto.orca.commands.queue"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"python -m {QUEUE_WORKER_MODULE}")
    parser.add_argument("--config", required=True)
    return parser


def existing_worker_pid(cfg: AppConfig) -> int | None:
    """The live queue worker that already owns ``cfg``'s runs_root, if any.

    Both ``orca_auto queue worker`` and this entry point refuse to start a
    second worker on it.
    """
    return read_worker_pid_file(Path(cfg.runtime.allowed_root).expanduser().resolve())


def cmd_queue_worker(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    existing_pid = existing_worker_pid(cfg)
    if existing_pid is not None:
        logger.error(
            "Worker already running (pid=%d). Check the active systemd service.",
            existing_pid,
        )
        return 1
    return OrcaQueueWorker(cfg, str(args.config)).run()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args)
    return cmd_queue_worker(args)


__all__ = ["QUEUE_WORKER_MODULE", "build_parser", "cmd_queue_worker", "existing_worker_pid", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
