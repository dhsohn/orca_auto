from __future__ import annotations

import argparse

QUEUE_WORKER_MODULE = "orca_auto.core.engines.queue_worker"


def run_engine_queue_worker(engine: str, argv: list[str]) -> int:
    from .registry import get_engine_definition

    definition = get_engine_definition(engine)
    return definition.queue_worker_main(argv)


def build_engine_queue_worker_parser(prog: str) -> argparse.ArgumentParser:
    """The argv contract every engine's parent queue worker is invoked with."""

    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--config", required=True)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = build_engine_queue_worker_parser(f"python -m {QUEUE_WORKER_MODULE}")
    parser.add_argument("--engine", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, remainder = parser.parse_known_args(argv)
    forwarded = ["--config", args.config, *remainder]
    return run_engine_queue_worker(str(args.engine).strip().lower(), forwarded)


__all__ = [
    "QUEUE_WORKER_MODULE",
    "build_engine_queue_worker_parser",
    "build_parser",
    "main",
    "run_engine_queue_worker",
]


if __name__ == "__main__":
    raise SystemExit(main())
