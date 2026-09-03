from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orca_auto import cli_systemd_units
from orca_auto._process_evidence import PROCESS_IMPORT_SOURCE_ENV
from orca_auto.core.utils.coercion import normalize_text

_WORKER_PROCESS_LABELS = frozenset({"worker", "workflow"})


def _unit_main_pid(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    try:
        completed = cli_systemd_units._show_unit_property(unit, "MainPID", run=run)
    except OSError:
        return 0
    try:
        return int(cli_systemd_units._single_line_command_output(completed))
    except ValueError:
        return 0


def _unit_start_epoch(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> float:
    """Epoch at which the unit's main process started, from systemd's record.

    systemd snapshots CLOCK_REALTIME when it forks the main process, so the
    value stays true after later clock steps. Deriving the start from
    ``/proc/<pid>/stat`` ticks plus ``btime`` does not: ``btime`` is recomputed
    from the current wall clock minus the monotonic uptime, and on WSL2 the
    wall clock is stepped forward after host sleeps while the monotonic clock
    stood still, which shifts every derived start time forward and can mask a
    genuinely stale worker (observed live: +32 min).
    """
    try:
        completed = cli_systemd_units._show_unit_property(
            unit,
            "ExecMainStartTimestamp",
            run=run,
            extra_args=("--timestamp=utc",),
        )
    except OSError as exc:
        raise ValueError(f"systemctl show failed: {exc}") from exc
    value = cli_systemd_units._single_line_command_output(completed)
    # "Mon 2026-08-03 09:33:12 UTC" — parse without the weekday token so the
    # verdict does not depend on the CLI locale.
    tokens = value.split()
    if len(tokens) != 4 or tokens[3] != "UTC":
        raise ValueError(f"unrecognized ExecMainStartTimestamp: {value!r}")
    started = datetime.strptime(f"{tokens[1]} {tokens[2]}", "%Y-%m-%d %H:%M:%S")
    return started.replace(tzinfo=UTC).timestamp()


@dataclass(frozen=True)
class _CheckoutHeadEvidence:
    source_root: Path
    head_sha: str
    head_commit_epoch: int
    head_update_epoch: float


def _git_checkout_output(
    source_root: Path,
    *git_args: str,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    all_lines: bool = False,
) -> str:
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
    value = normalize_text(completed.stdout)
    if not value:
        raise ValueError(f"git {' '.join(git_args)} returned no output")
    if all_lines:
        return "\n".join(line.strip() for line in value.splitlines() if line.strip())
    return value.splitlines()[0]


def _checkout_head_evidence(
    observed_root: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> _CheckoutHeadEvidence:
    """Snapshot the checkout HEAD and the time this checkout moved to it.

    Commit timestamps describe when a commit object was created, not when a
    checkout deployed it. The latest per-worktree HEAD reflog entry records
    both the deployed SHA and the time this checkout moved to it, including a
    later fast-forward to an older commit object.
    """

    root_text = _git_checkout_output(observed_root, "rev-parse", "--show-toplevel", run=run)
    root = Path(root_text).expanduser().resolve(strict=True)
    head_before = _git_checkout_output(root, "rev-parse", "--verify", "HEAD^{commit}", run=run)
    reflog_text = _git_checkout_output(
        root,
        "reflog",
        "--date=unix",
        "--format=%H%x00%gd",
        run=run,
        all_lines=True,
    )
    head_after = _git_checkout_output(root, "rev-parse", "--verify", "HEAD^{commit}", run=run)
    if head_before != head_after:
        raise ValueError("checkout HEAD changed during freshness inspection")
    head_update_epoch = _head_update_epoch_from_reflog(reflog_text, head_sha=head_after)
    commit_epoch_text = _git_checkout_output(
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
    return _CheckoutHeadEvidence(
        source_root=root,
        head_sha=head_after,
        head_commit_epoch=commit_epoch,
        head_update_epoch=float(head_update_epoch),
    )


def _parse_reflog_entry(entry: str) -> tuple[str, int]:
    reflog_sha, separator, selector = entry.partition("\0")
    selector_prefix = "HEAD@{"
    if not separator or not selector.startswith(selector_prefix) or not selector.endswith("}"):
        raise ValueError(f"invalid HEAD reflog entry: {entry!r}")
    try:
        epoch = int(selector[len(selector_prefix) : -1])
    except ValueError as exc:
        raise ValueError(f"invalid HEAD reflog timestamp: {selector!r}") from exc
    if epoch < 0:
        raise ValueError(f"invalid HEAD reflog timestamp: {selector!r}")
    return reflog_sha, epoch


def _head_update_epoch_from_reflog(reflog_text: str, *, head_sha: str) -> int:
    """The time this checkout first moved to its current HEAD commit.

    The newest reflog entry must name HEAD. Newer entries that re-select the
    same commit (``git checkout main`` while on main, ``git reset --hard HEAD``,
    a ``git switch -`` round trip) are not deploys: a worker that imported this
    commit before them is still fresh, so the update time is taken from the
    oldest entry of the newest run of entries that all name HEAD.
    """
    entries = [line for line in reflog_text.splitlines() if line.strip()]
    if not entries:
        raise ValueError("checkout has no HEAD reflog entry")
    newest_sha, newest_epoch = _parse_reflog_entry(entries[0])
    if newest_sha != head_sha:
        raise ValueError("latest HEAD reflog entry does not match the checkout's current HEAD")
    update_epoch = newest_epoch
    for entry in entries[1:]:
        entry_sha, entry_epoch = _parse_reflog_entry(entry)
        if entry_sha != head_sha:
            break
        update_epoch = min(update_epoch, entry_epoch)
    return update_epoch


def _read_process_file(path: str) -> bytes:
    return Path(path).read_bytes()


def _process_start_ticks(raw_stat: bytes, *, pid: int) -> int:
    # Linux proc(5) makes comm parenthesized and permits spaces (and closing
    # parentheses) inside it. Split after the final ')' so field 22 remains the
    # twentieth token in the remainder (which starts at field 3).
    closing_paren = raw_stat.rfind(b")")
    fields = raw_stat[closing_paren + 1 :].split() if closing_paren >= 0 else []
    if len(fields) <= 19:
        raise ValueError(f"invalid /proc/{pid}/stat process identity")
    try:
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise ValueError(f"invalid /proc/{pid}/stat process identity") from exc
    if start_ticks <= 0:
        raise ValueError(f"invalid /proc/{pid}/stat process identity")
    return start_ticks


def _read_process_start_ticks(
    pid: int,
    *,
    read_process_file: Callable[[str], bytes] = _read_process_file,
) -> int:
    try:
        raw_stat = read_process_file(f"/proc/{pid}/stat")
    except OSError as exc:
        raise ValueError(f"cannot read /proc/{pid}/stat: {exc}") from exc
    return _process_start_ticks(raw_stat, pid=pid)


@dataclass(frozen=True)
class _WorkerImportEvidence:
    import_source: Path
    process_start_ticks: int


def _worker_process_import_evidence(
    pid: int,
    *,
    read_process_file: Callable[[str], bytes] = _read_process_file,
) -> _WorkerImportEvidence:
    start_ticks_before = _read_process_start_ticks(pid, read_process_file=read_process_file)
    try:
        raw_environ = read_process_file(f"/proc/{pid}/environ")
    except OSError as exc:
        raise ValueError(f"cannot read /proc/{pid}/environ: {exc}") from exc
    prefix = f"{PROCESS_IMPORT_SOURCE_ENV}=".encode()
    values = [
        entry[len(prefix) :] for entry in raw_environ.split(b"\0") if entry.startswith(prefix)
    ]
    if len(values) != 1 or not values[0]:
        raise ValueError("worker import-source evidence is missing or ambiguous")
    import_source = Path(os.fsdecode(values[0])).expanduser()
    if not import_source.is_absolute():
        raise ValueError(f"worker import source is not absolute: {import_source!s}")
    try:
        import_source = import_source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve worker import source {import_source}: {exc}") from exc
    if not import_source.is_file():
        raise ValueError(f"worker import source is not a file: {import_source}")
    start_ticks_after = _read_process_start_ticks(pid, read_process_file=read_process_file)
    if start_ticks_after != start_ticks_before:
        raise ValueError("worker process identity changed during freshness inspection")
    return _WorkerImportEvidence(
        import_source=import_source,
        process_start_ticks=start_ticks_before,
    )


def _tracked_checkout_for_import_source(
    import_source: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> Path | None:
    git_ancestor = next(
        (ancestor for ancestor in import_source.parents if (ancestor / ".git").exists()),
        None,
    )
    if git_ancestor is None:
        return None
    root_text = _git_checkout_output(git_ancestor, "rev-parse", "--show-toplevel", run=run)
    root = Path(root_text).expanduser().resolve(strict=True)
    try:
        relative_source = import_source.relative_to(root)
    except ValueError as exc:
        raise ValueError("worker import source is outside its reported Git checkout") from exc
    try:
        _git_checkout_output(
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


def _checkout_import_package_dirty(
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
    git_args = (
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        package_path,
    )
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
    return bool(normalize_text(completed.stdout))


def _process_identity_race_detail(
    unit: str,
    *,
    pid: int,
    process_start_ticks: int | None,
    run: Callable[..., subprocess.CompletedProcess[Any]],
    read_process_file: Callable[[str], bytes],
) -> str:
    if _unit_main_pid(unit, run=run) != pid:
        return "main PID changed during freshness inspection"
    # Without import evidence there are no start ticks to recheck; the PID
    # comparison above is the whole race check for that caller.
    if process_start_ticks is None:
        return ""
    try:
        observed_ticks = _read_process_start_ticks(pid, read_process_file=read_process_file)
    except ValueError as exc:
        return f"cannot re-read worker process identity: {exc}"
    if observed_ticks != process_start_ticks:
        return "worker process identity changed during freshness inspection"
    return ""


def _common_evidence_value(rows: Sequence[dict[str, Any]], key: str) -> Any:
    values = {row[key] for row in rows if row.get(key) is not None}
    return values.pop() if len(values) == 1 else None


def _worker_staleness_payload(
    *,
    workers: list[dict[str, Any]],
    stale: list[dict[str, Any]],
    undetermined: list[dict[str, Any]],
    uncompared: list[dict[str, Any]] | None = None,
    aggregate_evidence: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence_rows = aggregate_evidence if aggregate_evidence is not None else workers
    return {
        # Keep the original aggregate field for JSON consumers. It is now
        # informational only; the verdict uses each checkout's HEAD update time.
        "head_commit_epoch": _common_evidence_value(evidence_rows, "head_commit_epoch"),
        "source_root": _common_evidence_value(evidence_rows, "source_root"),
        "head_sha": _common_evidence_value(evidence_rows, "head_sha"),
        "head_update_epoch": _common_evidence_value(evidence_rows, "head_update_epoch"),
        "workers": workers,
        "stale": stale,
        "undetermined": undetermined,
        "uncompared": uncompared or [],
    }


def collect_worker_staleness(
    statuses: Sequence[cli_systemd_units.ServiceUnitStatus],
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    source_root: Path | None = None,
    read_process_file: Callable[[str], bytes] = _read_process_file,
) -> dict[str, Any] | None:
    """Compare each active worker process against its checkout's HEAD update.

    The module CLI re-execs once with the resolved source file it actually
    imported in its process environment. That evidence, not the process cwd,
    selects the checkout whose HEAD reflog is compared with the unit start.
    PID plus kernel process-start ticks are rechecked around the observation so
    a restart or PID reuse becomes undetermined rather than false-fresh. A
    source imported from an installed wheel is not comparable; an all-wheel set
    returns ``None``, while mixed sets still judge tracked editable checkouts.

    ``source_root`` is retained as a test/diagnostic override. Production calls
    leave it unset and inspect process-bound import evidence for every worker.
    """
    active_workers = tuple(
        status
        for status in statuses
        if status.label in _WORKER_PROCESS_LABELS and status.active == "active"
    )
    override_root = source_root.expanduser().resolve(strict=False) if source_root else None
    if not active_workers:
        if override_root is None:
            return None
        if not (override_root / ".git").exists():
            return None
        try:
            evidence = _checkout_head_evidence(override_root, run=run)
        except (OSError, ValueError, RuntimeError) as exc:
            return _worker_staleness_payload(
                workers=[],
                stale=[],
                undetermined=[
                    {"label": "", "unit": "", "detail": f"cannot read checkout HEAD: {exc}"}
                ],
            )
        evidence_row = {
            "source_root": str(evidence.source_root),
            "head_sha": evidence.head_sha,
            "head_commit_epoch": evidence.head_commit_epoch,
            "head_update_epoch": evidence.head_update_epoch,
        }
        return _worker_staleness_payload(
            workers=[],
            stale=[],
            undetermined=[],
            uncompared=[],
            aggregate_evidence=[evidence_row],
        )

    workers: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    undetermined: list[dict[str, Any]] = []
    uncompared: list[dict[str, Any]] = []
    for status in active_workers:
        pid_before = _unit_main_pid(status.unit, run=run)
        if pid_before <= 0:
            undetermined.append(
                {
                    "label": status.label,
                    "unit": status.unit,
                    "detail": "no readable main PID",
                }
            )
            continue
        import_evidence: _WorkerImportEvidence | None = None
        import_source: Path | None = None
        observed_root: Path | None
        if override_root is not None:
            observed_root = override_root
            race_detail = _process_identity_race_detail(
                status.unit,
                pid=pid_before,
                process_start_ticks=None,
                run=run,
                read_process_file=read_process_file,
            )
        else:
            try:
                import_evidence = _worker_process_import_evidence(
                    pid_before,
                    read_process_file=read_process_file,
                )
                import_source = import_evidence.import_source
                observed_root = _tracked_checkout_for_import_source(import_source, run=run)
            except (OSError, ValueError, RuntimeError) as exc:
                undetermined.append(
                    {
                        "label": status.label,
                        "unit": status.unit,
                        "pid": pid_before,
                        "detail": f"cannot identify worker checkout: {exc}",
                    }
                )
                continue
            race_detail = _process_identity_race_detail(
                status.unit,
                pid=pid_before,
                process_start_ticks=import_evidence.process_start_ticks,
                run=run,
                read_process_file=read_process_file,
            )
        if race_detail:
            row: dict[str, Any] = {
                "label": status.label,
                "unit": status.unit,
                "detail": race_detail,
            }
            if import_source is not None:
                row["import_source"] = str(import_source)
            elif observed_root is not None:
                row["source_root"] = str(observed_root)
            undetermined.append(row)
            continue

        # Wheel installs have no checkout HEAD to compare. Preserve the public
        # null/not-applicable contract instead of turning a healthy wheel worker
        # into an undetermined deployment, even when systemd cannot render a
        # start timestamp that is irrelevant without checkout evidence. In a
        # mixed deployment, keep judging git-backed workers and expose the wheel
        # worker as additive evidence.
        if observed_root is None:
            assert import_source is not None
            uncompared.append(
                {
                    "label": status.label,
                    "unit": status.unit,
                    "pid": pid_before,
                    "source_root": str(import_source.parent),
                    "import_source": str(import_source),
                    "reason": "installed_distribution",
                }
            )
            continue

        try:
            started_epoch = _unit_start_epoch(status.unit, run=run)
        except ValueError as exc:
            undetermined.append(
                {
                    "label": status.label,
                    "unit": status.unit,
                    "pid": pid_before,
                    "source_root": str(observed_root),
                    "detail": f"cannot read unit start time: {exc}",
                }
            )
            continue
        race_detail = _process_identity_race_detail(
            status.unit,
            pid=pid_before,
            process_start_ticks=(
                None if import_evidence is None else import_evidence.process_start_ticks
            ),
            run=run,
            read_process_file=read_process_file,
        )
        if race_detail:
            undetermined.append(
                {
                    "label": status.label,
                    "unit": status.unit,
                    "source_root": str(observed_root),
                    "detail": race_detail,
                }
            )
            continue

        # A checkout can move while this command is observing another worker,
        # and an editable package can diverge from HEAD without moving it at
        # all. Snapshot both for each process instead of reusing an earlier
        # worker's evidence or treating a dirty source tree as proven fresh.
        try:
            dirty_before = (
                _checkout_import_package_dirty(observed_root, import_source, run=run)
                if import_source is not None
                else False
            )
            head_evidence = _checkout_head_evidence(observed_root, run=run)
            dirty_after = (
                _checkout_import_package_dirty(observed_root, import_source, run=run)
                if import_source is not None
                else False
            )
        except (OSError, ValueError, RuntimeError) as exc:
            undetermined.append(
                {
                    "label": status.label,
                    "unit": status.unit,
                    "pid": pid_before,
                    "source_root": str(observed_root),
                    "detail": f"cannot read checkout HEAD: {exc}",
                }
            )
            continue
        if dirty_before or dirty_after:
            undetermined.append(
                {
                    "label": status.label,
                    "unit": status.unit,
                    "pid": pid_before,
                    "source_root": str(observed_root),
                    "import_source": str(import_source),
                    "detail": "worker import package has uncommitted source changes",
                }
            )
            continue

        race_detail = _process_identity_race_detail(
            status.unit,
            pid=pid_before,
            process_start_ticks=(
                None if import_evidence is None else import_evidence.process_start_ticks
            ),
            run=run,
            read_process_file=read_process_file,
        )
        if race_detail:
            undetermined.append(
                {
                    "label": status.label,
                    "unit": status.unit,
                    "source_root": str(head_evidence.source_root),
                    "detail": race_detail,
                }
            )
            continue
        worker_row = {
            "label": status.label,
            "unit": status.unit,
            "pid": pid_before,
            "started_epoch": int(started_epoch),
            "source_root": str(head_evidence.source_root),
            "head_sha": head_evidence.head_sha,
            "head_commit_epoch": head_evidence.head_commit_epoch,
            "head_update_epoch": head_evidence.head_update_epoch,
        }
        if import_evidence is not None and import_source is not None:
            worker_row["import_source"] = str(import_source)
            worker_row["process_start_ticks"] = import_evidence.process_start_ticks
        workers.append(worker_row)
        # systemd's formatted start timestamp has one-second precision. Treat an
        # equal-second checkout update conservatively rather than allowing a
        # timing truncation to produce a false-fresh verdict.
        if started_epoch <= head_evidence.head_update_epoch:
            stale.append(dict(worker_row))
    if uncompared and not workers and not undetermined:
        return None
    return _worker_staleness_payload(
        workers=workers,
        stale=stale,
        undetermined=undetermined,
        uncompared=uncompared,
    )


__all__ = ["collect_worker_staleness"]
