from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orca_auto import cli_style
from orca_auto._process_evidence import PROCESS_IMPORT_SOURCE_ENV
from orca_auto._version import installed_version_drift
from orca_auto.cli_errors import emit_error
from orca_auto.cli_systemd_apply import _run_command
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.systemd_plan import (
    _engine_workers_unit_for_user,
    _is_root,
    _runtime_unit_for_user,
    _worker_unit_for_user,
    _workflow_worker_unit_for_user,
)

SERVICE_UNIT_ORDER = (
    ("runtime", "orca_auto-runtime@{user}.target"),
    ("engines", "orca_auto-engine-workers@{user}.target"),
    ("worker", "orca_auto-queue-worker@{user}.service"),
    ("workflow", "orca_auto-workflow-worker@{user}.service"),
)
_ENABLED_UNIT_FILE_STATES = frozenset({"enabled", "enabled-runtime"})
_READABLE_UNIT_FILE_STATES = frozenset(
    {
        "alias",
        "disabled",
        "enabled",
        "enabled-runtime",
        "generated",
        "indirect",
        "linked",
        "linked-runtime",
        "masked",
        "masked-runtime",
        "static",
        "transient",
    }
)


@dataclass(frozen=True)
class ServiceUnitStatus:
    label: str
    unit: str
    active: str
    enabled: str


def _default_service_user() -> str:
    # These commands act on system units, so operators reach for `sudo
    # orca_auto service ...`. getpass.getuser() reports root under sudo, and
    # every unit name then resolves to an @root instance nobody installed.
    # systemd treats those as success rather than error -- `reset-failed`
    # exits 0 on a unit it reports as "not loaded" -- so the command claims to
    # have restarted workers it never touched. Template units cannot catch this
    # either: they load for any instance name. Prefer the invoking account.
    if _is_root():
        invoking_user = normalize_text(os.environ.get("SUDO_USER"))
        if invoking_user and invoking_user != "root":
            return invoking_user
    return getpass.getuser()


def _service_units_for_user(target_user: str) -> tuple[tuple[str, str], ...]:
    user_text = normalize_text(target_user)
    if not user_text:
        raise ValueError("service user is required")
    return tuple((label, template.format(user=user_text)) for label, template in SERVICE_UNIT_ORDER)


def _single_line_command_output(completed: subprocess.CompletedProcess[Any]) -> str:
    output = normalize_text(completed.stdout)
    if not output:
        output = normalize_text(completed.stderr)
    if not output:
        output = f"exit {completed.returncode}"
    return output.splitlines()[0]


