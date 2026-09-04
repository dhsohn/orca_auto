from __future__ import annotations

import argparse
import os
import sys


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


def main(argv: list[str] | None = None) -> int:
    from orca_auto import cli_style

    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "no_color", False):
        cli_style.set_color_override(False)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    result = int(args.func(args))
    try:
        # Text streams are block-buffered on a pipe. A short command can finish
        # rendering without observing the closed reader until interpreter
        # shutdown, which would otherwise replace a handled result with exit 120.
        sys.stdout.flush()
    except BrokenPipeError:
        _silence_broken_stdout()
    return result


if __name__ == "__main__":
    from orca_auto._process_evidence import exec_with_import_source_evidence

    exec_with_import_source_evidence()
    raise SystemExit(main())
