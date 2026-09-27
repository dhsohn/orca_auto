"""Top-level command handlers: ``init``, ``run-dir`` and ``index prune|rebuild``.

``resolve_command_config`` is the one answer to which config and runs_root a
command reads and what it says when either is missing.

Each handler composes the domain packages and owns the operator-facing
surface: ``error:`` lines on stderr, the shared ``emit_json`` document under
``--json``, and the exit-code rule (0 success or nothing to do, 1 refused or
failed, 2 argparse usage).
"""

from __future__ import annotations

import argparse
import os
import stat
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orca_auto.core.config.files import (
    YAML_CONFIG_LOAD_EXCEPTIONS,
    SharedConfig,
    discover_shared_config_path,
    shared_runs_root_from_config,
    usable_runs_root_text,
)
from orca_auto.core.indexing import (
    JobLocationIndexError,
    JobLocationPruneResult,
    JobLocationRecord,
    prune_job_locations,
)
from orca_auto.core.utils import normalize_text
from orca_auto.orca.config import OrcaConfigSections, load_orca_shared_config
from orca_auto.orca.run_dir_guard import (
    use_run_dir_publication_guard,
    validate_production_run_dir_target,
)
from orca_auto.terminal import emit_error, emit_json, label, status_text

if TYPE_CHECKING:
    from orca_auto.orca.job_locations import JobLocationRebuildResult


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


def _require_orca_input(target: Path) -> None:
    if not any(candidate.is_file() for candidate in target.glob("*.inp")):
        raise ValueError("Could not infer run-dir target type: expected an ORCA *.inp file.")


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
    json_output = bool(getattr(args, "json", False))
    try:
        raw_target = normalize_text(getattr(args, "path", None))
        if not raw_target:
            raise ValueError("run-dir requires a target directory path")
        # Keep the caller-visible directory entry as a lexical absolute path.
        # Resolving it would erase the namespace identity that must remain bound
        # to the fd-backed inode for the whole synchronous publication.
        namespace_target = Path(raw_target).expanduser().absolute()
        config_path = discover_shared_config_path(getattr(args, "config", None))
        runs_root = shared_runs_root_from_config(config_path) or ""
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


def _index_prune_payload(result: JobLocationPruneResult) -> dict[str, Any]:
    return {
        "index_path": result.index_path,
        "total": result.total,
        "pruned_count": len(result.pruned),
        "applied": result.applied,
        "pruned": [_index_row_payload(record) for record in result.pruned],
    }


def _emit_index_prune(result: JobLocationPruneResult, *, json_output: bool) -> int:
    if json_output:
        emit_json(_index_prune_payload(result))
        return 0

    print(f"{label('index:')} {result.index_path}")
    print(f"{label('rows:')} {result.total}")
    count_label = "pruned:" if result.applied else "prunable:"
    print(f"{label(count_label)} {len(result.pruned)}")
    if result.pruned:
        # Rows still labelled running/queued are the ones an operator should
        # notice before --apply; the per-status counts make them visible even
        # when the listing is long.
        counts = Counter(record.status or "-" for record in result.pruned)
        summary = ", ".join(f"{status} {count}" for status, count in sorted(counts.items()))
        print(f"{label('by status:')} {summary}")
    for record in result.pruned:
        shown = record.latest_known_path or record.original_run_dir or record.selected_input_xyz
        print(f"  - {record.job_id} {status_text(record.status)} {shown}")
    if not result.pruned:
        print("nothing to prune.")
    elif not result.applied:
        print("dry run: pass --apply to remove these rows.")
    return 0


