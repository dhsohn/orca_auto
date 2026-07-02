from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import AppConfig, load_config
from ..job_locations import reindex_job_locations
from ..organize_index import (
    acquire_index_lock,
    append_failed_rollback,
    append_record,
    load_index,
    rebuild_index,
)
from ..result_organizer_filesystem import check_conflict, execute_move, rollback_move
from ..result_organizer_models import OrganizePlan, SkipReason
from ..result_organizer_planning import plan_root_scan, plan_single
from ..result_organizer_state import sync_state_after_move, sync_state_after_rollback
from ..state import now_utc_iso
from . import organize_apply as _organize_apply
from . import organize_notifications as _organize_notifications
from . import organize_output as _organize_output
from . import organize_tracking as _organize_tracking
from ._helpers import (
    _validate_reaction_dir,
    _validate_root_scan_dir,
    _workflow_runtime_paths,
    finalize_batch_apply,
)

logger = logging.getLogger(__name__)


def resolved_organized_root(cfg: AppConfig, reaction_dir: str | Path) -> Path:
    runtime_paths = _workflow_runtime_paths(cfg, reaction_dir)
    if runtime_paths is not None:
        return runtime_paths["organized_root"].expanduser().resolve()
    return Path(cfg.runtime.organized_root).resolve()


def default_apply_dependencies() -> _organize_apply.OrganizeApplyDependencies:
    """Wire the production OrganizeApplyDependencies.

    Globals are looked up at call time so tests can patch
    ``organize_service.<name>`` (e.g. ``append_record``, ``check_conflict``).
    """
    return _organize_apply.OrganizeApplyDependencies(
        index=_organize_apply.OrganizeApplyIndexDeps(
            acquire_index_lock=acquire_index_lock,
            append_failed_rollback=append_failed_rollback,
            append_record=append_record,
            build_index_record=_organize_tracking.build_index_record,
            check_conflict=check_conflict,
            load_index=load_index,
            now_utc_iso=now_utc_iso,
        ),
        move=_organize_apply.OrganizeApplyMoveDeps(
            cleanup_organized_ref_stub=_organize_tracking.cleanup_organized_ref_stub,
            execute_move=execute_move,
            rollback_move=rollback_move,
        ),
        tracking=_organize_apply.OrganizeApplyTrackingDeps(
            sync_state_after_move=sync_state_after_move,
            sync_state_after_rollback=sync_state_after_rollback,
            write_tracking_after_move=_organize_tracking.write_tracking_after_move,
            restore_tracking_after_rollback=_organize_tracking.restore_tracking_after_rollback,
        ),
        notifications=_organize_apply.OrganizeApplyNotificationDeps(
            send_organize_notification=_organize_notifications._send_organize_notification,
            log=logger,
        ),
    )


def apply_organize_plans(
    plans: list[OrganizePlan],
    skips: list[SkipReason],
    organized_root: Path,
    cfg: AppConfig,
    *,
    notify_summary: bool,
    deps: _organize_apply.OrganizeApplyDependencies | None = None,
) -> dict[str, Any]:
    return _organize_apply._apply_organize_plans(
        plans,
        skips,
        organized_root,
        cfg,
        notify_summary=notify_summary,
        deps=deps or default_apply_dependencies(),
    )


