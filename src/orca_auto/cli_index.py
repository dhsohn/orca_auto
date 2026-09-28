"""``orca_auto index prune|rebuild``: preview or apply job-location index maintenance."""

from __future__ import annotations

import argparse
from collections import Counter
from typing import TYPE_CHECKING, Any

from orca_auto.cli_handlers import CommandConfigError, resolve_command_config
from orca_auto.core.indexing import (
    JobLocationIndexError,
    JobLocationPruneResult,
    JobLocationRecord,
    prune_job_locations,
)
from orca_auto.terminal import emit_error, emit_json, label, status_text

if TYPE_CHECKING:
    from orca_auto.orca.job_locations import JobLocationRebuildResult


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
    return _emit_index_prune(result, json_output=bool(getattr(args, "json", False)))


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
    return _emit_index_rebuild(result, json_output=bool(getattr(args, "json", False)))