def _query_systemctl(
    action: str,
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    try:
        completed = run(
            ["systemctl", action, unit],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return f"error: {exc}"
    return _single_line_command_output(completed)


def _unit_load_state(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    try:
        completed = run(
            ["systemctl", "show", "--property=LoadState", "--value", unit],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return f"error: {exc}"
    return _single_line_command_output(completed)


def collect_service_status(
    target_user: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[ServiceUnitStatus, ...]:
    return tuple(
        ServiceUnitStatus(
            label=label,
            unit=unit,
            active=_query_systemctl("is-active", unit, run=run),
            enabled=_query_systemctl("is-enabled", unit, run=run),
        )
        for label, unit in _service_units_for_user(target_user)
    )


_SERVICE_ACTIVE_COLORS = {
    "active": cli_style.GREEN,
    "failed": cli_style.RED,
    "inactive": cli_style.DIM,
    "dead": cli_style.DIM,
}


def _service_active_color(value: str) -> str:
    return _SERVICE_ACTIVE_COLORS.get(value.strip().lower(), cli_style.YELLOW)


def _paint_field(text: str, width: int, color: str | None) -> str:
    padded = f"{text:<{width}}"
    return cli_style.paint(padded, color) if color else padded


def _print_service_status(target_user: str, statuses: Sequence[ServiceUnitStatus]) -> None:
    print(f"orca_auto service status for {target_user} ({_selected_service_mode(statuses)}):")
    print(cli_style.paint(f"{'Name':<10} {'Active':<14} Unit", cli_style.BOLD))
    for status in statuses:
        active = _paint_field(status.active, 14, _service_active_color(status.active))
        print(f"{status.label:<10} {active} {status.unit}")


def _service_status_payload(
    target_user: str,
    statuses: Sequence[ServiceUnitStatus],
    drift: tuple[str, str] | None = None,
    staleness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mode = _selected_service_mode(statuses)
    required_labels = _required_service_labels(mode)
    return {
        "target_user": target_user,
        "mode": mode,
        "ok": _required_services_active(statuses, required_labels=required_labels),
        "worker_staleness": staleness,
        "version_drift": (
            None
            if drift is None
            else {
                "installed": drift[0],
                "source": drift[1],
                # A host can hold several editable installs of one checkout —
                # here, the units' virtualenv and the operator's shell — so the
                # verdict is only meaningful with the interpreter it describes.
                "interpreter": sys.executable,
            }
        ),
        "services": [
            {
                "label": status.label,
                "unit": status.unit,
                "active": status.active,
                "enabled": status.enabled,
                "required": status.label in required_labels,
            }
            for status in statuses
        ],
    }


def _selected_service_mode(statuses: Sequence[ServiceUnitStatus]) -> str:
    by_label = {status.label: status for status in statuses}
    runtime = by_label.get("runtime")
    engines = by_label.get("engines")
    worker = by_label.get("worker")
    if runtime is not None and runtime.enabled in _ENABLED_UNIT_FILE_STATES:
        return "full"
    if engines is not None and engines.enabled in _ENABLED_UNIT_FILE_STATES:
        return "worker-only"
    # Recognize the previous direct worker boot selection as worker-only, but
    # health remains false until the new engine-worker target is installed.
    if worker is not None and worker.enabled in _ENABLED_UNIT_FILE_STATES:
        return "worker-only"
    enabled_states = tuple(
        status.enabled for status in (runtime, engines, worker) if status is not None
    )
    # Fall back to the live graph only when the boot selection cannot be read.
    if (
        enabled_states
        and not all(state in _READABLE_UNIT_FILE_STATES for state in enabled_states)
        and runtime is not None
        and runtime.active == "active"
    ):
        return "full"
    return "worker-only"


def _required_service_labels(mode: str) -> frozenset[str]:
    if mode == "full":
        return frozenset({"runtime", "engines", "worker"})
    return frozenset({"engines", "worker"})


def _required_services_active(
    statuses: Sequence[ServiceUnitStatus], *, required_labels: frozenset[str]
) -> bool:
    by_label = {status.label: status for status in statuses}
    return all(
        label in by_label and by_label[label].active == "active" for label in required_labels
    )


_WORKER_PROCESS_LABELS = frozenset({"worker", "workflow"})


def _unit_main_pid(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    try:
        completed = run(
            ["systemctl", "show", "--property=MainPID", "--value", unit],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return 0
    try:
        return int(_single_line_command_output(completed))
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
        completed = run(
            [
                "systemctl",
                "show",
                "--property=ExecMainStartTimestamp",
                "--value",
                "--timestamp=utc",
                unit,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise ValueError(f"systemctl show failed: {exc}") from exc
    value = _single_line_command_output(completed)
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
    reflog_entry = _git_checkout_output(
        root,
        "reflog",
        "-1",
        "--date=unix",
        "--format=%H%x00%gd",
        run=run,
    )
    head_after = _git_checkout_output(root, "rev-parse", "--verify", "HEAD^{commit}", run=run)
    if head_before != head_after:
        raise ValueError("checkout HEAD changed during freshness inspection")
    reflog_sha, separator, selector = reflog_entry.partition("\0")
    selector_prefix = "HEAD@{"
    if not separator or not selector.startswith(selector_prefix) or not selector.endswith("}"):
        raise ValueError(f"invalid HEAD reflog entry: {reflog_entry!r}")
    try:
        head_update_epoch = int(selector[len(selector_prefix) : -1])
    except ValueError as exc:
        raise ValueError(f"invalid HEAD reflog timestamp: {selector!r}") from exc
    if head_update_epoch < 0:
        raise ValueError(f"invalid HEAD reflog timestamp: {selector!r}")
    if reflog_sha != head_after:
        raise ValueError("latest HEAD reflog entry does not match the checkout's current HEAD")
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
    process_start_ticks: int,
    run: Callable[..., subprocess.CompletedProcess[Any]],
    read_process_file: Callable[[str], bytes],
) -> str:
    if _unit_main_pid(unit, run=run) != pid:
        return "main PID changed during freshness inspection"
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


def _epoch_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_worker_staleness(
    statuses: Sequence[ServiceUnitStatus],
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
            race_detail = (
                "main PID changed during freshness inspection"
                if _unit_main_pid(status.unit, run=run) != pid_before
                else ""
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
        race_detail = (
            _process_identity_race_detail(
                status.unit,
                pid=pid_before,
                process_start_ticks=import_evidence.process_start_ticks,
                run=run,
                read_process_file=read_process_file,
            )
            if import_evidence is not None
            else (
                "main PID changed during freshness inspection"
                if _unit_main_pid(status.unit, run=run) != pid_before
                else ""
            )
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

        race_detail = (
            _process_identity_race_detail(
                status.unit,
                pid=pid_before,
                process_start_ticks=import_evidence.process_start_ticks,
                run=run,
                read_process_file=read_process_file,
            )
            if import_evidence is not None
            else (
                "main PID changed during freshness inspection"
                if _unit_main_pid(status.unit, run=run) != pid_before
                else ""
            )
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


def _systemctl_available(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    return which("systemctl") is not None


def _sudo_available(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    return which("sudo") is not None


# A unit reaches any of these only because someone started it, so restarting it
# honors the opt-in rather than overriding it -- including out of the crash loop
# and tripped start limit a bad deploy leaves behind, which is the state the
# restart exists to clear.
_WORKFLOW_RUNNING_STATES = frozenset({"active", "activating", "reloading", "failed"})
# Stopped, or being stopped by the operator right now: leave it alone.
_WORKFLOW_STOPPED_STATES = frozenset({"inactive", "deactivating", "unknown", ""})
_WORKFLOW_STATE_RETURN_CODES = {
    "active": frozenset({0}),
    "activating": frozenset({0}),
    "reloading": frozenset({0}),
    "failed": frozenset({3}),
    "inactive": frozenset({3}),
    "deactivating": frozenset({0, 3}),
    "unknown": frozenset({3}),
    "": frozenset({3}),
}


def _query_workflow_worker_state(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    try:
        completed = run(
            ["systemctl", "is-active", unit],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise ValueError(f"systemctl failed: {exc}") from exc
    stdout = normalize_text(completed.stdout)
    stderr = normalize_text(completed.stderr)
    state = stdout.splitlines()[0] if stdout else ""
    expected_returncodes = _WORKFLOW_STATE_RETURN_CODES.get(state)
    if stderr or expected_returncodes is None or completed.returncode not in expected_returncodes:
        detail = stderr.splitlines()[0] if stderr else state or f"exit {completed.returncode}"
        raise ValueError(f"systemctl answered {detail!r} (exit {completed.returncode})")
    return state


def _restartable_worker_units(
    target_user: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[str, ...]:
    """Worker services a restart must reload code in, in restart order.

    The workflow worker is opt-in and belongs to no target, so no target restart
    reaches it. It joins the list once it is running, and is never started from
    a stopped state. An unreadable state is not a licence to guess: skipping it
    would report success over a worker still running pre-deploy code.
    """

    units = [_worker_unit_for_user(target_user)]
    workflow_unit = _workflow_worker_unit_for_user(target_user)
    try:
        state = _query_workflow_worker_state(workflow_unit, run=run)
    except ValueError as exc:
        raise ValueError(
            f"cannot tell whether {workflow_unit} is running; {exc}. "
            "Restart it yourself, or rerun once systemctl responds."
        ) from exc
    if state in _WORKFLOW_RUNNING_STATES:
        units.append(workflow_unit)
    elif state not in _WORKFLOW_STOPPED_STATES:
        raise ValueError(
            f"cannot tell whether {workflow_unit} is running; systemctl answered "
            f"{state!r}. Restart it yourself, or rerun once systemctl responds."
        )
    return tuple(units)


def _require_current_restart_units(
    target_user: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    required_units = (_engine_workers_unit_for_user(target_user),)
    missing = tuple(
        unit for unit in required_units if _unit_load_state(unit, run=run) == "not-found"
    )
    if not missing:
        return
    raise ValueError(
        "required systemd units are not installed: "
        f"{', '.join(missing)}. Rerun the installer for this checkout: "
        f"orca_auto systemd install --user {target_user} --repo <repo>"
    )


def _restart_unit_for_user(
    target_user: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    _require_current_restart_units(target_user, run=run)
    runtime_unit = _runtime_unit_for_user(target_user)
    engines_unit = _engine_workers_unit_for_user(target_user)
    worker_unit = _worker_unit_for_user(target_user)
    runtime_enabled = _query_systemctl("is-enabled", runtime_unit, run=run)
    if runtime_enabled in _ENABLED_UNIT_FILE_STATES:
        return runtime_unit
    engines_enabled = _query_systemctl("is-enabled", engines_unit, run=run)
    if engines_enabled in _ENABLED_UNIT_FILE_STATES:
        return engines_unit
    worker_enabled = _query_systemctl("is-enabled", worker_unit, run=run)
    if worker_enabled in _ENABLED_UNIT_FILE_STATES:
        return engines_unit
    if not all(
        state in _READABLE_UNIT_FILE_STATES
        for state in (runtime_enabled, engines_enabled, worker_enabled)
    ):
        runtime_active = _query_systemctl("is-active", runtime_unit, run=run)
        if runtime_active == "active":
            return runtime_unit
    return engines_unit


@dataclass(frozen=True)
class ServiceCliDeps:
    """Optional overrides for system-effect seams (test injection)."""

    run: Callable[..., subprocess.CompletedProcess[Any]] | None = None
    which: Callable[[str], str | None] | None = None
    is_root: Callable[[], bool] | None = None
    default_service_user: Callable[[], str] | None = None
    collect_service_status: Callable[..., tuple[ServiceUnitStatus, ...]] | None = None
    restart_unit_for_user: Callable[..., str] | None = None
    installed_version_drift: Callable[[], tuple[str, str] | None] | None = None
    collect_worker_staleness: Callable[..., dict[str, Any] | None] | None = None


def _service_target_user(args: argparse.Namespace, deps: ServiceCliDeps) -> str:
    default_user = deps.default_service_user or _default_service_user
    return normalize_text(getattr(args, "target_user", None)) or normalize_text(default_user())


def cmd_service_status(args: argparse.Namespace, *, deps: ServiceCliDeps | None = None) -> int:
    deps = deps or ServiceCliDeps()
    which = deps.which or shutil.which
    collect_status = deps.collect_service_status or collect_service_status
    if not _systemctl_available(which=which):
        emit_error("systemctl is not available in this environment")
        return 1

    target_user = _service_target_user(args, deps)
    try:
        statuses = collect_status(target_user, run=deps.run or subprocess.run)
    except ValueError as exc:
        emit_error(exc)
        return 1
    drift = (deps.installed_version_drift or installed_version_drift)()
    staleness = (deps.collect_worker_staleness or collect_worker_staleness)(
        statuses, run=deps.run or subprocess.run
    )
    payload = _service_status_payload(target_user, statuses, drift, staleness)
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        _print_service_status(target_user, statuses)
    if drift is not None:
        # This interpreter runs the checkout's code but declares the version its
        # last install froze, so every version it reports is wrong until the
        # editable install is refreshed. The verdict covers only the interpreter
        # that ran this command, which need not be the one the units run.
        installed, source = drift
        emit_error(
            f"{sys.executable} declares orca_auto {installed} but runs the source tree at {source}",
            hint=f"rerun `{sys.executable} -m pip install -e .`",
        )
    staleness_ok = staleness is None or not (staleness["stale"] or staleness["undetermined"])
    if staleness is not None:
        for entry in staleness["stale"]:
            # Legacy injected/test payloads only carry head_commit_epoch. New
            # collector payloads attach the checkout update evidence per worker.
            head_update_epoch = float(
                entry.get("head_update_epoch")
                or staleness.get("head_update_epoch")
                or staleness.get("head_commit_epoch")
                or 0
            )
            source_detail = (
                f" in {entry['source_root']}" if normalize_text(entry.get("source_root")) else ""
            )
            sha_detail = (
                f" ({normalize_text(entry.get('head_sha'))[:12]})"
                if normalize_text(entry.get("head_sha"))
                else ""
            )
            emit_error(
                f"{entry['unit']} (pid {entry['pid']}) started "
                f"{_epoch_iso(entry['started_epoch'])}, before checkout HEAD{sha_detail}{source_detail} "
                f"was updated {_epoch_iso(head_update_epoch)}; the process still runs "
                "pre-deploy code",
                hint="restart the workers in an idle window: orca_auto service restart",
            )
        for entry in staleness["undetermined"]:
            emit_error(
                "cannot judge worker code freshness"
                + (f" for {entry['unit']}" if entry["unit"] else "")
                + f": {entry['detail']}",
                hint="restart the workers in an idle window: orca_auto service restart",
            )
    return 0 if payload["ok"] and drift is None and staleness_ok else 1


def cmd_service_restart(args: argparse.Namespace, *, deps: ServiceCliDeps | None = None) -> int:
    deps = deps or ServiceCliDeps()
    which = deps.which or shutil.which
    run = deps.run or subprocess.run
    is_root = deps.is_root or _is_root
    restart_unit_for_user = deps.restart_unit_for_user or _restart_unit_for_user

    if not _systemctl_available(which=which):
        emit_error("systemctl is not available in this environment")
        return 1
    use_sudo = not is_root()
    if use_sudo and not _sudo_available(which=which):
        emit_error("sudo is required to restart system services; rerun as root")
        return 1

    target_user = _service_target_user(args, deps)
    try:
        unit = restart_unit_for_user(target_user, run=run)
        worker_units = _restartable_worker_units(target_user, run=run)
    except ValueError as exc:
        emit_error(exc)
        return 1

    for reset_unit in worker_units:
        print(f"Resetting service failure state for {reset_unit}")
        rc = _run_command(
            ("systemctl", "reset-failed", reset_unit),
            use_sudo=use_sudo,
            run=run,
        )
        if rc != 0:
            return rc

    print(f"Restarting {unit}")
    rc = _run_command(("systemctl", "restart", unit), use_sudo=use_sudo, run=run)
    if rc != 0:
        return rc

    # The target restart above is not enough to reload the workers. The opt-in
    # workflow worker is structurally out of reach: it belongs to no target. The
    # ORCA worker is a member, but `systemctl restart
    # orca_auto-runtime@<user>.target` still left its ExecMainStartTimestamp
    # untouched on the deploy host. Both import the checkout live and never
    # reload, so restarting the services is what carries a deploy to them --
    # which is what `service status` promises when it reports a stale worker.
    for worker_unit in worker_units:
        print(f"Restarting {worker_unit}")
        rc = _run_command(("systemctl", "restart", worker_unit), use_sudo=use_sudo, run=run)
        if rc != 0:
            return rc

    print("Restart requested successfully.")
    print("Check status with: orca_auto service status")
    return 0


__all__ = [
    "ServiceCliDeps",
    "ServiceUnitStatus",
    "cmd_service_restart",
    "cmd_service_status",
    "collect_service_status",
    "collect_worker_staleness",
]
