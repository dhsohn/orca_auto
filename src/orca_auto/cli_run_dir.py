"""``orca_auto run-dir``: pin the target directory and submit it through one inode."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.cli_handlers import CommandConfigError, _configure_orca_logging, command_config_path
from orca_auto.core.config.files import shared_runs_root_from_config
from orca_auto.core.utils import normalize_text
from orca_auto.orca.run_dir_guard import (
    _pinned_run_dir_target,
    _RunDirPublicationContract,
    _RunDirTargetChangedError,
    use_run_dir_publication_guard,
    validate_production_run_dir_target,
)
from orca_auto.terminal import emit_error, emit_json


def _require_orca_input(target: Path) -> None:
    if not any(candidate.is_file() for candidate in target.glob("*.inp")):
        raise ValueError("Could not infer run-dir target type: expected an ORCA *.inp file.")


def cmd_run_dir(args: Any) -> int:
    json_output = bool(getattr(args, "json", False))
    try:
        raw_target = normalize_text(getattr(args, "path", None))
        if not raw_target:
            raise ValueError("run-dir requires a target directory path")
        # Keep the caller-visible directory entry as a lexical absolute path.
        # Resolving it would erase the namespace identity that must remain bound
        # to the fd-backed inode for the whole synchronous publication.
        namespace_target = Path(raw_target).expanduser().absolute()
        config_path = command_config_path(args)
        runs_root = shared_runs_root_from_config(config_path) or ""
    except CommandConfigError as exc:
        emit_error(exc, hint=exc.hint, json_output=json_output)
        return 1
    except ValueError as exc:
        emit_error(exc, json_output=json_output)
        return 1

    try:
        # Open first, then check, classify, and synchronously submit through the
        # same fd-backed inode. Namespace replacement cannot swap in a reserved target.
        with _pinned_run_dir_target(raw_target) as pinned_target:
            try:
                if runs_root:
                    validate_production_run_dir_target(raw_target, runs_root)
                    validate_production_run_dir_target(pinned_target, runs_root)
                _require_orca_input(pinned_target)
                pinned_stat = pinned_target.stat()
                publication_contract = _RunDirPublicationContract(
                    pinned_target=pinned_target,
                    namespace_target=namespace_target,
                    runs_root=runs_root,
                    expected_identity=(pinned_stat.st_dev, pinned_stat.st_ino),
                )
                publication_contract("central dispatch")
            except ValueError as exc:
                emit_error(exc, json_output=json_output)
                return 1

            args.path = str(pinned_target)
            with use_run_dir_publication_guard(
                publication_contract,
                pinned_target=pinned_target,
            ):
                from orca_auto.orca.commands.run_inp import cmd_run_inp

                _configure_orca_logging(args)
                args.config = config_path
                # With --log-file the submission logger writes nowhere the operator
                # looks; the terminal error line (and the JSON error document) must
                # not depend on how logging was configured.
                return int(
                    cmd_run_inp(
                        args,
                        report_error=lambda message: emit_error(message, json_output=json_output),
                        emit_json=emit_json,
                    )
                )
    except _RunDirTargetChangedError as exc:
        emit_error(exc, json_output=json_output)
        return 1
