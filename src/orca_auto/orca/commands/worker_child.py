"""Child job entry point spawned by the ORCA queue worker for one queue entry."""

from __future__ import annotations

import argparse

from ..cli_logging import configure_logging
from ..worker_execution import WORKER_JOB_MODULE, run_worker_child_job


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"python -m {WORKER_JOB_MODULE}")
    parser.add_argument("--config", required=True)
    parser.add_argument("--queue-root", required=True)
    parser.add_argument("--queue-id", required=True)
    parser.add_argument("--admission-token", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args)
    return run_worker_child_job(
        config_path=args.config,
        queue_root=args.queue_root,
        queue_id=args.queue_id,
        admission_token=str(args.admission_token).strip() or None,
    )


__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
