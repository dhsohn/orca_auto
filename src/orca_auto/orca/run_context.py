from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.config.schema import normalize_max_concurrent
from orca_auto.core.paths import is_subpath
from orca_auto.core.paths.retired import path_is_retired_workflow_owned
from orca_auto.orca.config import AppConfig


def _validate_reaction_dir(cfg: AppConfig, reaction_dir_raw: str) -> Path:
    reaction_dir = Path(reaction_dir_raw).expanduser().resolve()
    if not reaction_dir.exists() or not reaction_dir.is_dir():
        raise ValueError(f"Job directory not found: {reaction_dir}")

    allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
    if not is_subpath(reaction_dir, allowed_root):
        raise ValueError(
            f"Job directory must be under allowed root: {allowed_root}. got={reaction_dir}"
        )
    if path_is_retired_workflow_owned(reaction_dir, allowed_root):
        raise ValueError(
            "Workflow directories are retired; submit a standalone ORCA input directory"
        )
    return reaction_dir


@dataclass(frozen=True)
class ResolvedRunTarget:
    reaction_dir: Path
    selected_inp: Path


@dataclass(frozen=True)
class RunExecutionContext:
    cfg: AppConfig
    reaction_dir: Path
    selected_inp: Path
    admission_root: Path
    reservation_token: str | None = None
    admission_app_name: str | None = None
    admission_task_id: str | None = None
    force: bool = False
    execution_provenance: dict[str, Any] | None = None
    queue_id: str | None = None
    queue_generation: str | None = None


@dataclass(frozen=True)
class WorkerStatusInfo:
    status: str | None = None
    pid: int | None = None
    log_file: str | Path | None = None
    detail: str | None = None


@dataclass(frozen=True)
class RunSubmissionContext:
    cfg: AppConfig
    reaction_dir: Path
    selected_inp: Path
    allowed_root: Path


def configured_max_concurrent(cfg: AppConfig) -> int:
    return normalize_max_concurrent(cfg.runtime.max_concurrent, 4)


def configured_admission_root(cfg: AppConfig) -> Path:
    return Path(cfg.runtime.resolved_admission_root).expanduser().resolve()


def configured_admission_limit(cfg: AppConfig) -> int:
    return cfg.runtime.resolved_admission_limit


def reaction_dir_arg(args: Any) -> str | None:
    raw = getattr(args, "path", None) or getattr(args, "reaction_dir", None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw


def resolve_run_target(
    cfg: AppConfig,
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
    cfg: AppConfig,
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
    cfg: AppConfig | None,
    load_config_fn: Callable[[Any], AppConfig],
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


__all__ = [
    "ResolvedRunTarget",
    "RunExecutionContext",
    "RunSubmissionContext",
    "WorkerStatusInfo",
    "configured_admission_limit",
    "configured_admission_root",
    "configured_max_concurrent",
    "reaction_dir_arg",
    "resolve_run_target",
    "resolve_run_target_or_log",
    "resolve_submission_context",
]