def resolve_organize_scope(
    cfg: AppConfig,
    *,
    organized_root: Path,
    reaction_dir_raw: str | None,
    root_raw: str | None,
    validate_reaction_dir_fn: Callable[[AppConfig, str], Path] | None = None,
    validate_root_scan_dir_fn: Callable[[AppConfig, str], Path] | None = None,
    plan_single_fn: Callable[[Path, Path], tuple[OrganizePlan | None, SkipReason | None]]
    | None = None,
    plan_root_scan_fn: Callable[[Path, Path], tuple[list[OrganizePlan], list[SkipReason]]]
    | None = None,
    log: logging.Logger = logger,
) -> tuple[list[OrganizePlan], list[SkipReason]] | None:
    validate_reaction_dir = validate_reaction_dir_fn or _validate_reaction_dir
    validate_root_scan_dir = validate_root_scan_dir_fn or _validate_root_scan_dir
    plan_single_dir = plan_single_fn or plan_single
    plan_root = plan_root_scan_fn or plan_root_scan

    if reaction_dir_raw:
        try:
            reaction_dir = validate_reaction_dir(cfg, reaction_dir_raw)
        except ValueError as exc:
            log.error("%s", exc)
            return None
        plan, skip = plan_single_dir(reaction_dir, organized_root)
        return ([plan] if plan else []), ([skip] if skip else [])

    try:
        assert isinstance(root_raw, str)
        root = validate_root_scan_dir(cfg, root_raw)
    except ValueError as exc:
        log.error("%s", exc)
        return None
    return plan_root(root, organized_root)


