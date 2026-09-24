"""``orca_auto scratch`` — operator surface for RAM scratch workspaces.

``EngineScratchWorkspace.create`` fails closed: one stale, unverifiable, or
invalid-manifest workspace under ``orca.runtime.scratch_root`` blocks every
later scratch launch on the host. ``scratch list`` shows that state without
touching anything; ``scratch clear`` removes exactly the non-live workspaces
an operator names (or all of them with ``--all-stale``) through the same
fd-pinned removal the worker uses. Live workspaces are never removed here.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

from orca_auto.core.config.discovery import (
    resolve_shared_config_path,
    shared_config_text_from_args,
)
from orca_auto.core.config.files import YAML_CONFIG_LOAD_EXCEPTIONS
from orca_auto.core.engine_scratch import (
    SCRATCH_REMOVABLE_STATES,
    SCRATCH_STATE_LIVE,
    EngineScratchError,
    ScratchWorkspaceRemoval,
    ScratchWorkspaceReport,
    inspect_scratch_root,
    remove_scratch_workspace,
)
from orca_auto.orca.config import load_config
from orca_auto.orca.scratch import OrcaScratchPolicy
from orca_auto.terminal import RED, YELLOW, emit_error, emit_json, label, paint

_CLEAR_HINT = (
    "Run `orca_auto scratch clear NAME` for one workspace, or "
    "`orca_auto scratch clear --all-stale` for every non-live one."
)


class _ScratchCommandError(Exception):
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def _scratch_policy_from_args(args: argparse.Namespace) -> tuple[OrcaScratchPolicy, Path]:
    """Build the policy exactly as the worker does and return it with the runs root."""

    config_path = resolve_shared_config_path(shared_config_text_from_args(args) or None)
    if not config_path:
        raise _ScratchCommandError(
            "shared config is not configured",
            hint="Pass --config pointing at orca_auto.yaml, or run `orca_auto init`.",
        )
    try:
        cfg = load_config(config_path)
    except YAML_CONFIG_LOAD_EXCEPTIONS as exc:
        raise _ScratchCommandError(
            str(exc),
            hint="Check the config path and repair the reported setting before retrying.",
        ) from exc
    if not cfg.scratch.enabled:
        raise _ScratchCommandError(
            f"orca.runtime.scratch_root is not configured in {config_path}",
            hint="RAM scratch is disabled; there are no scratch workspaces to inspect.",
        )
    try:
        policy = OrcaScratchPolicy(
            root=Path(cfg.scratch.root),
            min_free_bytes=int(cfg.scratch.min_free_gb) * 1024**3,
            max_task_memory_bytes=int(cfg.resources.max_memory_gb_per_task) * 1024**3,
        )
    except ValueError as exc:
        raise _ScratchCommandError(str(exc)) from exc
    return policy, Path(cfg.runtime.allowed_root)


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _state_text(state: str) -> str:
    if state == SCRATCH_STATE_LIVE:
        return state
    return paint(state, RED if state in SCRATCH_REMOVABLE_STATES else YELLOW)


def _report_line(report: ScratchWorkspaceReport) -> str:
    parts = [f"  - {report.name}", _state_text(report.state)]
    if report.owner_pid is not None:
        parts.append(f"pid {report.owner_pid}")
    size = _human_bytes(report.size_bytes)
    if report.size_walk_truncated:
        size = f">= {size}"
    parts.append(size)
    if report.durable_dir:
        parts.append(report.durable_dir)
    line = " ".join(parts)
    if report.publication_journal is not None:
        journal = report.publication_journal
        phase = "corrupt" if journal.corrupt else journal.phase
        line += f"\n      publication journal: {phase} ({journal.item_count} items)"
    return line


def _list_payload(root: Path, reports: list[ScratchWorkspaceReport]) -> dict[str, Any]:
    return {
        "root": str(root),
        "root_exists": root.is_dir(),
        "workspace_count": len(reports),
        "blocking_count": sum(1 for report in reports if report.blocks_launch),
        "workspaces": [report.as_payload() for report in reports],
    }


def _emit_scratch_list(
    root: Path, reports: list[ScratchWorkspaceReport], *, json_output: bool
) -> int:
    if json_output:
        emit_json(_list_payload(root, reports))
        return 0
    print(f"{label('root:')} {root}")
    print(f"{label('workspaces:')} {len(reports)}")
    if reports:
        counts = Counter(report.state for report in reports)
        summary = ", ".join(f"{state} {count}" for state, count in sorted(counts.items()))
        print(f"{label('by state:')} {summary}")
    for report in reports:
        print(_report_line(report))
    blocking = [report for report in reports if report.blocks_launch]
    if not reports:
        print("no scratch workspaces.")
    elif not blocking:
        print("nothing blocks scratch launches.")
    else:
        names = ", ".join(report.name for report in blocking)
        print(
            paint(
                f"{len(blocking)} workspace(s) block every new scratch launch: {names}",
                RED,
            )
        )
        print(_CLEAR_HINT)
    return 0


def cmd_scratch_list(args: argparse.Namespace) -> int:
    json_output = bool(getattr(args, "json", False))
    try:
        policy, _runs_root = _scratch_policy_from_args(args)
        reports = inspect_scratch_root(policy.root)
    except _ScratchCommandError as exc:
        emit_error(exc, hint=exc.hint, json_output=json_output)
        return 1
    except (EngineScratchError, OSError) as exc:
        emit_error(
            exc,
            hint="The scratch root is unreadable or unsafe; inspect it by hand.",
            json_output=json_output,
        )
        return 1
    try:
        return _emit_scratch_list(policy.root, reports, json_output=json_output)
    except BrokenPipeError:
        return 0


def _clear_targets(
    args: argparse.Namespace,
    reports: list[ScratchWorkspaceReport],
) -> tuple[list[ScratchWorkspaceReport], list[dict[str, Any]]]:
    """Return the workspaces to remove and the ones refused up front."""

    name = getattr(args, "name", None)
    all_stale = bool(getattr(args, "all_stale", False))
    if bool(name) == all_stale:
        raise _ScratchCommandError(
            "scratch clear takes exactly one of NAME or --all-stale",
            hint="Run `orca_auto scratch list` to see workspace names.",
        )
    if all_stale:
        return [report for report in reports if report.state in SCRATCH_REMOVABLE_STATES], []
    matched = [report for report in reports if report.name == name]
    if not matched:
        raise _ScratchCommandError(
            f"no scratch workspace named {name!r}",
            hint="Run `orca_auto scratch list` to see workspace names.",
        )
    report = matched[0]
    if report.state not in SCRATCH_REMOVABLE_STATES:
        return [], [_refusal(report)]
    return [report], []


def _refusal(report: ScratchWorkspaceReport, reason: str | None = None) -> dict[str, Any]:
    if reason is None:
        if report.state == SCRATCH_STATE_LIVE:
            reason = f"workspace is live (owner pid {report.owner_pid})"
        else:
            reason = f"{report.state} entries cannot be removed by this command"
    return {"name": report.name, "state": report.state, "reason": reason}


def _remove_each(
    root: Path,
    targets: list[ScratchWorkspaceReport],
    remover: Callable[..., ScratchWorkspaceRemoval],
) -> Iterator[tuple[ScratchWorkspaceReport, ScratchWorkspaceRemoval | None, str | None]]:
    for report in targets:
        try:
            yield report, remover(root, report.name), None
        except (EngineScratchError, OSError) as exc:
            yield report, None, str(exc)


def _emit_scratch_clear(
    root: Path,
    removed: list[ScratchWorkspaceRemoval],
    refused: list[dict[str, Any]],
    *,
    json_output: bool,
) -> int:
    # Nothing to remove is not a failure; a refused workspace is, because the
    # operator asked for it and it still blocks scratch launches.
    error = f"{len(refused)} workspace(s) could not be removed" if refused else None
    exit_code = 1 if refused else 0
    if json_output:
        payload = {
            "root": str(root),
            "removed_count": len(removed),
            "removed": [item.as_payload() for item in removed],
            "refused": refused,
        }
        emit_json(payload, ok=not refused, error=error)
        if error:
            emit_error(error, hint=_CLEAR_HINT)
        return exit_code
    print(f"{label('root:')} {root}")
    print(f"{label('removed:')} {len(removed)}")
    for item in removed:
        report = item.report
        print(f"  - {report.name} {_state_text(report.state)} {_human_bytes(report.size_bytes)}")
        for name in item.removed_durable_entries:
            print(f"      removed publication temp: {report.durable_dir}/{name}")
        if item.publication_journal_removed:
            print(f"      removed committed publication journal in {report.durable_dir}")
        if item.durable_note:
            print(f"      note: {item.durable_note}")
    if refused:
        print(f"{label('refused:')} {len(refused)}")
        for entry in refused:
            print(f"  - {entry['name']} {_state_text(entry['state'])} {entry['reason']}")
    if not removed and not refused:
        print("nothing to clear.")
    if error:
        emit_error(error, hint=_CLEAR_HINT)
    return exit_code


def cmd_scratch_clear(args: argparse.Namespace) -> int:
    json_output = bool(getattr(args, "json", False))
    try:
        policy, runs_root = _scratch_policy_from_args(args)
        reports = inspect_scratch_root(policy.root)
        targets, refused = _clear_targets(args, reports)
    except _ScratchCommandError as exc:
        emit_error(exc, hint=exc.hint, json_output=json_output)
        return 1
    except (EngineScratchError, OSError) as exc:
        emit_error(
            exc,
            hint="The scratch root is unreadable or unsafe; inspect it by hand.",
            json_output=json_output,
        )
        return 1
    removed: list[ScratchWorkspaceRemoval] = []
    remover = partial(remove_scratch_workspace, durable_root=runs_root)
    for report, removal, failure in _remove_each(policy.root, targets, remover):
        if removal is not None:
            removed.append(removal)
        else:
            # The workspace changed between listing and the locked re-check
            # (for example its owner became live); nothing was removed.
            refused.append(_refusal(report, failure))
    try:
        return _emit_scratch_clear(policy.root, removed, refused, json_output=json_output)
    except BrokenPipeError:
        return 0


__all__ = ["cmd_scratch_clear", "cmd_scratch_list"]
