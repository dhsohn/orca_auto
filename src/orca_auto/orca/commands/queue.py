"""CLI commands for the queue subsystem."""

from __future__ import annotations

import argparse
import logging
from typing import Any

from ..config import load_config
from ..queue import worker as _queue_worker_runtime
from ..queue.worker import QueueWorker, read_worker_pid

logger = logging.getLogger(__name__)


# -- Subcommands ----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orca_auto.orca.commands.queue")
    parser.add_argument("--config", required=True)
    return parser


def cmd_queue_worker(args: Any) -> int:
    return _queue_worker_runtime._queue_module.run_pidfile_worker_command(
        args,
        load_config_fn=load_config,
        config_path_fn=lambda worker_args: str(worker_args.config),
        read_worker_pid_fn=read_worker_pid,
        existing_pid_report_fn=_log_existing_worker,
        max_concurrent_fn=lambda cfg: max(1, int(cfg.runtime.max_concurrent)),
        worker_factory=lambda cfg, config_path, **kwargs: QueueWorker(
            cfg,
            config_path,
            **kwargs,
        ),
    )


def _log_existing_worker(pid: int) -> None:
    logger.error(
        "Worker already running (pid=%d). Check the active systemd service.",
        pid,
    )


def main(argv: list[str] | None = None) -> int:
    return cmd_queue_worker(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
