"""Worker freshness policy: pick the deployment model from process evidence.

Each active worker is identified by the import source it published at exec
time. A source tracked by a Git checkout is judged as an editable install
(:mod:`cli_systemd_freshness_checkout`); any other source is judged as a
prepared runtime or an unmanaged wheel (:mod:`cli_systemd_freshness_runtime`).
Both verdicts are then compared with the pin the installed unit declares.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from orca_auto import cli_systemd_units
from orca_auto.cli_systemd_evidence import (
    WorkerVerdict,
    process_identity_race_detail,
    read_process_file,
    unit_main_pid,
    worker_process_import_evidence,
)
from orca_auto.cli_systemd_freshness_checkout import (
    judge_checkout_worker,
    tracked_checkout_for_import_source,
)
from orca_auto.cli_systemd_freshness_runtime import (
    judge_installed_runtime,
    judge_runtime_worker,
)

_WORKER_PROCESS_LABELS = frozenset({"worker"})


def _common_evidence_value(rows: Sequence[dict[str, Any]], key: str) -> Any:
    values = {row[key] for row in rows if row.get(key) is not None}
    return values.pop() if len(values) == 1 else None


def _worker_staleness_payload(
    *,
    workers: list[dict[str, Any]],
    stale: list[dict[str, Any]],
    undetermined: list[dict[str, Any]],
    uncompared: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        # Keep the original aggregate field for JSON consumers. It is now
        # informational only; the verdict uses each checkout's HEAD update time.
        "head_commit_epoch": _common_evidence_value(workers, "head_commit_epoch"),
        "source_root": _common_evidence_value(workers, "source_root"),
        "head_sha": _common_evidence_value(workers, "head_sha"),
        "head_update_epoch": _common_evidence_value(workers, "head_update_epoch"),
        "workers": workers,
        "stale": stale,
        "undetermined": undetermined,
        "uncompared": uncompared,
    }


def _judge_worker(
    status: cli_systemd_units.ServiceUnitStatus,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
    read_process_file: Callable[[str], bytes],
) -> WorkerVerdict:
    """Judge one active worker: identify its source, then apply that model."""
    base_row: dict[str, Any] = {"label": status.label, "unit": status.unit}
    pid = unit_main_pid(status.unit, run=run)
    if pid <= 0:
        return WorkerVerdict("undetermined", {**base_row, "detail": "no readable main PID"})
    try:
        evidence = worker_process_import_evidence(pid, read_process_file=read_process_file)
        checkout_root = tracked_checkout_for_import_source(evidence.import_source, run=run)
    except (OSError, ValueError, RuntimeError) as exc:
        return WorkerVerdict(
            "undetermined",
            {**base_row, "pid": pid, "detail": f"cannot identify worker checkout: {exc}"},
        )
    detail = process_identity_race_detail(
        status.unit,
        pid=pid,
        process_start_ticks=evidence.process_start_ticks,
        run=run,
        read_process_file=read_process_file,
    )
    if detail:
        return WorkerVerdict(
            "undetermined",
            {**base_row, "detail": detail, "import_source": str(evidence.import_source)},
        )
    if checkout_root is None:
        verdict = judge_runtime_worker(
            label=status.label,
            unit=status.unit,
            pid=pid,
            evidence=evidence,
            run=run,
            read_process_file=read_process_file,
        )
    else:
        verdict = judge_checkout_worker(
            label=status.label,
            unit=status.unit,
            pid=pid,
            evidence=evidence,
            checkout_root=checkout_root,
            run=run,
            read_process_file=read_process_file,
        )
    return judge_installed_runtime(verdict, run=run, read_process_file=read_process_file)


def collect_worker_staleness(
    statuses: Sequence[cli_systemd_units.ServiceUnitStatus],
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    read_process_file: Callable[[str], bytes] = read_process_file,
) -> dict[str, Any] | None:
    """Compare each active worker process against the code it should be running.

    The module CLI re-execs once with the resolved source file it actually
    imported in its process environment. That evidence, not the process cwd,
    selects the deployment model: an editable checkout is compared by its HEAD
    reflog against the unit start, a prepared runtime by its build id against
    the bundle and the installed unit's pin. PID plus kernel process-start
    ticks are rechecked around every observation so a restart or PID reuse
    becomes undetermined rather than false-fresh. Unmanaged installed wheels
    have no comparison; an all-unmanaged-wheel set (or no active worker)
    returns ``None``.
    """
    active_workers = tuple(
        status
        for status in statuses
        if status.label in _WORKER_PROCESS_LABELS and status.active == "active"
    )
    if not active_workers:
        return None

    workers: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    undetermined: list[dict[str, Any]] = []
    uncompared: list[dict[str, Any]] = []
    for status in active_workers:
        verdict = _judge_worker(status, run=run, read_process_file=read_process_file)
        if verdict.kind == "undetermined":
            undetermined.append(verdict.row)
        elif verdict.kind == "uncompared":
            uncompared.append(verdict.row)
        else:
            workers.append(verdict.row)
            if verdict.stale:
                stale.append(dict(verdict.row))
    if uncompared and not workers and not undetermined:
        return None
    return _worker_staleness_payload(
        workers=workers,
        stale=stale,
        undetermined=undetermined,
        uncompared=uncompared,
    )


__all__ = ["collect_worker_staleness"]
