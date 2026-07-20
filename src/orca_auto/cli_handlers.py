from __future__ import annotations

import argparse
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.cli_common import (
    _configure_orca_logging,
    _engine_config_for_command,
)
from orca_auto.cli_errors import emit_error
from orca_auto.core.commands.run_dir import (
    use_run_dir_publication_guard,
    validate_production_run_dir_target,
)
from orca_auto.core.config.files import shared_workflow_root_from_config
from orca_auto.core.utils import normalize_text
from orca_auto.flow.run_dir.layout import inspect_workflow_run_dir


def cmd_init(args: argparse.Namespace) -> int:
    from orca_auto.orca.commands.init import cmd_init as _cmd_orca_init

    _configure_orca_logging(args)
    args.config = _engine_config_for_command(args)
    return int(_cmd_orca_init(args))


def cmd_orca_run_dir(args: argparse.Namespace) -> int:
    from orca_auto.orca.commands.run_inp import cmd_run_inp as _cmd_orca_run_dir

    _configure_orca_logging(args)
    args.config = _engine_config_for_command(args)
    return int(_cmd_orca_run_dir(args))


def cmd_workflow_scaffold(args: argparse.Namespace) -> int:
    from orca_auto.flow.scaffold import cmd_scaffold as _cmd_workflow_scaffold

    return int(_cmd_workflow_scaffold(args))


def _resolved_run_dir_target(args: argparse.Namespace) -> Path:
    raw_path = normalize_text(getattr(args, "path", None))
    if not raw_path:
        raise ValueError("run-dir requires a target directory path")

    try:
        target = Path(raw_path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"run-dir target could not be resolved safely: {raw_path}") from exc
    if not target.exists():
        raise ValueError(f"run-dir target not found: {target}")
    if not target.is_dir():
        raise ValueError(f"run-dir target is not a directory: {target}")
    return target


def _detect_run_dir_app(args: argparse.Namespace, *, target: Path | None = None) -> str:
    target = target or _resolved_run_dir_target(args)

    workflow_layout = inspect_workflow_run_dir(target)
    markers = {
        "workflow": (target / "workflow.json").is_file() or workflow_layout.has_manifest,
        "orca": any(candidate.is_file() for candidate in target.glob("*.inp")),
    }
    # Workflow inputs legitimately contain engine-specific ``*.inp`` files, so
    # a workflow manifest remains authoritative over a bare ORCA input.
    if markers["workflow"]:
        return "workflow"
    if markers["orca"]:
        return "orca"

    raise ValueError(
        "Could not infer run-dir target type from directory. "
        "Expected flow.yaml/workflow.json for a workflow, or *.inp for ORCA."
    )


def _configured_runs_root_for_run_dir(args: Any) -> str:
    config_path = _engine_config_for_command(args)
    return shared_workflow_root_from_config(config_path) or ""


class _RunDirTargetChangedError(ValueError):
    pass


@dataclass(frozen=True)
class _RunDirPublicationContract:
    pinned_target: Path
    namespace_target: Path
    runs_root: str
    expected_identity: tuple[int, int]

    def __call__(self, stage: str) -> None:
        try:
            path_stat = self.pinned_target.stat()
        except OSError as exc:
            raise _RunDirTargetChangedError(
                f"run-dir target became unavailable before {stage}"
            ) from exc
        if (path_stat.st_dev, path_stat.st_ino) != self.expected_identity:
            raise _RunDirTargetChangedError(f"run-dir target identity changed before {stage}")
        try:
            namespace_stat = self.namespace_target.stat()
        except OSError as exc:
            raise _RunDirTargetChangedError(
                f"run-dir namespace target became unavailable before {stage}"
            ) from exc
        if (namespace_stat.st_dev, namespace_stat.st_ino) != self.expected_identity:
            raise _RunDirTargetChangedError(
                f"run-dir namespace target identity changed before {stage}"
            )
        if not self.runs_root:
            return
        try:
            validate_production_run_dir_target(self.pinned_target, self.runs_root)
        except ValueError as exc:
            raise _RunDirTargetChangedError(
                f"run-dir publication guard rejected the target before {stage}: {exc}"
            ) from exc


