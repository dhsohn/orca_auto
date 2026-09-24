from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from orca_auto.orca.cli_logging import remove_managed_handlers


class _BrokenPipeGuardedStdout:
    """Discard writes only after the real stdout pipe reports closure."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.broken = False

    def write(self, text: str) -> int:
        if self.broken:
            return len(text)
        try:
            return int(self._stream.write(text))
        except BrokenPipeError:
            self.broken = True
            return len(text)

    def flush(self) -> None:
        if self.broken:
            return
        try:
            self._stream.flush()
        except BrokenPipeError:
            self.broken = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _silence_broken_stdout() -> None:
    """Redirect a closed stdout pipe so interpreter shutdown cannot fail again."""

    try:
        stdout_fd = sys.stdout.fileno()
        null_fd = os.open(os.devnull, os.O_WRONLY)
    except (AttributeError, OSError, ValueError):
        return
    try:
        os.dup2(null_fd, stdout_fd)
    except OSError:
        pass
    finally:
        os.close(null_fd)


def build_parser() -> argparse.ArgumentParser:
    from orca_auto.cli_parsers import build_parser as _build_parser

    return _build_parser()


def dispatches_queue_worker(args: argparse.Namespace) -> bool:
    """Whether the parsed command line runs the ``queue worker`` supervisor."""
    from orca_auto.cli_workers import cmd_queue_worker

    return getattr(args, "func", None) is cmd_queue_worker


def main(
    argv: list[str] | None = None,
    *,
    before_queue_worker: Callable[[], None] | None = None,
) -> int:
    """Parse and dispatch one command.

    ``before_queue_worker`` runs after parsing and only when the parsed command
    dispatches to the queue worker. The module entry point uses it to publish
    process evidence before the worker starts, bound to the parser's own
    dispatch rather than to a raw ``sys.argv`` prefix.
    """
    from orca_auto.core import terminal

    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "no_color", False):
        terminal.set_color_override(False)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    if before_queue_worker is not None and dispatches_queue_worker(args):
        before_queue_worker()

    original_stdout = sys.stdout
    guarded_stdout = _BrokenPipeGuardedStdout(original_stdout)
    sys.stdout = guarded_stdout
    try:
        result = int(args.func(args))
        # Text streams are block-buffered on a pipe. A short command can finish
        # rendering without observing the closed reader until interpreter
        # shutdown, which would otherwise replace a handled result with exit 120.
        guarded_stdout.flush()
    finally:
        sys.stdout = original_stdout
        # A command that configured file/stream logging must not leave its
        # handler on the root logger: the stream it captured may be closed by
        # the time an in-process caller runs the next command.
        remove_managed_handlers(logging.getLogger())
        if guarded_stdout.broken:
            _silence_broken_stdout()
    return result


if __name__ == "__main__":
    from orca_auto._process_evidence import exec_with_import_source_evidence

    raise SystemExit(main(before_queue_worker=exec_with_import_source_evidence))