def cmd_organize_apply(
    plans: list[OrganizePlan],
    skips: list[SkipReason],
    organized_root: Path,
    cfg: AppConfig,
    *,
    apply_plans_fn: Callable[..., dict[str, Any]] | None = None,
    finalize_batch_apply_fn: Callable[[dict[str, Any], Any, list[dict[str, Any]]], int]
    | None = None,
    emit_organize_fn: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    apply_plans = apply_plans_fn or apply_organize_plans
    finalize = finalize_batch_apply_fn or finalize_batch_apply
    emit_organize = emit_organize_fn or _organize_output.emit_organize

    summary = apply_plans(
        plans,
        skips,
        organized_root,
        cfg,
        notify_summary=True,
    )
    return finalize(
        summary,
        emit_organize,
        summary["failures"],
    )


def organize_no_plan_result(reaction_dir: Path, skips: list[SkipReason]) -> dict[str, Any]:
    if skips:
        first_skip = skips[0]
        return {
            "action": "skipped",
            "reaction_dir": first_skip.reaction_dir,
            "reason": first_skip.reason,
        }
    return {
        "action": "skipped",
        "reaction_dir": str(reaction_dir),
        "reason": "nothing_to_organize",
    }


def organize_failure_result(reaction_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    failure = next(
        (item for item in summary["failures"] if isinstance(item, dict)),
        {},
    )
    return {
        "action": "failed",
        "reaction_dir": str(reaction_dir),
        "reason": str(failure.get("reason") or "organize_failed"),
        "run_id": str(failure.get("run_id") or ""),
    }


def organize_success_result(organized: list[Any]) -> dict[str, Any] | None:
    if not organized:
        return None
    plan = organized[0].get("_plan") if isinstance(organized[0], dict) else None
    if not isinstance(plan, OrganizePlan):
        return None
    return {
        "action": "organized",
        "reaction_dir": str(plan.source_dir),
        "run_id": plan.run_id,
        "target_dir": str(plan.target_abs_path),
        "job_type": plan.job_type,
        "molecule_key": plan.molecule_key,
    }


def organize_skipped_result(
    reaction_dir: Path,
    *,
    skipped_results: list[Any],
    skips: list[SkipReason],
) -> dict[str, Any]:
    if skipped_results:
        skipped_item = skipped_results[0]
        return {
            "action": "skipped",
            "reaction_dir": str(reaction_dir),
            "reason": str(skipped_item.get("reason") or "skipped"),
            "run_id": str(skipped_item.get("run_id") or ""),
        }
    return organize_no_plan_result(reaction_dir, skips)


def organize_reaction_dir(
    cfg: AppConfig,
    reaction_dir: Path,
    *,
    notify_summary: bool = True,
    resolved_organized_root_fn: Callable[[AppConfig, str | Path], Path] | None = None,
    resolve_scope_fn: Callable[..., tuple[list[OrganizePlan], list[SkipReason]] | None]
    | None = None,
    apply_plans_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolve_root = resolved_organized_root_fn or resolved_organized_root
    resolve_scope = resolve_scope_fn or resolve_organize_scope
    apply_plans = apply_plans_fn or apply_organize_plans

    organized_root = resolve_root(cfg, reaction_dir)
    scope = resolve_scope(
        cfg,
        organized_root=organized_root,
        reaction_dir_raw=str(reaction_dir),
        root_raw=None,
    )
    if scope is None:
        return {
            "action": "failed",
            "reaction_dir": str(reaction_dir),
            "reason": "invalid_reaction_dir",
        }

    plans, skips = scope
    if not plans:
        return organize_no_plan_result(reaction_dir, skips)

    summary = apply_plans(
        plans,
        skips,
        organized_root,
        cfg,
        notify_summary=notify_summary,
    )
    organized = summary.get("_organized_results", [])
    skipped_results = summary.get("_skipped_results", [])

    if summary.get("failed"):
        return organize_failure_result(reaction_dir, summary)

    organized_result = organize_success_result(organized)
    if organized_result is not None:
        return organized_result
    return organize_skipped_result(
        reaction_dir,
        skipped_results=skipped_results,
        skips=skips,
    )


def cmd_organize(
    args: Any,
    *,
    load_config_fn: Callable[[Any], AppConfig] | None = None,
    rebuild_index_fn: Callable[[Path], int] | None = None,
    reindex_job_locations_fn: Callable[[AppConfig], int] | None = None,
    emit_organize_fn: Callable[[dict[str, Any]], None] | None = None,
    resolved_organized_root_fn: Callable[[AppConfig, str | Path], Path] | None = None,
    resolve_scope_fn: Callable[..., tuple[list[OrganizePlan], list[SkipReason]] | None]
    | None = None,
    build_dry_run_summary_fn: Callable[[list[OrganizePlan], list[SkipReason]], dict[str, Any]]
    | None = None,
    cmd_apply_fn: Callable[[list[OrganizePlan], list[SkipReason], Path, AppConfig], int]
    | None = None,
    log: logging.Logger = logger,
) -> int:
    load_cfg = load_config_fn or load_config
    rebuild = rebuild_index_fn or rebuild_index
    reindex = reindex_job_locations_fn or reindex_job_locations
    emit_organize = emit_organize_fn or _organize_output.emit_organize
    resolve_root = resolved_organized_root_fn or resolved_organized_root
    resolve_scope = resolve_scope_fn or resolve_organize_scope
    build_dry_run_summary = build_dry_run_summary_fn or _organize_output.build_dry_run_summary
    cmd_apply = cmd_apply_fn or cmd_organize_apply

    cfg = load_cfg(args.config)
    organized_root = Path(cfg.runtime.organized_root).resolve()

    if getattr(args, "rebuild_index", False):
        count = rebuild(organized_root)
        tracking_count = reindex(cfg)
        emit_organize(
            {
                "action": "rebuild_index",
                "records_count": count,
                "job_locations_count": tracking_count,
            }
        )
        return 0

    reaction_dir_raw = getattr(args, "reaction_dir", None)
    root_raw = getattr(args, "root", None)

    if reaction_dir_raw and root_raw:
        log.error("--reaction-dir and --root are mutually exclusive")
        return 1

    if not reaction_dir_raw and not root_raw:
        log.error("Either --reaction-dir or --root is required")
        return 1

    if reaction_dir_raw:
        organized_root = resolve_root(cfg, str(reaction_dir_raw))

    scope = resolve_scope(
        cfg,
        organized_root=organized_root,
        reaction_dir_raw=reaction_dir_raw,
        root_raw=root_raw,
    )
    if scope is None:
        return 1
    plans, skips_list = scope

    if not getattr(args, "apply", False):
        emit_organize(build_dry_run_summary(plans, skips_list))
        return 0

    return cmd_apply(plans, skips_list, organized_root, cfg)
