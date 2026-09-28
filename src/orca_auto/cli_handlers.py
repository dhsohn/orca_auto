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
    validated_runs_root_text,
)
from orca_auto.orca.config import (
    OrcaConfigSections,
    load_orca_shared_config,
    missing_config_error,
)
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
        _path, shared, orca_sections = load_orca_shared_config(
            config_path, missing_error=missing_config_error
        )
    except YAML_CONFIG_LOAD_EXCEPTIONS as exc:
        # A missing or damaged config names its own failure instead of reading
        # as "not configured"; a missing one also says how to create it.
        raise CommandConfigError(
            str(exc),
            hint="Check the config path and repair the reported state file before retrying.",
        ) from exc
    invalid = f"runs_root is missing or invalid in {config_path}"
    root_hint = "Set runs_root to an absolute directory path in the config."
    if not shared.runs_root:
        raise CommandConfigError(invalid, hint=root_hint)
    try:
        runs_root = Path(validated_runs_root_text(shared.runs_root)).expanduser().resolve()
    except ValueError as exc:
        # The validator names the rule the value breaks, such as an absolute Linux path.
        raise CommandConfigError(f"{invalid}: {exc}", hint=root_hint) from None
    if not runs_root.is_dir():
        # A typo here would otherwise read as an empty queue or index.
        problem = "is not a directory" if runs_root.exists() else "does not exist"
        raise CommandConfigError(
            f"runs_root {problem}: {runs_root}",
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
