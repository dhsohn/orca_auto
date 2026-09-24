"""Worker freshness for editable installs: process start versus checkout HEAD.

An editable worker imports straight from a Git checkout, so the code it runs is
dated by the checkout's last HEAD move (its reflog), not by any package
metadata. The verdict compares that move with systemd's record of the unit
start and refuses to judge a working tree that differs from HEAD.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.cli_systemd_evidence import (
    WorkerImportEvidence,
    WorkerVerdict,
    process_identity_race_detail,
    unit_start_epoch,
)
from orca_auto.core.utils.coercion import normalize_text

_CHECKOUT_ERRORS = (OSError, ValueError, RuntimeError)


@dataclass(frozen=True)
class CheckoutHeadEvidence:
    source_root: Path
    head_sha: str
    head_commit_epoch: int
    head_update_epoch: float


def _run_git(
    source_root: Path,
    *git_args: str,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    """Normalized stdout of one git command, or ValueError with its first error line."""
    try:
        completed = run(
            ["git", "-C", str(source_root), *git_args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise ValueError(f"git inspection failed: {exc}") from exc
    if completed.returncode != 0:
        detail = normalize_text(completed.stderr) or normalize_text(completed.stdout)
        raise ValueError(
            detail.splitlines()[0]
            if detail
            else f"git {' '.join(git_args)} exited {completed.returncode}"
        )
    return normalize_text(completed.stdout)


def git_checkout_output(
    source_root: Path,
    *git_args: str,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    all_lines: bool = False,
) -> str:
    value = _run_git(source_root, *git_args, run=run)
    if not value:
        raise ValueError(f"git {' '.join(git_args)} returned no output")
    if all_lines:
        return "\n".join(line.strip() for line in value.splitlines() if line.strip())
    return value.splitlines()[0]


def _parse_reflog_entry(entry: str) -> tuple[str, int, str]:
    reflog_sha, separator, remainder = entry.partition("\0")
    selector, subject_separator, subject = remainder.partition("\0")
    selector_prefix = "HEAD@{"
    if (
        not separator
        or not subject_separator
        or not selector.startswith(selector_prefix)
        or not selector.endswith("}")
    ):
        raise ValueError(f"invalid HEAD reflog entry: {entry!r}")
    try:
        epoch = int(selector[len(selector_prefix) : -1])
    except ValueError as exc:
        raise ValueError(f"invalid HEAD reflog timestamp: {selector!r}") from exc
    if epoch < 0:
        raise ValueError(f"invalid HEAD reflog timestamp: {selector!r}")
    return reflog_sha, epoch, subject.strip()


def head_update_epoch_from_reflog(reflog_text: str, *, head_sha: str) -> int:
    """The time of the newest HEAD reflog entry, which must name HEAD.

    Every entry counts, including one that re-selected the commit already
    checked out: ``git checkout -f main`` while on main and ``git reset --hard
    HEAD`` write the same ``checkout:``/``reset:`` subjects as their no-op
    forms but restore the working tree, and the reflog cannot tell the two
    apart. A worker started before such an entry may be running code the
    checkout no longer has, so the verdict errs toward stale: a plain
    ``git checkout main`` while on main also asks for a restart.
    """
    entries = [line for line in reflog_text.splitlines() if line.strip()]
    if not entries:
        raise ValueError("checkout has no HEAD reflog entry")
    newest_sha, newest_epoch, _subject = _parse_reflog_entry(entries[0])
    if newest_sha != head_sha:
        raise ValueError("latest HEAD reflog entry does not match the checkout's current HEAD")
    return newest_epoch


def checkout_head_evidence(
    observed_root: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> CheckoutHeadEvidence:
    """Snapshot the checkout HEAD and the time this checkout moved to it.

    Commit timestamps describe when a commit object was created, not when a
    checkout deployed it. The latest per-worktree HEAD reflog entry records
    both the deployed SHA and the time this checkout moved to it, including a
    later fast-forward to an older commit object.
    """

    root_text = git_checkout_output(observed_root, "rev-parse", "--show-toplevel", run=run)
    root = Path(root_text).expanduser().resolve(strict=True)
    head_before = git_checkout_output(root, "rev-parse", "--verify", "HEAD^{commit}", run=run)
    reflog_text = git_checkout_output(
        root,
        "reflog",
        "--date=unix",
        "--format=%H%x00%gd%x00%gs",
        run=run,
        all_lines=True,
    )
    head_after = git_checkout_output(root, "rev-parse", "--verify", "HEAD^{commit}", run=run)
    if head_before != head_after:
        raise ValueError("checkout HEAD changed during freshness inspection")
    head_update_epoch = head_update_epoch_from_reflog(reflog_text, head_sha=head_after)
    commit_epoch_text = git_checkout_output(
        root,
        "show",
        "-s",
        "--format=%ct",
        head_after,
        run=run,
    )
    try:
        commit_epoch = int(commit_epoch_text)
    except ValueError as exc:
        raise ValueError(f"invalid HEAD commit timestamp: {commit_epoch_text!r}") from exc
    return CheckoutHeadEvidence(
        source_root=root,
        head_sha=head_after,
        head_commit_epoch=commit_epoch,
        head_update_epoch=float(head_update_epoch),
    )


def tracked_checkout_for_import_source(
    import_source: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> Path | None:
    """The checkout that tracks the imported file, or None for an installed wheel.

    A wheel installed into a virtualenv that happens to live inside a Git
    working directory is untracked there and is not an editable install.
    """
    git_ancestor = next(
        (ancestor for ancestor in import_source.parents if (ancestor / ".git").exists()),
        None,
    )
    if git_ancestor is None:
        return None
    root_text = git_checkout_output(git_ancestor, "rev-parse", "--show-toplevel", run=run)
    root = Path(root_text).expanduser().resolve(strict=True)
    try:
        relative_source = import_source.relative_to(root)
    except ValueError as exc:
        raise ValueError("worker import source is outside its reported Git checkout") from exc
    try:
        git_checkout_output(
            root,
            "ls-files",
            "--error-unmatch",
            "--",
            relative_source.as_posix(),
            run=run,
        )
    except ValueError as exc:
        if any(
            parent.name in {"site-packages", "dist-packages"} for parent in import_source.parents
        ):
            return None
        raise ValueError("worker import source is not tracked by its Git checkout") from exc
    return root


def checkout_import_package_dirty(
    source_root: Path,
    import_source: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> bool:
    """Whether the imported package tree differs from the checkout's HEAD/index."""

    try:
        relative_package = import_source.parent.relative_to(source_root)
    except ValueError as exc:
        raise ValueError("worker import package is outside its reported Git checkout") from exc
    package_path = relative_package.as_posix() or "."
    return bool(
        _run_git(
            source_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            package_path,
            run=run,
        )
    )


