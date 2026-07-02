from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, ClassVar

from ..config import AppConfig
from ..result_organizer_models import OrganizePlan, SkipReason

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PlanApplyResult:
    run_id: str
    action: str
    reason: str = ""
    plan: OrganizePlan | None = None

    def to_result_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"run_id": self.run_id, "action": self.action}
        if self.reason:
            payload["reason"] = self.reason
        if self.plan is not None:
            payload["_plan"] = self.plan
        return payload

    def to_failure_payload(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "reason": self.reason}


@dataclass(frozen=True)
class OrganizeApplyIndexDeps:
    acquire_index_lock: Any
    append_failed_rollback: Any
    append_record: Any
    build_index_record: Any
    check_conflict: Any
    load_index: Any
    now_utc_iso: Any


@dataclass(frozen=True)
class OrganizeApplyMoveDeps:
    cleanup_organized_ref_stub: Any
    execute_move: Any
    rollback_move: Any


@dataclass(frozen=True)
class OrganizeApplyTrackingDeps:
    sync_state_after_move: Any
    sync_state_after_rollback: Any
    write_tracking_after_move: Any
    restore_tracking_after_rollback: Any


@dataclass(frozen=True)
class OrganizeApplyNotificationDeps:
    send_organize_notification: Any
    log: Any = logger


@dataclass(frozen=True)
class OrganizeApplyExtensionDeps:
    plan_conflict_result: Any = None
    bookkeep_successful_move: Any = None
    bookkeep_rollback_failure: Any = None
    rollback_after_apply_failure: Any = None
    apply_one_organize_plan: Any = None


_ORGANIZE_APPLY_DEP_GROUPS: Mapping[str, type[Any]] = {
    "index": OrganizeApplyIndexDeps,
    "move": OrganizeApplyMoveDeps,
    "tracking": OrganizeApplyTrackingDeps,
    "notifications": OrganizeApplyNotificationDeps,
    "extensions": OrganizeApplyExtensionDeps,
}

_ORGANIZE_APPLY_DEP_TARGETS: Mapping[str, str] = {
    field.name: group_name
    for group_name, deps_type in _ORGANIZE_APPLY_DEP_GROUPS.items()
    for field in fields(deps_type)
}


@dataclass(frozen=True)
class OrganizeApplyDependencies:
    index: OrganizeApplyIndexDeps
    move: OrganizeApplyMoveDeps
    tracking: OrganizeApplyTrackingDeps
    notifications: OrganizeApplyNotificationDeps
    extensions: OrganizeApplyExtensionDeps = field(default_factory=OrganizeApplyExtensionDeps)

    _PASSTHROUGH_TARGETS: ClassVar[Mapping[str, str]] = _ORGANIZE_APPLY_DEP_TARGETS

    def __getattr__(self, name: str) -> Any:
        group_name = self._PASSTHROUGH_TARGETS.get(name)
        if group_name is None:
            raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")
        return getattr(getattr(self, group_name), name)


def _plan_conflict_result(
    plan: OrganizePlan,
    index: dict[str, dict[str, Any]],
    *,
    deps: OrganizeApplyDependencies,
) -> _PlanApplyResult | None:
    conflict = deps.check_conflict(plan, index)
    if conflict == "already_organized":
        return _PlanApplyResult(plan.run_id, "skipped", conflict)
    if conflict:
        return _PlanApplyResult(plan.run_id, "failed", conflict)
    return None


def _bookkeep_successful_move(
    cfg: AppConfig,
    *,
    organized_root: Path,
    plan: OrganizePlan,
    deps: OrganizeApplyDependencies,
) -> _PlanApplyResult:
    state_after_move = deps.sync_state_after_move(plan)
    deps.write_tracking_after_move(cfg, plan=plan, state_after_move=state_after_move)
    deps.append_record(organized_root, deps.build_index_record(plan, state_after_move))
    return _PlanApplyResult(plan.run_id, "moved", plan=plan)


def _bookkeep_rollback_failure(
    organized_root: Path,
    *,
    plan: OrganizePlan,
    rollback_exc: Exception,
    deps: OrganizeApplyDependencies,
) -> None:
    deps.log.error("Rollback failed for %s: %s", plan.run_id, rollback_exc)
    deps.append_failed_rollback(
        organized_root,
        {
            "run_id": plan.run_id,
            "target_path": str(plan.target_abs_path),
            "error": str(rollback_exc),
            "timestamp": deps.now_utc_iso(),
        },
    )


def _rollback_after_apply_failure(
    cfg: AppConfig,
    *,
    organized_root: Path,
    plan: OrganizePlan,
    failure_reason: str,
    deps: OrganizeApplyDependencies,
) -> str:
    try:
        deps.cleanup_organized_ref_stub(plan)
        deps.rollback_move(plan)
        state_after_rollback = deps.sync_state_after_rollback(plan)
        deps.restore_tracking_after_rollback(
            cfg, plan=plan, state_after_rollback=state_after_rollback
        )
        return f"{failure_reason}; rolled_back=true"
    except Exception as rollback_exc:  # noqa: BLE001
        bookkeep_failure = deps.bookkeep_rollback_failure or (
            lambda root, *, plan, rollback_exc: _bookkeep_rollback_failure(
                root, plan=plan, rollback_exc=rollback_exc, deps=deps
            )
        )
        bookkeep_failure(
            organized_root,
            plan=plan,
            rollback_exc=rollback_exc,
        )
        return f"{failure_reason}; rollback_failed: {rollback_exc}"


