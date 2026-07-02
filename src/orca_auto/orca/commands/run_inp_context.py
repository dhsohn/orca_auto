from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.config.schema import resolved_admission_limit
from orca_auto.orca.admission_env import (
    ADMISSION_APP_NAME_ENV_VAR,
    ADMISSION_TASK_ID_ENV_VAR,
    ADMISSION_TOKEN_ENV_VAR,
)
from orca_auto.orca.retry_policy import effective_max_retries

from ._helpers import _validate_reaction_dir


@dataclass(frozen=True)
class ResolvedRunTarget:
    reaction_dir: Path
    selected_inp: Path


@dataclass(frozen=True)
class RunExecutionContext:
    cfg: Any
    reaction_dir: Path
    selected_inp: Path
    allowed_root: Path
    admission_root: Path
    max_retries: int
    max_concurrent: int
    admission_limit: int
    reservation_token: str | None
    admission_app_name: str | None
    admission_task_id: str | None


@dataclass(frozen=True)
class WorkerStatusInfo:
    status: str | None = None
    pid: int | None = None
    log_file: str | Path | None = None
    detail: str | None = None


@dataclass(frozen=True)
class RunSubmissionContext:
    cfg: Any
    reaction_dir: Path
    selected_inp: Path
    allowed_root: Path


def configured_max_concurrent(cfg: Any) -> int:
    raw = getattr(cfg.runtime, "max_concurrent", 4)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 4
    return max(1, value)


def configured_admission_root(cfg: Any) -> Path:
    raw = (
        getattr(cfg.runtime, "resolved_admission_root", None)
        or getattr(cfg.runtime, "admission_root", "")
        or getattr(cfg.runtime, "allowed_root", "")
    )
    return Path(str(raw)).expanduser().resolve()


def configured_admission_limit(cfg: Any) -> int:
    return resolved_admission_limit(
        getattr(cfg.runtime, "admission_limit", None),
        configured_max_concurrent(cfg),
    )


def reaction_dir_arg(args: Any) -> str | None:
    raw = getattr(args, "path", None) or getattr(args, "reaction_dir", None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw


def resolve_run_target(
    cfg: Any,
    reaction_dir_raw: str,
    *,
    select_latest_inp_fn: Callable[[Path], Path],
) -> ResolvedRunTarget:
    reaction_dir = _validate_reaction_dir(cfg, reaction_dir_raw)
    return ResolvedRunTarget(
        reaction_dir=reaction_dir,
        selected_inp=select_latest_inp_fn(reaction_dir),
    )


def resolve_run_target_or_log(
    cfg: Any,
    reaction_dir_raw: str,
    *,
    select_latest_inp_fn: Callable[[Path], Path],
    logger: Any,
) -> ResolvedRunTarget | None:
    try:
        return resolve_run_target(
            cfg,
            reaction_dir_raw,
            select_latest_inp_fn=select_latest_inp_fn,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        return None


def resolve_submission_context(
    args: Any,
    *,
    cfg: Any | None,
    load_config_fn: Callable[[Any], Any],
    select_latest_inp_fn: Callable[[Path], Path],
    logger: Any,
) -> RunSubmissionContext | None:
    if cfg is None:
        cfg = load_config_fn(args.config)
    reaction_dir_raw = reaction_dir_arg(args)
    if reaction_dir_raw is None:
        logger.error("job directory path is required")
        return None
    target = resolve_run_target_or_log(
        cfg,
        reaction_dir_raw,
        select_latest_inp_fn=select_latest_inp_fn,
        logger=logger,
    )
    if target is None:
        return None
    return RunSubmissionContext(
        cfg=cfg,
        reaction_dir=target.reaction_dir,
        selected_inp=target.selected_inp,
        allowed_root=Path(cfg.runtime.allowed_root).expanduser().resolve(),
    )


def resolve_execution_context(
    args: Any,
    *,
    cfg: Any | None,
    reaction_dir: Path | None,
    selected_inp: Path | None,
    reservation_token: str | None,
    admission_app_name: str | None,
    admission_task_id: str | None,
    load_config_fn: Callable[[Any], Any],
    select_latest_inp_fn: Callable[[Path], Path],
    logger: Any,
    env_get_fn: Callable[[str, str], str | None] = os.getenv,
) -> RunExecutionContext | None:
    if cfg is None:
        cfg = load_config_fn(args.config)
    if reaction_dir is None or selected_inp is None:
        reaction_dir_raw = reaction_dir_arg(args)
        if reaction_dir_raw is None:
            logger.error("job directory path is required")
            return None
        target = resolve_run_target_or_log(
            cfg,
            reaction_dir_raw,
            select_latest_inp_fn=select_latest_inp_fn,
            logger=logger,
        )
        if target is None:
            return None
        reaction_dir = target.reaction_dir
        selected_inp = target.selected_inp

    return RunExecutionContext(
        cfg=cfg,
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        allowed_root=Path(cfg.runtime.allowed_root).expanduser().resolve(),
        admission_root=configured_admission_root(cfg),
        max_retries=effective_max_retries(
            selected_inp,
            configured_max_retries=max(0, int(cfg.runtime.default_max_retries)),
        ),
        max_concurrent=configured_max_concurrent(cfg),
        admission_limit=configured_admission_limit(cfg),
        reservation_token=reservation_token
        if reservation_token is not None
        else ((env_get_fn(ADMISSION_TOKEN_ENV_VAR, "") or "").strip() or None),
        admission_app_name=admission_app_name
        if admission_app_name is not None
        else ((env_get_fn(ADMISSION_APP_NAME_ENV_VAR, "") or "").strip() or None),
        admission_task_id=admission_task_id
        if admission_task_id is not None
        else ((env_get_fn(ADMISSION_TASK_ID_ENV_VAR, "") or "").strip() or None),
    )


__all__ = [
    "ResolvedRunTarget",
    "RunExecutionContext",
    "RunSubmissionContext",
    "WorkerStatusInfo",
    "configured_admission_limit",
    "configured_admission_root",
    "configured_max_concurrent",
    "reaction_dir_arg",
    "resolve_execution_context",
    "resolve_run_target",
    "resolve_run_target_or_log",
    "resolve_submission_context",
]
