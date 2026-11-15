"""Acceptance-helper contracts without launching a worker, MPI or an engine."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import checkpoint_acceptance as harness


def descriptor(path: Path, payload: bytes) -> dict:
    path.write_bytes(payload)
    return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}


def test_missing_and_mismatched_donors_stop(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing"):
        harness.verified_file({"path": str(tmp_path / "absent"), "sha256": "0" * 64})
    donor = descriptor(tmp_path / "donor", b"authentic-placeholder")
    donor["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        harness.copy_verified(donor, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()


def test_verified_copy_changes_no_original_bytes_or_mode(tmp_path: Path) -> None:
    donor = descriptor(tmp_path / "original", b"authentic-placeholder")
    source = Path(donor["path"])
    source.chmod(0o400)
    before = harness.identity(source)
    harness.copy_verified(donor, tmp_path / "copy")
    assert harness.identity(source) == before
    assert (tmp_path / "copy").read_bytes() == source.read_bytes()


def test_source_change_between_verify_and_capture_is_rejected(tmp_path: Path, monkeypatch) -> None:
    donor = descriptor(tmp_path / "original", b"first")
    original = harness.verified_file

    def changed(item):
        path = original(item)
        path.write_bytes(b"other")
        return path

    monkeypatch.setattr(harness, "verified_file", changed)
    with pytest.raises(ValueError, match="changed before copy"):
        harness.copy_verified(donor, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()


_DIAGNOSTIC = b"""! HF STO-3G SP MORead
%moinp "probe.moinp.gbw"
%pal nprocs 4 end
%maxcore 2048
%scf AutoStart false MaxIter 1 end
* xyz 0 1
H 0 0 0
H 0 0 0.74
*
"""


@pytest.mark.parametrize(
    "change", ["none", "noiter", "nprocs", "maxiter", "guess", "reference", "implicit-autostart"]
)
def test_matrix_input_constraints(tmp_path: Path, change: str) -> None:
    payload = _DIAGNOSTIC
    replacements = {
        "noiter": (b"SP MORead", b"SP MORead NoIter"),
        "nprocs": (b"nprocs 4", b"nprocs 2"),
        "maxiter": (b"MaxIter 1", b"MaxIter 2"),
        "guess": (b"SP MORead", b"SP MORead PModel"),
        "reference": (b"probe.moinp.gbw", b"wrong.gbw"),
        "implicit-autostart": (b"AutoStart false", b"AutoStart true"),
    }
    if change != "none":
        payload = payload.replace(*replacements[change])
    item = {**descriptor(tmp_path / "probe.inp", payload), "dependencies": []}
    if change == "none":
        assert harness.validate_input(item, diagnostic=True) == Path(item["path"])
    else:
        with pytest.raises(ValueError):
            harness.validate_input(item, diagnostic=True)


def test_success_requires_moread_finished_guess_then_scf() -> None:
    read = b"Guess MOs are being read from file: probe.moinp.gbw\n"
    guess = b"Finished Guess after 0.1 sec\n"
    scf = b"Iteration    Energy (Eh)    Delta-E\n"
    assert harness.output_observation(read + guess + scf)["moread_consumed_then_scf"]
    for payload in (
        guess + read + scf,
        scf + guess + read,
        read + scf,
        guess + scf,
        b"| 1> " + read + b"# " + guess + scf,
    ):
        assert not harness.output_observation(payload)["moread_consumed_then_scf"]


def test_scoped_call_check_rejects_representative_unowned_mutation(monkeypatch) -> None:
    monkeypatch.setattr(
        harness.inspect,
        "getsource",
        lambda _: "reserve_slot(root, 4, owned=ours)\nlist_slots(root, owned=ours)",
    )
    assert harness.scoped_calls(None, {"reserve_slot", "list_slots"})
    monkeypatch.setattr(
        harness.inspect,
        "getsource",
        lambda _: "reserve_slot(root, 4)\nlist_slots(root, owned=ours)",
    )
    assert not harness.scoped_calls(None, {"reserve_slot", "list_slots"})


def test_foreign_slots_do_not_have_to_clear(tmp_path: Path, monkeypatch) -> None:
    from orca_auto.core import admission
    from orca_auto.core.utils import process

    owned = SimpleNamespace(work_dir=str(tmp_path / "runs" / "job"), queue_id="ours", owner_pid=101)
    foreign = SimpleNamespace(
        work_dir=str(tmp_path / "runs-other" / "job"), queue_id="foreign", owner_pid=202
    )
    monkeypatch.setattr(admission, "list_all_slots", lambda _: [foreign])
    assert harness.own_slots(tmp_path / "runs", "ours") == []
    monkeypatch.setattr(admission, "list_all_slots", lambda _: [owned, foreign])
    assert harness.own_slots(tmp_path / "runs", "ours") == [owned]
    assert not harness.cleanup_complete(tmp_path / "runs", "ours", [])
    monkeypatch.setattr(admission, "list_all_slots", lambda _: [foreign])
    monkeypatch.setattr(process, "process_identity_alive", lambda *args: False)
    monkeypatch.setattr(process, "process_group_exists", lambda _: True)
    observed = [
        {"owner_pid": 101, "process_start_ticks": 1, "owner_boot_id": "boot", "engine_pgid": 303}
    ]
    assert not harness.cleanup_complete(tmp_path / "runs", "ours", observed)
    monkeypatch.setattr(process, "process_group_exists", lambda _: False)
    assert harness.cleanup_complete(tmp_path / "runs", "ours", observed)


@pytest.mark.parametrize("use_scratch", [True, False])
def test_configuration_preserves_shared_limits_and_disables_notifications(
    tmp_path: Path, use_scratch: bool
) -> None:
    production = tmp_path / "production"
    (production / ".admission").mkdir(parents=True)
    config = descriptor(
        tmp_path / "production.yaml",
        b"""runs_root: /production
