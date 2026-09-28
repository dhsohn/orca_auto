"""Top-level command handlers: ``init`` and the config every runs_root command reads.

``resolve_command_config`` is the one answer to which config and runs_root a
command reads and what it says when either is missing. ``run-dir`` lives in
``cli_run_dir`` and ``index prune|rebuild`` in ``cli_index``.

Each handler composes the domain packages and owns the operator-facing
surface: ``error:`` lines on stderr, the shared ``emit_json`` document under
``--json``, and the exit-code rule (0 success or nothing to do, 1 refused or
failed, 2 argparse usage).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from orca_auto.core.config.files import (
    YAML_CONFIG_LOAD_EXCEPTIONS,
    SharedConfig,
    discover_shared_config_path,
    usable_runs_root_text,
)
from orca_auto.orca.config import OrcaConfigSections, load_orca_shared_config
from orca_auto.terminal import emit_error


class CommandConfigError(Exception):
    """The config or runs_root a command reads is missing or unusable."""

    def __init__(self, message: str, *, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class CommandConfig:
    """The one config a command reads, loaded once, and its existing runs_root."""

    path: str
    runs_root: Path
    shared: SharedConfig
    orca_sections: OrcaConfigSections


def command_config_path(args: argparse.Namespace) -> str:
    """The config a command reads: ``--config``, ``ORCA_AUTO_CONFIG``, then the home default."""
    config_path = discover_shared_config_path(getattr(args, "config", None))
    if not config_path:
        raise CommandConfigError(
            "No orca_auto.yaml found: pass --config, set ORCA_AUTO_CONFIG, "
            "or create ~/orca_auto/config/orca_auto.yaml.",
            hint="Run `orca_auto init` to create it.",
        )
    return config_path


def resolve_command_config(args: argparse.Namespace) -> CommandConfig:
    """Discover, load and check the config every runs_root command reads.

    A missing config, a config that does not load, a missing or invalid
    runs_root and a runs_root that is not a directory each raise one message;
    nothing is created for a missing root.
    """
    config_path = command_config_path(args)
    try:
        _path, shared, orca_sections = load_orca_shared_config(config_path)
    except YAML_CONFIG_LOAD_EXCEPTIONS as exc:
        # A missing or damaged config names its own failure instead of reading
        # as "not configured".
        raise CommandConfigError(
            str(exc),
            hint="Check the config path and repair the reported state file before retrying.",
        ) from exc
    root_text = usable_runs_root_text(shared.runs_root)
    if not root_text:
        raise CommandConfigError(
            f"runs_root is missing or invalid in {config_path}",
            hint="Set runs_root to an absolute directory path in the config.",
        )
    runs_root = Path(root_text).expanduser().resolve()
    if not runs_root.is_dir():
        # A typo here would otherwise read as an empty queue or index.
        raise CommandConfigError(
            f"runs_root does not exist: {runs_root}",
            hint="Check runs_root in the config; a missing root is never created.",
        )
    return CommandConfig(
        path=config_path, runs_root=runs_root, shared=shared, orca_sections=orca_sections
    )


def _configure_orca_logging(args: argparse.Namespace) -> None:
    from orca_auto.orca.cli_logging import configure_logging

    configure_logging(
        argparse.Namespace(
            verbose=bool(getattr(args, "verbose", False)),
            log_file=getattr(args, "log_file", None),
        )
    )


def cmd_init(args: argparse.Namespace) -> int:
    from orca_auto.orca.commands.init import cmd_init as _cmd_orca_init

    _configure_orca_logging(args)
    args.config = discover_shared_config_path(getattr(args, "config", None))
    # The domain package cannot import the terminal layer; hand it the shared
    # stderr error line so its refusals render like every other command's.
    return int(_cmd_orca_init(args, report_error=emit_error))