@contextmanager
def _pinned_run_dir_target(raw_target: str | Path) -> Iterator[Path]:
    """Yield one inode for classification, policy checks, and dispatch."""

    target = Path(raw_target).expanduser()
    flags = os.O_RDONLY | os.O_DIRECTORY
    try:
        directory_fd = os.open(target, flags)
    except FileNotFoundError as exc:
        raise _RunDirTargetChangedError(
            f"run-dir target not found: {target.resolve(strict=False)}"
        ) from exc
    except NotADirectoryError as exc:
        raise _RunDirTargetChangedError(
            f"run-dir target is not a directory: {target.resolve(strict=False)}"
        ) from exc
    except OSError as exc:
        raise _RunDirTargetChangedError(
            f"run-dir target could not be opened safely: {target}"
        ) from exc

    try:
        try:
            opened_stat = os.fstat(directory_fd)
            if not stat.S_ISDIR(opened_stat.st_mode):
                raise OSError("opened run-dir target is not a directory")
            pinned_target = Path("/proc/self/fd") / str(directory_fd)
            pinned_stat = pinned_target.stat()
            if (pinned_stat.st_dev, pinned_stat.st_ino) != (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ):
                raise OSError("fd path does not identify the opened run-dir target")
        except OSError as exc:
            raise _RunDirTargetChangedError(
                f"run-dir target could not be pinned safely: {target}"
            ) from exc
        yield pinned_target
    finally:
        os.close(directory_fd)


def cmd_run_dir(args: Any) -> int:
    try:
        raw_target = normalize_text(getattr(args, "path", None))
        if not raw_target:
            raise ValueError("run-dir requires a target directory path")
        # Keep the caller-visible directory entry as a lexical absolute path.
        # Resolving it would erase the namespace identity that must remain bound
        # to the fd-backed inode for the whole synchronous publication.
        namespace_target = Path(raw_target).expanduser().absolute()
        runs_root = _configured_runs_root_for_run_dir(args)
    except ValueError as exc:
        emit_error(exc)
        return 1

    try:
        # Open first, then check, classify, and synchronously submit through the
        # same fd-backed inode. Namespace replacement cannot swap in a reserved target.
        with _pinned_run_dir_target(raw_target) as pinned_target:
            try:
                if runs_root:
                    validate_production_run_dir_target(raw_target, runs_root)
                    validate_production_run_dir_target(pinned_target, runs_root)
                run_dir_app = _detect_run_dir_app(args, target=pinned_target)
                pinned_stat = pinned_target.stat()
                publication_contract = _RunDirPublicationContract(
                    pinned_target=pinned_target,
                    namespace_target=namespace_target,
                    runs_root=runs_root,
                    expected_identity=(pinned_stat.st_dev, pinned_stat.st_ino),
                )
                publication_contract("central dispatch")
            except ValueError as exc:
                emit_error(exc)
                return 1

            args.path = str(pinned_target)
            args.run_dir_app = run_dir_app
            with use_run_dir_publication_guard(
                publication_contract,
                pinned_target=pinned_target,
            ):
                if run_dir_app == "workflow":
                    args.workflow_dir = args.path
                    return int(cmd_workflow_run_dir(args))
                if (
                    getattr(args, "max_cores", None) is not None
                    or getattr(args, "max_memory_gb", None) is not None
                ):
                    emit_error(
                        "ORCA run-dir does not support --max-cores or --max-memory-gb. "
                        "Edit %pal/%maxcore in the selected .inp, or use a workflow run-dir."
                    )
                    return 1
                if getattr(args, "priority", None) is None:
                    args.priority = 10
                return int(cmd_orca_run_dir(args))
    except _RunDirTargetChangedError as exc:
        emit_error(exc)
        return 1


def cmd_workflow_run_dir(args: argparse.Namespace) -> int:
    from orca_auto.flow.cli.run_dir import cmd_run_dir as _cmd_workflow_run_dir

    shared_config = _engine_config_for_command(args)
    if shared_config:
        args.orca_auto_config = shared_config
    return int(_cmd_workflow_run_dir(args))


def cmd_orca_monitor(args: argparse.Namespace) -> int:
    from orca_auto.orca.commands.monitor import cmd_monitor as _cmd_orca_monitor

    _configure_orca_logging(args)
    args.config = _engine_config_for_command(args)
    return int(_cmd_orca_monitor(args))