def judge_checkout_worker(
    *,
    label: str,
    unit: str,
    pid: int,
    evidence: WorkerImportEvidence,
    checkout_root: Path,
    run: Callable[..., subprocess.CompletedProcess[Any]],
    read_process_file: Callable[[str], bytes],
) -> WorkerVerdict:
    """Judge one editable worker: unit start versus the checkout's HEAD update."""
    base_row: dict[str, Any] = {"label": label, "unit": unit}
    import_source = evidence.import_source

    def race_detail() -> str:
        return process_identity_race_detail(
            unit,
            pid=pid,
            process_start_ticks=evidence.process_start_ticks,
            run=run,
            read_process_file=read_process_file,
        )

    try:
        started_epoch = unit_start_epoch(unit, run=run)
    except ValueError as exc:
        return WorkerVerdict(
            "undetermined",
            {
                **base_row,
                "pid": pid,
                "source_root": str(checkout_root),
                "detail": f"cannot read unit start time: {exc}",
            },
        )
    detail = race_detail()
    if detail:
        return WorkerVerdict(
            "undetermined",
            {**base_row, "source_root": str(checkout_root), "detail": detail},
        )

    # A checkout can move while this command is observing another worker,
    # and an editable package can diverge from HEAD without moving it at
    # all. Snapshot both for each process instead of reusing an earlier
    # worker's evidence or treating a dirty source tree as proven fresh.
    try:
        dirty_before = checkout_import_package_dirty(checkout_root, import_source, run=run)
        head_evidence = checkout_head_evidence(checkout_root, run=run)
        dirty_after = checkout_import_package_dirty(checkout_root, import_source, run=run)
    except _CHECKOUT_ERRORS as exc:
        return WorkerVerdict(
            "undetermined",
            {
                **base_row,
                "pid": pid,
                "source_root": str(checkout_root),
                "detail": f"cannot read checkout HEAD: {exc}",
            },
        )
    if dirty_before or dirty_after:
        return WorkerVerdict(
            "undetermined",
            {
                **base_row,
                "pid": pid,
                "source_root": str(checkout_root),
                "import_source": str(import_source),
                "detail": "worker import package has uncommitted source changes",
            },
        )

    detail = race_detail()
    if detail:
        return WorkerVerdict(
            "undetermined",
            {**base_row, "source_root": str(head_evidence.source_root), "detail": detail},
        )
    worker_row = {
        **base_row,
        "pid": pid,
        "started_epoch": int(started_epoch),
        "source_root": str(head_evidence.source_root),
        "head_sha": head_evidence.head_sha,
        "head_commit_epoch": head_evidence.head_commit_epoch,
        "head_update_epoch": head_evidence.head_update_epoch,
        "import_source": str(import_source),
        "process_start_ticks": evidence.process_start_ticks,
    }
    # systemd's formatted start timestamp has one-second precision. Treat an
    # equal-second checkout update conservatively rather than allowing a
    # timing truncation to produce a false-fresh verdict.
    return WorkerVerdict(
        "worker", worker_row, stale=started_epoch <= head_evidence.head_update_epoch
    )


__all__ = [
    "CheckoutHeadEvidence",
    "checkout_head_evidence",
    "checkout_import_package_dirty",
    "git_checkout_output",
    "head_update_epoch_from_reflog",
    "judge_checkout_worker",
    "tracked_checkout_for_import_source",
]
