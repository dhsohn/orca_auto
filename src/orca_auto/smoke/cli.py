from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orca_auto.cli_parser_commands import add_smoke_arguments
from orca_auto.core.config.engines import load_xtb_md_config
from orca_auto.core.config.files import (
    discover_shared_config_path,
    load_shared_config_mapping,
    runs_root_from_mapping,
    scheduler_admission_root,
    validated_runs_root_text,
)

from .runner import RealEngineAdmission, run_smoke_suite


def _source_checkout_root(repo_root_argument: str) -> Path:
    loaded_root = Path(__file__).resolve().parents[3]
    repo_root = (
        Path(repo_root_argument).expanduser().resolve()
        if repo_root_argument.strip()
        else loaded_root
    )
    if repo_root != loaded_root:
        raise ValueError(
            "--repo-root must match the source checkout backing this orca_auto command"
        )

    required = (
        repo_root / ".git",
        repo_root / "pyproject.toml",
        repo_root / "src" / "orca_auto" / "smoke" / "catalog.py",
        repo_root / "tests",
    )
    if not all(path.exists() for path in required):
        raise ValueError(
            "smoke requires an orca_auto source checkout with Git metadata and tests; "
            "use the checkout's scripts/smoke.sh when validating a separate worktree"
        )
    return repo_root


def _positive_int(value: object, *, field: str, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer >= 1") from exc
    if parsed < 1:
        raise ValueError(f"{field} must be an integer >= 1")
    return parsed


def _scheduler(raw: dict[str, Any]) -> dict[str, Any]:
    scheduler = raw.get("scheduler")
    return dict(scheduler) if isinstance(scheduler, dict) else {}


def _resolve_roots(
    *,
    repo_root: Path,
    runs_root_argument: str,
    config_argument: str,
    profile: str,
) -> tuple[Path, RealEngineAdmission | None]:
    needs_config = not runs_root_argument.strip() or profile != "fake"
    config_path = (
        discover_shared_config_path(config_argument or None, repo_root) if needs_config else None
    )
    raw: dict[str, Any] = {}
    if config_path is not None:
        _, raw = load_shared_config_mapping(config_path)

    config_root_text = runs_root_from_mapping(raw)
    root_text = runs_root_argument.strip() or config_root_text
    if not root_text:
        raise ValueError(
            "runs_root is required; run 'orca_auto init', configure ORCA_AUTO_CONFIG, "
            "or pass --runs-root"
        )
    runs_root = Path(validated_runs_root_text(root_text)).expanduser().resolve()

    if profile == "fake":
        return runs_root, None
    if config_path is None:
        raise ValueError("real engine smoke requires --config or a discoverable shared config")
    if not config_root_text:
        raise ValueError("real engine smoke requires runs_root in the shared config")
    configured_runs_root = Path(validated_runs_root_text(config_root_text)).expanduser().resolve()
    if runs_root != configured_runs_root:
        raise ValueError("real engine smoke --runs-root must match the shared config runs_root")
    scheduler = _scheduler(raw)
    admission_root = scheduler_admission_root(scheduler, default_runs_root=runs_root)
    if admission_root is None:
        raise ValueError("real engine smoke requires a resolvable scheduler admission root")
    configured_xtb_md_limit = _positive_int(
        scheduler.get("max_active_xtb_md"),
        field="scheduler.max_active_xtb_md",
        default=1,
    )
    scratch_root: Path | None = None
    scratch_min_free_gb: int | None = None
    if profile in {"real-xtb", "all"}:
        xtb_md_config = load_xtb_md_config(str(config_path))
        if xtb_md_config.scratch.enabled:
            scratch_root = Path(xtb_md_config.scratch.root)
            scratch_min_free_gb = xtb_md_config.scratch.min_free_gb
    admission = RealEngineAdmission(
        root=admission_root,
        global_limit=_positive_int(
            scheduler.get("max_active_simulations"),
            field="scheduler.max_active_simulations",
            default=4,
        ),
        xtb_md_limit=(configured_xtb_md_limit if profile in {"real-xtb", "all"} else None),
        scratch_root=scratch_root,
        scratch_min_free_gb=scratch_min_free_gb,
    )
    return runs_root, admission


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orca-auto-smoke",
        description="Run retained ORCA/xTB/workflow smoke scenarios and build a review packet.",
    )
    add_smoke_arguments(parser)
    return parser


def cmd_smoke(args: argparse.Namespace) -> int:
    try:
        repo_root = _source_checkout_root(str(getattr(args, "repo_root", "") or ""))
        runs_root, admission = _resolve_roots(
            repo_root=repo_root,
            runs_root_argument=str(getattr(args, "runs_root", "") or ""),
            config_argument=str(getattr(args, "config", "") or ""),
            profile=str(getattr(args, "profile", "fake") or "fake"),
        )
        result = run_smoke_suite(
            repo_root=repo_root,
            runs_root=runs_root,
            profile=str(getattr(args, "profile", "fake") or "fake"),
            selected_ids=tuple(getattr(args, "scenario", ()) or ()),
            real_engine_admission=admission,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"smoke setup failed: {exc}", file=sys.stderr)
        return 2

    print(f"smoke batch: {result.batch_dir}")
    if result.review is not None:
        print(f"review index: {result.review.index_path}")
        print(f"review summary: {result.review.summary_path}")
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    return cmd_smoke(parser.parse_args(argv))


__all__ = ["build_parser", "cmd_smoke", "main"]
