"""Golden durable files and cross-process write order of whole worker scenarios.

Each scenario drives only public entry points (``cli.main`` and
``OrcaQueueWorker.run_once`` with real worker children running a fake ORCA)
and compares every public on-disk file, the recorded notifications and the
effect log (``effect_log.py``) with ``golden/<scenario>/``. A parent that dies
is simulated by raising ``_ParentKilled`` from the worker method at the point
of death, so the in-memory worker is abandoned without cleanup.
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.messaging import Message
from orca_auto.orca.queue.worker import OrcaQueueWorker
from orca_auto.orca.scratch_config import ScratchConfig
from tests.conftest import enqueue_entry, make_queue_entry
from tests.contracts import effect_log
from tests.contracts.conftest import Harness
from tests.contracts.normalize import Normalizer, assert_golden, key_tree, read_json


class _ParentKilled(BaseException):
    """The worker parent died at this point; nothing after it runs."""


def _kill_parent(*_args: Any, **_kwargs: Any) -> None:
    raise _ParentKilled


def _generation_dirs(job_dir: Path, n: Normalizer) -> list[Path]:
    """Generation directories in the order the goldens first named them."""
    candidates = [
        path
        for path in job_dir.iterdir()
        if path.is_dir() and n.text(path.name).startswith("<generation:")
    ]
    return sorted(candidates, key=lambda path: int(n.text(path.name).split(":")[1].rstrip(">")))


def _json_or_none(path: Path) -> Any:
    return read_json(path) if path.exists() else None


def assert_durable_goldens(scenario: str, h: Harness, *jobs: Path) -> None:
    """Compare every public durable file of the scenario with its golden."""
    n = h.n

    def golden(name: str, value: Any) -> None:
        assert_golden(f"{scenario}/{name}", value)

    golden("queue.json", n(_json_or_none(h.runs / "queue.json")))
    golden("admission_slots.json", n(_json_or_none(h.tmp / "admission" / "admission_slots.json")))
    golden("job_locations.json", n(_json_or_none(h.runs / "job_locations.json")))
    intents = h.runs / ".orca_auto_snapshot_intents"
    golden(
        "snapshot_intents.json",
        n([read_json(path) for path in sorted(intents.glob("*.json"))] if intents.is_dir() else []),
    )
    for job_dir in jobs:
        prefix = job_dir.name
        golden(f"{prefix}.root_job_state.json", n(_json_or_none(job_dir / "job_state.json")))
        for index, generation in enumerate(_generation_dirs(job_dir, n), start=1):
            label = f"{prefix}.generation{index}"
            golden(f"{label}.job_state.json", n(_json_or_none(generation / "job_state.json")))
            golden(
                f"{label}.execution_provenance.json",
                n(_json_or_none(generation / "execution_provenance.json")),
            )
            machine = _json_or_none(generation / "machine.json")
            golden(f"{label}.machine_keys.json", None if machine is None else key_tree(machine))
    messages = [message for message in h.channel.sends if isinstance(message, Message)]
    assert len(messages) == len(h.channel.sends)
    golden("notifications.json", n([dataclasses.asdict(message) for message in messages]))
    golden("effects.json", effect_log.read(h.log))


def _running_goldens(scenario: str, h: Harness, job: Path) -> None:
    """Durable files while the calculation is running."""
    n = h.n
    assert_golden(f"{scenario}/running.queue.json", n(read_json(h.runs / "queue.json")))
    assert_golden(
        f"{scenario}/running.admission_slots.json",
        n(read_json(h.tmp / "admission" / "admission_slots.json")),
    )
    assert_golden(f"{scenario}/running.root_job_state.json", n(read_json(job / "job_state.json")))


def test_opt_freq_completed(harness: Harness) -> None:
    job = harness.job("opt_freq")
    assert harness.cli("run-dir", str(job))[0] == 0
    assert harness.run_worker() == 0
    assert_durable_goldens("01_opt_freq_completed", harness, job)


def test_error_termination_nonzero_exit(harness: Harness) -> None:
    harness.mode("error")
    job = harness.job("error_exit")
    assert harness.cli("run-dir", str(job))[0] == 0
    assert harness.run_worker() == 0
    assert_durable_goldens("02_error_termination", harness, job)


def test_cancel_while_pending(harness: Harness) -> None:
    job = harness.job("cancel_pending")
    assert harness.cli("run-dir", str(job))[0] == 0
    [row] = harness.rows()
    rc, out, _err = harness.cli("queue", "cancel", row["queue_id"], "--json")
    assert rc == 0
    assert_golden("03_cancel_pending/cancel.stdout.json", harness.n(json.loads(out)))
    assert harness.run_worker() == 0
    assert_durable_goldens("03_cancel_pending", harness, job)


def test_cancel_while_running(harness: Harness) -> None:
    harness.mode("hang")
    job = harness.job("cancel_running")
    assert harness.cli("run-dir", str(job))[0] == 0
    [row] = harness.rows()

    def cancel() -> None:
        _running_goldens("04_cancel_running", harness, job)
        rc, out, _err = harness.cli("queue", "cancel", row["queue_id"], "--json")
        assert rc == 0
        assert_golden("04_cancel_running/cancel.stdout.json", harness.n(json.loads(out)))

    assert harness.run_worker(harness.when_started(cancel)) == 0
    assert_durable_goldens("04_cancel_running", harness, job)


def test_sigterm_shutdown_requeue_and_restart(harness: Harness) -> None:
    harness.mode("hang")
    job = harness.job("sigterm")
    assert harness.cli("run-dir", str(job))[0] == 0

    def sigterm() -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    assert harness.run_worker(harness.when_started(sigterm)) == 0
    assert_golden("05_sigterm_restart/requeued.queue.json", harness.n(harness.rows()))
    harness.mode("complete")
    assert harness.run_worker() == 0
    assert_durable_goldens("05_sigterm_restart", harness, job)


def test_ram_scratch_capacity_deferral(harness: Harness, shm_scratch_root: Path) -> None:
    harness.paths[shm_scratch_root] = "<scratch>"
    # A reserve no host can meet refuses the launch for capacity.
    harness.configure(scratch=ScratchConfig(root=str(shm_scratch_root), min_free_gb=10**6))
    job = harness.job("scratch_deferral")
    assert harness.cli("run-dir", str(job))[0] == 0
    assert harness.run_worker() == 0
    assert_durable_goldens("06_ram_scratch_deferral", harness, job)


def test_crash_recovery_rebind(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.mode("crash")
    job = harness.job("crash_rebind")
    assert harness.cli("run-dir", str(job))[0] == 0
    # The host dies: the child is SIGKILLed mid-run and the parent never finalizes.
    with monkeypatch.context() as crashed:
        crashed.setattr(OrcaQueueWorker, "_finalize_completed_job", _kill_parent)
        with pytest.raises(_ParentKilled):
            harness.run_worker()
    assert_golden("07_crash_rebind/crashed.queue.json", harness.n(harness.rows()))
    harness.mode("complete")
    assert harness.run_worker() == 0
    assert_durable_goldens("07_crash_rebind", harness, job)


def test_parent_killed_after_terminal_mark_then_replayed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = harness.job("killed_parent")
    assert harness.cli("run-dir", str(job))[0] == 0
    with monkeypatch.context() as killed:
        killed.setattr(OrcaQueueWorker, "_finish_terminal_job", _kill_parent)
        with pytest.raises(_ParentKilled):
            harness.run_worker()
    assert_golden("08_parent_killed_replay/marked.queue.json", harness.n(harness.rows()))
    assert harness.run_worker() == 0
    assert_durable_goldens("08_parent_killed_replay", harness, job)


def test_prelaunch_rejection(harness: Harness) -> None:
    job = harness.job("prelaunch")
    assert harness.cli("run-dir", str(job))[0] == 0
    [row] = harness.rows()
    bound_input = Path(row["metadata"]["selected_inp"])
    bound_input.chmod(0o600)
    bound_input.write_text(bound_input.read_text(encoding="utf-8") + "# edited\n", "utf-8")
    assert harness.run_worker() == 0
    assert_durable_goldens("09_prelaunch_rejection", harness, job)


def test_forced_resubmission_into_same_directory(harness: Harness) -> None:
    job = harness.job("resubmit")
    assert harness.cli("run-dir", str(job))[0] == 0
    assert harness.run_worker() == 0
    assert harness.cli("run-dir", str(job), "--force")[0] == 0
    assert harness.run_worker() == 0
    assert_durable_goldens("10_forced_resubmission", harness, job)


def test_retired_workflow_refusal(harness: Harness) -> None:
    flow = harness.runs / "legacy_flow"
    job = harness.job("legacy_flow/step1")
    (flow / "workflow.json").write_text("{}", encoding="utf-8")
    rc, out, err = harness.cli("run-dir", str(job))
    assert_golden(
        "11_retired_workflow/run_dir.txt",
        harness.n.text(f"exit={rc}\n--- stdout\n{out}--- stderr\n{err}"),
    )
    enqueue_entry(
        harness.runs,
        make_queue_entry(
            queue_id="legacy-row",
            task_id="legacy-task",
            reaction_dir=harness.job("legacy_owned"),
            metadata={"workflow_id": "legacy-flow"},
        ),
    )
    assert harness.run_worker() == 0
    assert_durable_goldens("11_retired_workflow", harness, job)