def cmd_index_prune(args: argparse.Namespace) -> int:
    try:
        root = resolve_command_config(args).runs_root
    except CommandConfigError as exc:
        emit_error(exc, hint=exc.hint, json_output=bool(getattr(args, "json", False)))
        return 1
    try:
        result = prune_job_locations(root, apply=bool(getattr(args, "apply", False)))
    except (JobLocationIndexError, OSError) as exc:
        # OSError covers an unreadable recorded path, a read-only runs root
        # and a contended index lock; in every case the index is untouched.
        emit_error(
            exc,
            hint=(
                "Repair the reported path or job_locations.json before retrying; "
                "nothing was written."
            ),
            json_output=bool(getattr(args, "json", False)),
        )
        return 1
    try:
        return _emit_index_prune(result, json_output=bool(getattr(args, "json", False)))
    except BrokenPipeError:
        return 0


def _index_row_payload(record: JobLocationRecord) -> dict[str, Any]:
    return {
        "job_id": record.job_id,
        "app_name": record.app_name,
        "status": record.status,
        "original_run_dir": record.original_run_dir,
        "latest_known_path": record.latest_known_path,
    }


def _index_rebuild_payload(result: JobLocationRebuildResult) -> dict[str, Any]:
    return {
        "index_path": result.index_path,
        "scanned": result.scanned,
        "total": result.total,
        "added_count": len(result.added),
        "updated_count": len(result.updated),
        "unchanged_count": result.unchanged,
        "skipped_count": len(result.skipped),
        "applied": result.applied,
        "added": [_index_row_payload(record) for record in result.added],
        "updated": [_index_row_payload(record) for record in result.updated],
        "skipped": list(result.skipped),
        "conflicts": [
            {
                "job_id": conflict.job_id,
                "kept_path": conflict.kept_path,
                "ignored_paths": list(conflict.ignored_paths),
            }
            for conflict in result.conflicts
        ],
    }


def _emit_index_rebuild(result: JobLocationRebuildResult, *, json_output: bool) -> int:
    if json_output:
        emit_json(_index_rebuild_payload(result))
        return 0

    print(f"{label('index:')} {result.index_path}")
    print(f"{label('scanned:')} {result.scanned}")
    print(f"{label('rows:')} {result.total}")
    print(f"{label('added:')} {len(result.added)}")
    print(f"{label('updated:')} {len(result.updated)}")
    print(f"{label('unchanged:')} {result.unchanged}")
    print(f"{label('skipped:')} {len(result.skipped)}")
    for heading, rows in (("+", result.added), ("~", result.updated)):
        for record in rows:
            shown = record.latest_known_path or record.original_run_dir
            print(f"  {heading} {record.job_id} {status_text(record.status)} {shown}")
    for job_dir in result.skipped:
        print(f"  ? {job_dir} (state names no job id)")
    for conflict in result.conflicts:
        ignored = ", ".join(conflict.ignored_paths)
        print(
            f"{label('conflict:')} {conflict.job_id} kept {conflict.kept_path}; ignored {ignored}"
        )
    if not result.added and not result.updated:
        print("nothing to change.")
    elif not result.applied:
        print("dry run: rerun without --dry-run to write these rows.")
    return 0


def cmd_index_rebuild(args: argparse.Namespace) -> int:
    from orca_auto.orca.job_locations import rebuild_job_location_records

    try:
        root = resolve_command_config(args).runs_root
    except CommandConfigError as exc:
        emit_error(exc, hint=exc.hint, json_output=bool(getattr(args, "json", False)))
        return 1
    try:
        result = rebuild_job_location_records(root, apply=not bool(getattr(args, "dry_run", False)))
    except (JobLocationIndexError, OSError) as exc:
        # The walk, the state reads and the single locked rewrite all fail
        # closed: a damaged index or an unreadable root leaves the file as is.
        emit_error(
            exc,
            hint=(
                "Repair the reported path or job_locations.json before retrying; "
                "nothing was written."
            ),
            json_output=bool(getattr(args, "json", False)),
        )
        return 1
    try:
        return _emit_index_rebuild(result, json_output=bool(getattr(args, "json", False)))
    except BrokenPipeError:
        return 0
