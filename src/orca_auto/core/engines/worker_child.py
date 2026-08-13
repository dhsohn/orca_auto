from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

WORKER_CHILD_MODULE = "orca_auto.core.engines.worker_child"


@dataclass(frozen=True)
class EngineWorkerChild:
    engine: str

    def run(
        self,
        *,
        config_path: str,
        queue_root: str | Path,
        queue_id: str,
        admission_token: str | None = None,
    ) -> int:
        return run_engine_worker_child_job(
            engine=self.engine,
            config_path=config_path,
            queue_root=queue_root,
            queue_id=queue_id,
            admission_token=admission_token,
        )


def build_worker_child_command(
    *,
    engine: str,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    admission_token: str | None = None,
    admission_root: str | Path | None = None,
) -> list[str]:
    del admission_root
    command = [
        sys.executable,
        "-m",
        WORKER_CHILD_MODULE,
        "--engine",
        str(engine).strip().lower(),
        "--config",
        str(config_path),
        "--queue-root",
        str(queue_root),
        "--queue-id",
        str(queue_id),
    ]
    if admission_token:
        command.extend(["--admission-token", str(admission_token)])
    return command


def build_worker_child_command_for_engine(engine: str) -> Callable[..., list[str]]:
    engine_id = str(engine).strip().lower()

    def build_engine_worker_child_command(
        *,
        config_path: str,
        queue_root: str | Path,
        queue_id: str,
        admission_token: str | None = None,
        admission_root: str | Path | None = None,
    ) -> list[str]:
        return build_worker_child_command(
            engine=engine_id,
            config_path=config_path,
            queue_root=queue_root,
            queue_id=queue_id,
            admission_token=admission_token,
            admission_root=admission_root,
        )

    return build_engine_worker_child_command


def run_engine_worker_child_job(
    *,
    engine: str,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    admission_token: str | None = None,
) -> int:
    from .registry import get_engine_definition

    definition = get_engine_definition(engine)
    return definition.worker_child_main(
        config_path=config_path,
        queue_root=queue_root,
        queue_id=queue_id,
        admission_token=admission_token,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"python -m {WORKER_CHILD_MODULE}")
    parser.add_argument("--engine", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--queue-root", required=True)
    parser.add_argument("--queue-id", required=True)
    parser.add_argument("--admission-token", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_engine_worker_child_job(
        engine=args.engine,
        config_path=args.config,
        queue_root=args.queue_root,
        queue_id=args.queue_id,
        admission_token=str(args.admission_token).strip() or None,
    )


__all__ = [
    "EngineWorkerChild",
    "WORKER_CHILD_MODULE",
    "build_parser",
    "build_worker_child_command",
    "build_worker_child_command_for_engine",
    "main",
    "run_engine_worker_child_job",
]


if __name__ == "__main__":
    raise SystemExit(main())