def _apply_one_organize_plan(
    cfg: AppConfig,
    *,
    organized_root: Path,
    plan: OrganizePlan,
    deps: OrganizeApplyDependencies,
) -> _PlanApplyResult:
    try:
        with deps.acquire_index_lock(organized_root):
            return _apply_locked_organize_plan(
                cfg, organized_root=organized_root, plan=plan, deps=deps
            )
    except Exception as exc:  # noqa: BLE001
        # Reached only when the index lock itself cannot be acquired; per-plan
        # failures inside the lock are handled in _apply_locked_organize_plan.
        deps.log.error("Organize apply failed for %s: %s", plan.run_id, exc)
        return _PlanApplyResult(plan.run_id, "failed", f"apply_failed: {exc}")


def _apply_locked_organize_plan(
    cfg: AppConfig,
    *,
    organized_root: Path,
    plan: OrganizePlan,
    deps: OrganizeApplyDependencies,
) -> _PlanApplyResult:
    # Runs while the index lock is held, so a failed move is rolled back before
    # the lock is released. A concurrent organizer therefore never observes the
    # transient moved-but-unrecorded state that an outside-the-lock rollback
    # would briefly expose.
    moved = False
    try:
        index = deps.load_index(organized_root)
        conflict_result = _call_plan_conflict_result(plan, index, deps=deps)
        if conflict_result is not None:
            return conflict_result

        deps.execute_move(plan)
        moved = True
        return _call_bookkeep_successful_move(
            cfg,
            organized_root=organized_root,
            plan=plan,
            deps=deps,
        )
    except Exception as exc:  # noqa: BLE001
        deps.log.error("Organize apply failed for %s: %s", plan.run_id, exc)
        failure_reason = f"apply_failed: {exc}"
        if moved:
            failure_reason = _call_rollback_after_apply_failure(
                cfg,
                organized_root=organized_root,
                plan=plan,
                failure_reason=failure_reason,
                deps=deps,
            )
        return _PlanApplyResult(plan.run_id, "failed", failure_reason)


def _build_apply_summary(
    *,
    results: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    skips: list[SkipReason],
) -> dict[str, Any]:
    organized = [r for r in results if r.get("action") == "moved"]
    skipped_results = [r for r in results if r.get("action") == "skipped"]

    return {
        "action": "apply",
        "organized": len(organized),
        "skipped": len(skips) + len(skipped_results),
        "failed": len(failures),
        "failures": failures,
        "_organized_results": organized,
        "_skipped_results": skipped_results,
        "_skip_reasons": skips,
    }


def _apply_organize_plans(
    plans: list[OrganizePlan],
    skips: list[SkipReason],
    organized_root: Path,
    cfg: AppConfig,
    *,
    notify_summary: bool,
    deps: OrganizeApplyDependencies,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for plan in plans:
        result = _call_apply_one_organize_plan(
            cfg, organized_root=organized_root, plan=plan, deps=deps
        )
        if result.action == "failed":
            failures.append(result.to_failure_payload())
        else:
            results.append(result.to_result_payload())

    summary = _build_apply_summary(results=results, failures=failures, skips=skips)

    if notify_summary:
        deps.send_organize_notification(
            cfg,
            organized=summary["_organized_results"],
            skipped_results=summary["_skipped_results"],
            failures=failures,
            skips=skips,
        )

    return summary


def _call_plan_conflict_result(
    plan: OrganizePlan,
    index: dict[str, dict[str, Any]],
    *,
    deps: OrganizeApplyDependencies,
) -> _PlanApplyResult | None:
    if deps.plan_conflict_result is not None:
        return deps.plan_conflict_result(plan, index)
    return _plan_conflict_result(plan, index, deps=deps)


def _call_bookkeep_successful_move(
    cfg: AppConfig,
    *,
    organized_root: Path,
    plan: OrganizePlan,
    deps: OrganizeApplyDependencies,
) -> _PlanApplyResult:
    if deps.bookkeep_successful_move is not None:
        return deps.bookkeep_successful_move(cfg, organized_root=organized_root, plan=plan)
    return _bookkeep_successful_move(cfg, organized_root=organized_root, plan=plan, deps=deps)


def _call_rollback_after_apply_failure(
    cfg: AppConfig,
    *,
    organized_root: Path,
    plan: OrganizePlan,
    failure_reason: str,
    deps: OrganizeApplyDependencies,
) -> str:
    if deps.rollback_after_apply_failure is not None:
        return deps.rollback_after_apply_failure(
            cfg,
            organized_root=organized_root,
            plan=plan,
            failure_reason=failure_reason,
        )
    return _rollback_after_apply_failure(
        cfg,
        organized_root=organized_root,
        plan=plan,
        failure_reason=failure_reason,
        deps=deps,
    )


def _call_apply_one_organize_plan(
    cfg: AppConfig,
    *,
    organized_root: Path,
    plan: OrganizePlan,
    deps: OrganizeApplyDependencies,
) -> _PlanApplyResult:
    if deps.apply_one_organize_plan is not None:
        return deps.apply_one_organize_plan(cfg, organized_root=organized_root, plan=plan)
    return _apply_one_organize_plan(cfg, organized_root=organized_root, plan=plan, deps=deps)