scheduler: {max_active_simulations: 4}
orca:
  runtime: {scratch_root: /dev/shm/shared, scratch_min_free_gb: 12}
messenger:
  provider: slack
  slack: {bot_token: do-not-copy, default_channel_id: channel}
""",
    )
    plan = {
        "experiment_root": str(tmp_path),
        "session": "science",
        "production_config": config,
        "source_revision": "a" * 40,
    }
    facts = {"operational_root": str(production), "use_scratch": use_scratch}
    session = harness.session_setup(plan, facts)
    raw = harness.yaml.safe_load((session / "config.yaml").read_text())
    assert raw["scheduler"] == {"max_active_simulations": 4}
    assert raw["orca"]["runtime"] == (
        {"scratch_root": "/dev/shm/shared", "scratch_min_free_gb": 12} if use_scratch else {}
    )
    assert harness.identity(Path(config["path"]))["sha256"] == config["sha256"]
    assert raw["messenger"] == {
        "provider": "discord",
        "discord": {"bot_token": "", "default_channel_id": ""},
    }
    assert (session / "runs" / ".admission").resolve() == production / ".admission"
    marker = harness.read_json(session / "session.json")
    assert marker["matrix_launches"] == marker["recovery_launches"] == 0
    assert "do-not-copy" not in (session / "config.yaml").read_text()
    (session / "STOP.json").write_text(json.dumps({"reason": "unresolved"}))
    with pytest.raises(ValueError, match="session stopped"):
        harness.session_setup(plan, facts)


def test_cancel_is_fenced_to_requested_experiment_row(tmp_path: Path, monkeypatch) -> None:
    from orca_auto.orca.queue import adapter

    row = SimpleNamespace(queue_id="only-this", status=SimpleNamespace(value="running"))
    calls = []
    monkeypatch.setattr(adapter, "get_entry_by_id", lambda root, queue_id: row)

    def cancel(root, queue_id, **kwargs):
        calls.append((root, queue_id, kwargs))
        return row

    monkeypatch.setattr(adapter, "cancel", cancel)
    harness._cancel(tmp_path, "only-this")
    assert calls == [(tmp_path, "only-this", {"expected_entry": row})]


def test_real_runtime_scoped_call_inspection() -> None:
    from orca_auto.orca import execution
    from orca_auto.orca.queue import worker

    assert harness.scoped_calls(execution, {"activate_reserved_slot"})
    assert harness.scoped_calls(
        worker,
        {
            "reserve_slot",
            "list_slots",
            "reconcile_stale_slots",
            "recover_orphaned_engine_slots",
            "update_slot_metadata",
        },
    )


@pytest.mark.parametrize("shutdown", ["none", "before", "during"])
def test_private_worker_sleep_preserves_poll_cadence_but_honors_shutdown(
    tmp_path, monkeypatch, shutdown
):
    from orca_auto.orca import config
    from orca_auto.orca.queue.worker import OrcaQueueWorker
    from tests.conftest import make_app_cfg
    from tests.process_helpers import preserved_signal_handlers

    cfg = make_app_cfg(tmp_path)
    control = {"session": str(tmp_path), "config": {"path": "private.yaml"}}
    monkeypatch.setattr(harness, "_control", lambda _: control)
    monkeypatch.setattr(config, "load_config", lambda _: cfg)
    clock = {"now": 0.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds
        if shutdown == "during" and clock["now"] >= 0.12:
            signal.raise_signal(signal.SIGTERM)

    def run_once(worker):
        assert worker.cfg is cfg
        assert worker.config_path == "private.yaml"
        assert worker.poll_interval_seconds == 5
        worker._install_signal_handlers()
        if shutdown == "before":
            signal.raise_signal(signal.SIGTERM)
        worker._sleep_fn(worker.poll_interval_seconds)
        assert worker._shutdown_requested == (shutdown != "none")
        return 23

    monkeypatch.setattr(harness.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(harness.time, "sleep", sleep)
    monkeypatch.setattr(OrcaQueueWorker, "run_once", run_once)
    with preserved_signal_handlers(signal.SIGTERM, signal.SIGINT):
        assert harness.private_process("_worker", tmp_path / "control.json") == 23
    assert clock["now"] == pytest.approx({"none": 5, "before": 0, "during": 0.15}[shutdown])
    assert all(0 < seconds <= 0.05 for seconds in sleeps)
    assert (tmp_path / "worker-pid.json").is_file()


def test_external_watchdog_cancels_only_private_row_and_retains_stop(
    tmp_path: Path, monkeypatch
) -> None:
    from orca_auto.core.utils import process

    session = tmp_path / "session"
    evidence = session / "evidence" / "case"
    evidence.mkdir(parents=True)
    control = {
        "session": str(session),
        "queue_id": "only-this",
        "deadline": -1,
        "supervisor_pid": 100,
        "supervisor_ticks": 1,
        "supervisor_boot": "boot",
    }
    harness.write_json(evidence / "worker-pid.json", {"pid": 200, "ticks": 2, "boot": "boot"})
    monkeypatch.setattr(harness, "_control", lambda _: control)
    calls = []
    monkeypatch.setattr(harness, "_cancel", lambda root, queue_id: calls.append((root, queue_id)))
    monkeypatch.setattr(harness, "own_slots", lambda *args: [])
    monkeypatch.setattr(harness, "cleanup_complete", lambda *args: True)
    monkeypatch.setattr(process, "process_identity_alive", lambda *args: False)
    assert harness.private_process("_watchdog", evidence / "control.json") == 2
    assert calls == [(session / "runs", "only-this")]
    assert harness.read_json(evidence / "watchdog-cleanup.json")["resolved"]
    assert harness.read_json(session / "STOP.json")["queue_id"] == "only-this"


def _session_tree(tmp_path: Path) -> Path:
    session = tmp_path / "session"
    session.mkdir()
    (session / "runs").mkdir()
    (session / "evidence").mkdir()
    (session / "config.yaml").write_text("runs_root: /tmp/runs\n")
    harness.write_json(
        session / "session.json",
        {
            "deadline": time.monotonic() + 10_000,
            "matrix_launches": 0,
            "recovery_launches": 0,
        },
    )
    return session


def test_supervisor_timeout_latches_stop_before_watchdog(tmp_path: Path, monkeypatch) -> None:
    session = _session_tree(tmp_path)
    row = SimpleNamespace(
        queue_id="row-1",
        metadata={"execution_snapshot": {"execution_dir": str(tmp_path / "generation")}},
    )
    cancel_calls: list[str] = []
    clock = {"value": 100.0}

    def monotonic() -> float:
        clock["value"] += 90.0
        return clock["value"]

    class FakeProc:
        returncode = 0

        def poll(self) -> int | None:
            return None if clock["value"] < 400.0 else 0

    class FakeWatchdog:
        def poll(self) -> int | None:
            return None

        def wait(self, *, timeout: float) -> None:
            return None

    monkeypatch.setattr(harness.time, "monotonic", monotonic)
    monkeypatch.setattr(harness, "LAUNCH_SECONDS", 120)
    monkeypatch.setattr(harness, "CLEANUP_SECONDS", 0)
    monkeypatch.setattr(harness, "_cancel", lambda root, queue_id: cancel_calls.append(queue_id))
    monkeypatch.setattr(harness, "own_slots", lambda *args: [])
    monkeypatch.setattr(harness, "cleanup_complete", lambda *args: True)
    monkeypatch.setattr(
        harness,
        "_live_files",
        lambda snapshot, scratch: (tmp_path / "missing.out", None),
    )
    monkeypatch.setattr(
        harness.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeWatchdog() if "_watchdog" in args[0] else FakeProc(),
    )

    from orca_auto.orca.queue import adapter

    monkeypatch.setattr(adapter, "get_entry_by_id", lambda root, queue_id: row)

    observed = harness.run_worker(session, "row-1", "matrix-cell", tmp_path)

    assert observed["supervisor_timeout_fired"]
    assert not observed["watchdog_fired"]
    assert cancel_calls == ["row-1"]
    assert harness.read_json(session / "STOP.json")["reason"] == "supervisor_timeout"
    assert (session / "evidence" / "matrix-cell" / "supervisor-timeout.json").is_file()


def test_matrix_stops_before_next_cell_after_supervisor_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    session = _session_tree(tmp_path)
    family = {
        "name": "water",
        "diagnostic": {"path": "/x", "sha256": "0" * 64, "dependencies": []},
        "positive_donor": {"path": "/y", "sha256": "1" * 64},
        "preguess_donor": {"path": "/z", "sha256": "2" * 64},
    }
    labels: list[str] = []
    row = SimpleNamespace(
        queue_id="row-1",
        metadata={
            "execution_snapshot": {
                "execution_dir": str(tmp_path / "generation"),
                "bound_selected_identity": {"sha256": "same"},
            }
        },
    )

    def submit(session, family_name, label, descriptor, donor=None):
        labels.append(label)
        return row

    monitoring = {
        "watchdog_fired": False,
        "supervisor_timeout_fired": True,
        "evidence_dir": str(session / "evidence" / "cell"),
        "scratch": {},
        "triggered": False,
    }

    monkeypatch.setattr(harness, "submit", submit)
    monkeypatch.setattr(harness, "mode_intervention", lambda snapshot, mode: {})
    monkeypatch.setattr(harness, "run_worker", lambda *args, **kwargs: monitoring)
    monkeypatch.setattr(
        harness,
        "result_receipt",
        lambda session, queue_id: {
            "row": {"status": "cancelled"},
            "completed_sp_evidence": False,
            "machine_valid": False,
        },
    )

    with pytest.raises(ValueError, match="timed out cell is inconclusive"):
        harness.matrix(session, family, tmp_path)

    assert labels == ["V400"]


def test_recovery_rejects_supervisor_timeout_on_interruption(tmp_path: Path, monkeypatch) -> None:
    session = _session_tree(tmp_path)
    family = {
        "name": "water",
        "recovery_default": {"path": "/a", "sha256": "0" * 64, "dependencies": []},
        "recovery_explicit": {"path": "/b", "sha256": "1" * 64, "dependencies": []},
        "explicit_guess_output": "MODEL POTENTIAL",
    }
    row = SimpleNamespace(queue_id="row-1")
    monkeypatch.setattr(harness, "submit", lambda *args, **kwargs: row)
    monkeypatch.setattr(harness, "result_receipt", lambda *args: {"row": {"status": "pending"}})
    monkeypatch.setattr(
        harness,
        "run_worker",
        lambda *args, **kwargs: {
            "watchdog_fired": False,
            "supervisor_timeout_fired": True,
            "triggered": False,
        },
    )

    with pytest.raises(ValueError, match="timed out cell is inconclusive"):
        harness.recovery(session, family, tmp_path)
    saved = harness.read_json(session / "evidence" / "water-pre-default-result.json")
    assert saved["first"]["supervisor_timeout_fired"]
    assert saved["stopped"]["row"]["status"] == "pending"


def test_recovery_rejects_supervisor_timeout_on_rebind(tmp_path: Path, monkeypatch) -> None:
    session = _session_tree(tmp_path)
    marker = harness.read_json(session / "session.json")
    harness.write_json(session / "session.json", {**marker, "use_scratch": False})
    generation = tmp_path / "generation"
    generation.mkdir()
    (generation / "probe.gbw").write_bytes(b"gbw")
    family = {
        "name": "water",
        "recovery_default": {"path": "/a", "sha256": "0" * 64, "dependencies": []},
        "recovery_explicit": {"path": "/b", "sha256": "1" * 64, "dependencies": []},
        "explicit_guess_output": "MODEL POTENTIAL",
    }
    row = SimpleNamespace(queue_id="row-1")
    identity = harness.identity
    calls = {"rebind": 0}

    def run_worker(session, queue_id, label, scratch, *, phase=""):
        if "rebind" in label:
            calls["rebind"] += 1
            return {
                "watchdog_fired": False,
                "supervisor_timeout_fired": True,
                "triggered": False,
            }
        return {"watchdog_fired": False, "supervisor_timeout_fired": False, "triggered": True}

    (generation / "probe.out").write_bytes(b"Finished Guess after 1 sec\n")
    stopped = {
        "row": {"status": "pending"},
        "snapshot": {
            "execution_dir": str(generation),
            "generation_name": "first",
        },
        "artifacts": {"probe.out": identity(generation / "probe.out")},
        "observation": {},
    }

    monkeypatch.setattr(harness, "submit", lambda *args, **kwargs: row)
    monkeypatch.setattr(harness, "run_worker", run_worker)
    monkeypatch.setattr(harness, "result_receipt", lambda session, queue_id: stopped)
    monkeypatch.setattr(
        "orca_auto.orca.recovery_orbitals.checkpoint_output_readiness",
        lambda payload: SimpleNamespace(eligible=False),
    )

    with pytest.raises(ValueError, match="timed out cell is inconclusive"):
        harness.recovery(session, family, tmp_path)

    assert calls["rebind"] == 1
    saved = harness.read_json(session / "evidence" / "water-pre-default-result.json")
    assert saved["second"]["supervisor_timeout_fired"]
    assert saved["final"] and saved["stopped"]


def test_preexisting_stop_refuses_submit_and_run_worker(tmp_path: Path, monkeypatch) -> None:
    session = _session_tree(tmp_path)
    harness.write_json(session / "STOP.json", {"reason": "prior"})

    from orca_auto.orca.queue import adapter

    monkeypatch.setattr(adapter, "list_queue", lambda root: [])

    with pytest.raises(ValueError, match="session stopped"):
        harness.submit(
            session,
            "water",
            "V400",
            {"path": str(tmp_path / "probe.inp"), "sha256": "0" * 64, "dependencies": []},
        )

    with pytest.raises(ValueError, match="session stopped"):
        harness.run_worker(session, "row-1", "blocked", tmp_path)


def test_candidate_output_requires_exact_private_name() -> None:
    private = "orca_auto_moinp_probe.moinp.gbw"
    payload = (
        f"Guess MOs are being read from file: {private}\n"
        "Finished Guess after 0.1 sec\nIteration Energy (Eh)\n"
    ).encode()
    assert not harness.output_observation(payload)["moread_consumed_then_scf"]
    assert harness.output_observation(payload, private)["moread_consumed_then_scf"]
    assert not harness.output_observation(payload.replace(private.encode(), b"other.gbw"), private)[
        "moread_consumed_then_scf"
    ]


@pytest.mark.parametrize("failure", ["watchdog_fired", "supervisor_timeout_fired"])
def test_production_stops_without_mode_intervention(tmp_path, monkeypatch, failure):
    session = _session_tree(tmp_path)
    harness.write_json(session / "session.json", {"use_scratch": True})
    source = descriptor(tmp_path / "source.gbw", b"original")
    bound = descriptor(tmp_path / "bound.inp", b"! SP\n")
    family = {
        "name": "family",
        "positive_donor": source,
        "preguess_donor": source,
        "diagnostic": bound,
    }
    snapshot = {"bound_selected_identity": bound, "materialized_inputs": {}}
    labels = []

    def submit(*args):
        labels.append(args[2])
        return SimpleNamespace(queue_id="id", metadata={"execution_snapshot": snapshot})

    def forbidden(*args):
        raise AssertionError("production must never chmod its bound checkpoint")

    observed = {"watchdog_fired": False, "supervisor_timeout_fired": False, failure: True}
    monkeypatch.setattr(harness, "submit", submit)
    monkeypatch.setattr(harness, "mode_intervention", forbidden)
    monkeypatch.setattr(harness, "run_worker", lambda *args: observed)
    monkeypatch.setattr(harness, "result_receipt", lambda *args: {"row": {"status": "failed"}})
    with pytest.raises(ValueError, match="timed out cell is inconclusive"):
        harness.production(session, family, tmp_path)
    assert labels == ["V-private"]


def test_machine_receipt_alone_does_not_prove_publication():
    receipt = {"machine_valid": True, "state": {"attempts": [{}]}}
    assert not harness.publication_valid(receipt, use_scratch=True)
    assert harness.publication_valid(receipt, use_scratch=False)
    receipt["state"]["attempts"][0]["scratch_provenance"] = {
        "used": True,
        "publication_status": "committed",
    }
    assert harness.publication_valid(receipt, use_scratch=True)
    assert not harness.publication_valid(receipt, use_scratch=False)


def test_prepared_small_system_mechanics_is_distinct_and_validated(tmp_path):
    from orca_auto.orca.recovery_orbitals import recovery_guess_policy
    from tools.prepare_checkpoint_mechanics import prepare

    path = prepare(tmp_path / "water")
    family = harness.read_json(path)["families"][0]
    assert family["acceptance_scope"] == "small_system_mechanics"
    for key, policy in [
        ("recovery_default", "automatic"),
        ("recovery_explicit", "explicit_fresh_guess"),
    ]:
        inp = harness.validate_input(family[key], diagnostic=False)
        assert recovery_guess_policy(inp.read_text().splitlines()) == policy
        assert "* xyz 0 1" in inp.read_text()
        assert "B3LYP D3BJ def2-SVP CPCM(THF) TightSCF DefGrid3 SP" in inp.read_text()
    assert (
        Path(family["reference_input"]["path"]).read_bytes()
        == Path(family["recovery_default"]["path"]).read_bytes()
    )
    with pytest.raises(FileExistsError):
        prepare(tmp_path / "water")


@pytest.mark.parametrize("use_scratch", [True, False])
@pytest.mark.parametrize("phase", ["active", "idle", "dead", "exited_during_read", "wrong_cwd"])
def test_copy_capture_requires_the_live_engine_in_the_observed_directory(
    tmp_path, monkeypatch, use_scratch, phase
):
    from orca_auto.core.utils import process

    generation = tmp_path / "generation"
    generation.mkdir()
    directory = tmp_path / "scratch" if use_scratch else generation
    directory.mkdir(exist_ok=True)
    source, target = "donor.gbw", "orca_auto_moinp_donor.gbw"
    inp = generation / "probe.inp"
    inp.write_text(f'%moinp "{target}"\n')
    (directory / source).write_bytes(b"immutable")
    (directory / source).chmod(0o400)
    (directory / target).write_bytes(b"private")
    (directory / target).chmod(0o600)
    snapshot = {"execution_dir": str(generation), "selected_inp": str(inp)}
    slot = {
        "engine_process_state": "idle" if phase == "idle" else "active",
        "engine_pid": 123,
        "engine_process_start_ticks": 456,
        "engine_process_boot_id": "boot",
    }
    alive = iter([phase != "dead", phase != "exited_during_read"])
    monkeypatch.setattr(process, "process_identity_alive", lambda *args: next(alive))
    resolve = Path.resolve

    def engine_cwd(path, *args, **kwargs):
        if path == Path("/proc/123/cwd"):
            return tmp_path if phase == "wrong_cwd" else directory
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", engine_cwd)
    captured = harness._capture_engine_copies(
        snapshot, directory, directory if use_scratch else None, [slot]
    )
    if phase != "active":
        assert captured == {}
    else:
        assert captured["directory"] == captured["engine_cwd"] == str(directory)
        assert captured["scratch_workspace"] == (str(directory) if use_scratch else None)
        assert captured["engine"] == slot
        assert captured["engine_alive_before"] and captured["engine_alive_after"]
        assert captured["copies"][target]["immutable"]["mode"] == "0o400"


@pytest.fixture
def recovery_probe(tmp_path, monkeypatch):
    session = _session_tree(tmp_path)
    marker = harness.read_json(session / "session.json")
    harness.write_json(session / "session.json", {**marker, "use_scratch": True})
    family = {
        "name": "water",
        "recovery_default": {},
        "recovery_explicit": {},
        "explicit_guess_output": "MODEL POTENTIAL",
    }
    probe = {"launches": [], "cancelled": [], "use_scratch": True, "defect": "", "label": ""}
    current = {}
    row = SimpleNamespace(queue_id="row-1")

    def result_path():
        return session / "evidence" / f"water-{probe['label']}-result.json"

    def submit(session, family, label, descriptor):
        probe["label"] = label
        return row

    def run_worker(session, queue_id, label, scratch, *, phase=""):
        if not phase:
            saved = harness.read_json(result_path())
            assert saved["first"]["triggered"]
            assert saved["stopped"]["snapshot"] == current["snapshot"]
            assert saved["stopped"]["artifacts"] == current["artifacts"]
            assert saved["stopped"]["publication_valid"]
        probe["launches"].append(label)
        generation = tmp_path / label
        generation.mkdir()
        seed = not phase and probe["label"] == "post-default"
        explicit = probe["label"] == "post-explicit"
        bound = '! SP\n%moinp "orca_auto_moinp_probe.moinp.gbw"\n' if seed else "! SP\n"
        if explicit:
            bound += "%scf Guess PModel end\n"
        inp = generation / "probe.inp"
        inp.write_text(bound)
        output = generation / "probe.out"
        finished_guess = phase != "pre" or probe["defect"] == "phase_miss"
        output.write_text(
            "* O   R   C   A *\nORCA STARTUP CALCULATIONS\nORCA GUESS\n"
            + ("Finished Guess after 0.1 sec\n" if finished_guess else "")
        )
        if finished_guess:
            (generation / "probe.gbw").write_bytes(b"checkpoint")
        publication = {
            "used": True,
            "publication_status": "committed",
            "published_files": ["probe.out"] + (["probe.gbw"] if phase != "pre" else []),
        }
        record = {
            "attempt_index": 1,
            "inp_path": str(inp),
            "outcome": "worker_shutdown",
            "stop_cause": "worker_shutdown",
            "publication": publication,
        }
        if probe["defect"] == probe["label"]:
            records = []
        else:
            records = [record] if probe["use_scratch"] else []
        current.clear()
        current.update(
            row={"status": "pending" if phase else "completed"},
            snapshot={
                "execution_dir": str(generation),
                "generation_name": label,
                "selected_inp": str(inp),
                "recovery": {"checkpoint_role": "seed" if seed else ""},
            },
            artifacts={p.name: harness.identity(p) for p in generation.iterdir()},
            observation={"moread_consumed_then_scf": seed, "guess_labels": ["MODEL POTENTIAL"]},
            machine_valid=not bool(phase),
            completed_sp_evidence=not bool(phase),
            state={
                "scratch_publications": records if phase else [],
                "attempts": []
                if phase
                else [{"scratch_provenance": publication if probe["use_scratch"] else {}}],
            },
        )
        return {
            "triggered": bool(phase),
            "watchdog_fired": False,
            "supervisor_timeout_fired": False,
        }

    def cancel(root, queue_id):
        saved = harness.read_json(result_path())
        assert saved["first"] and saved["stopped"]["artifacts"]
        probe["cancelled"].append(probe["label"])

    def copy_receipt(*args, **kwargs):
        if probe["defect"] == "copy_assertion":
            raise ValueError("missing live engine-workspace copy")
        return {"copy": {}}

    monkeypatch.setattr(harness, "submit", submit)
    monkeypatch.setattr(harness, "run_worker", run_worker)
    monkeypatch.setattr(harness, "result_receipt", lambda *args: dict(current))
    monkeypatch.setattr(harness, "_cancel", cancel)
    monkeypatch.setattr(harness, "production_copy_receipt", copy_receipt)
    return session, family, probe


@pytest.mark.parametrize("use_scratch", [True, False])
def test_startup_trigger_cannot_pass_when_frozen_output_completed_guess(
    recovery_probe, tmp_path, use_scratch
):
    session, family, probe = recovery_probe
    probe.update(use_scratch=use_scratch, defect="phase_miss")
    marker = harness.read_json(session / "session.json")
    harness.write_json(session / "session.json", {**marker, "use_scratch": use_scratch})
    with pytest.raises(ValueError, match="interruption missed its intended phase"):
        harness.recovery(session, family, tmp_path)
    assert probe["launches"] == ["water-pre-default"]
    assert probe["cancelled"] == ["pre-default"]
    evidence = harness.read_json(session / "evidence" / "water-pre-default-result.json")
    assert evidence["first"]["triggered"]
    assert evidence["verdict"] == "inconclusive_phase_miss"
    assert evidence["stopped"]["artifacts"]["probe.out"]["sha256"]
    assert "second" not in evidence and "scientific_acceptance_pass" not in evidence


@pytest.mark.parametrize(
    "label,index", [("pre-default", 0), ("post-default", 1), ("post-explicit", 2)]
)
def test_missing_interrupted_publication_stops_before_rebind(
    recovery_probe, tmp_path, label, index
):
    session, family, probe = recovery_probe
    probe["defect"] = label
    with pytest.raises(ValueError, match="interrupted publication"):
        harness.recovery(session, family, tmp_path)
    assert len(probe["launches"]) == 2 * index + 1
    assert probe["cancelled"] == [label]
    evidence = harness.read_json(session / "evidence" / f"water-{label}-result.json")
    assert evidence["stopped"]["state"]["scratch_publications"] == []
    assert not evidence["stopped"]["publication_valid"]
    assert "second" not in evidence


def test_second_launch_evidence_survives_copy_boundary_assertion(recovery_probe, tmp_path):
    session, family, probe = recovery_probe
    probe["defect"] = "copy_assertion"
    with pytest.raises(ValueError, match="missing live engine-workspace"):
        harness.recovery(session, family, tmp_path)
    assert len(probe["launches"]) == 4
    evidence = harness.read_json(session / "evidence" / "water-post-default-result.json")
    assert all(evidence[key] for key in ("first", "stopped", "second", "final"))
    assert evidence["stopped"]["artifacts"]["probe.gbw"]["sha256"]
    assert "scientific_acceptance_pass" not in evidence


@pytest.mark.parametrize("use_scratch", [True, False])
def test_interrupted_publication_does_not_require_terminal_machine(
    recovery_probe, tmp_path, use_scratch
):
    session, family, probe = recovery_probe
    probe["use_scratch"] = use_scratch
    marker = harness.read_json(session / "session.json")
    harness.write_json(session / "session.json", {**marker, "use_scratch": use_scratch})
    harness.recovery(session, family, tmp_path)
    assert len(probe["launches"]) == 6
    for label in ("pre-default", "post-default", "post-explicit"):
        evidence = harness.read_json(session / "evidence" / f"water-{label}-result.json")
        assert not evidence["stopped"]["machine_valid"]
        assert evidence["stopped"]["publication_valid"]
        assert evidence["scientific_acceptance_pass"]


@pytest.mark.parametrize(
    "defect", ["uncommitted", "wrong_input", "wrong_outcome", "no_output", "no_gbw"]
)
def test_interrupted_publication_record_must_match_the_stopped_generation(
    recovery_probe, tmp_path, defect
):
    session, family, _ = recovery_probe
    harness.recovery(session, family, tmp_path)
    receipt = harness.read_json(session / "evidence" / "water-post-default-result.json")["stopped"]
    record = receipt["state"]["scratch_publications"][0]
    if defect == "uncommitted":
        record["publication"]["publication_status"] = "prepared"
    elif defect == "wrong_input":
        record["inp_path"] = "/unrelated/probe.inp"
    elif defect == "wrong_outcome":
        record["outcome"] = "exception"
    else:
        record["publication"]["published_files"].remove(
            "probe.out" if defect == "no_output" else "probe.gbw"
        )
    assert not harness.interrupted_publication_valid(receipt, use_scratch=True)


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5])
def test_water_command_block_stops_in_a_fresh_shell(tmp_path, fail_at):
    document = Path("tools/checkpoint_acceptance.md").read_text()
    section = document.split("## Separate bounded small-system recovery mechanics", 1)[1]
    block = section.split("```bash\n", 1)[1].split("```", 1)[0]
    stub = tmp_path / "stub"
    stub.write_text(
        '#!/bin/bash\nn=0\n[[ ! -f "$CALLS" ]] || n=$(cat "$CALLS")\n'
        'n=$((n+1))\necho "$n" > "$CALLS"\n'
        '[[ "$n" != "$FAIL_AT" ]] || exit 2\nexit 0\n'
    )
    stub.chmod(0o700)
    (tmp_path / "python3").symlink_to(stub)
    (tmp_path / "sha256sum").symlink_to(stub)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/python").symlink_to(stub)
    (tmp_path / "test-water").mkdir()
    calls = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", block],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "SCIENCE": str(tmp_path),
            "ACCEPTANCE_TAG": "test",
            "CANDIDATE_PREFIX": str(tmp_path),
            "HARNESS": "/unused",
            "CALLS": str(calls),
            "FAIL_AT": str(fail_at),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, result.stderr
    assert calls.read_text().strip() == str(fail_at)
