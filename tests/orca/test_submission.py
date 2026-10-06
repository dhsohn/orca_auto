"""Direct queue submission error handling."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Self

import pytest

import orca_auto.orca.submission as submission_mod
from orca_auto.core.admission import AdmissionStore, admission_dir
from orca_auto.core.config import DiscordConfig, MessengerConfig
from orca_auto.core.messaging import discord_bot as discord_bot_mod
from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    queue_entry_is_claimable,
)
from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca import submission as run_inp
from orca_auto.orca.config import CommonResourceConfig, load_config
from orca_auto.orca.input_artifacts import OrcaSelectedInputArtifacts
from orca_auto.orca.notifications import notify_queue_enqueued_event
from orca_auto.orca.queue import adapter as queue_adapter
from orca_auto.orca.queue import enqueue_publication, publication_repair
from orca_auto.orca.queue import entries as queue_entries
from orca_auto.orca.queue import notifications as queue_notifications
from orca_auto.orca.run_dir_guard import use_run_dir_publication_guard
from tests.conftest import claim_next_entry, make_app_cfg, write_config_file, write_fake_orca


def test_submit_without_selectable_inp_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reaction dir without any .inp used to leak the ValueError from
    # resource-request resolution as a CLI traceback.
    target = SimpleNamespace(
        cfg=None,
        allowed_root=tmp_path,
        reaction_dir=tmp_path / "job",
        selected_inp=tmp_path / "job" / "job.inp",
    )

    def raise_value_error(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("No .inp file selected for ORCA queue submission.")

    monkeypatch.setattr(
        submission_mod, "resolve_submission_target", lambda *_args, **_kwargs: target
    )
    monkeypatch.setattr(submission_mod, "find_submission_conflict", lambda *_args: None)
    monkeypatch.setattr(submission_mod, "create_queued_submission", raise_value_error)

    result = submission_mod.submit_reaction_dir_to_queue(
        SimpleNamespace(), cfg=make_app_cfg(tmp_path)
    )

    assert result.status == "failed"
    assert result.reason == "invalid_submission_input"
    assert "No .inp file selected" in result.stderr


def _real_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    messenger: MessengerConfig | None = None,
) -> tuple[Path, Any]:
    """A real ``orca_auto.yaml`` under ``tmp_path`` that ``load_config`` reads for every call."""

    config = write_config_file(
        tmp_path / "orca_auto.yaml",
        make_app_cfg(
            tmp_path,
            orca_executable=write_fake_orca(tmp_path / "fake_orca", "#!/bin/sh\n"),
            resources=CommonResourceConfig(max_cores_per_task=2, max_memory_gb_per_task=4),
            messenger=messenger,
        ),
    )
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    (reaction_dir / "rxn.inp").write_text(
        "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        queue_notifications, "notify_queue_enqueued_event", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(run_inp, "read_worker_pid_file", lambda _root: None)
    args = SimpleNamespace(
        config=str(config),
        path=str(reaction_dir),
        force=False,
        priority=7,
    )
    return reaction_dir, args


def test_queue_metadata_assembles_supplied_values_without_creating_files(tmp_path: Path) -> None:
    snapshot = {"selected_inp": str(tmp_path / "generation" / "sample.inp")}
    requested = {"max_cores": 2, "max_memory_gb": 4}
    metadata = run_inp.build_queue_metadata(
        reaction_dir=tmp_path,
        artifacts=OrcaSelectedInputArtifacts(
            selected_inp=str(tmp_path / "sample.inp"),
            selected_input_xyz=str(tmp_path / "start.xyz"),
        ),
        job_type="opt",
        molecule_key="sample",
        resource_request=requested,
        execution_snapshot=snapshot,
    )
    assert metadata == {
        "submitted_via": "run_inp",
        "orca_queued_notification_pending": True,
        "job_type": "opt",
        "molecule_key": "sample",
        "resource_request": {"max_cores": 2, "max_memory_gb": 4},
        "resource_actual": {"max_cores": 2, "max_memory_gb": 4},
        "source_selected_inp": str(tmp_path / "sample.inp"),
        "selected_inp": str(tmp_path / "generation" / "sample.inp"),
        "selected_input_path": str(tmp_path / "start.xyz"),
        "selected_input_xyz": str(tmp_path / "start.xyz"),
        "execution_snapshot": snapshot,
        "reaction_dir": str(tmp_path.resolve()),
    }
    assert list(tmp_path.iterdir()) == []
    metadata["resource_actual"]["max_cores"] = 99
    assert metadata["resource_request"] == requested == {"max_cores": 2, "max_memory_gb": 4}


def test_queue_metadata_records_a_given_detail_kind_beside_the_coarse_type(
    tmp_path: Path,
) -> None:
    snapshot = {"selected_inp": str(tmp_path / "generation" / "sp.inp")}
    metadata = run_inp.build_queue_metadata(
        reaction_dir=tmp_path,
        artifacts=OrcaSelectedInputArtifacts(
            selected_inp=str(tmp_path / "sp.inp"), selected_input_xyz=""
        ),
        job_type="other",
        detail_kind="sp",
        molecule_key="sample",
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        execution_snapshot=snapshot,
    )

    assert metadata == {
        "submitted_via": "run_inp",
        "orca_queued_notification_pending": True,
        "job_type": "other",
        "detail_kind": "sp",
        "molecule_key": "sample",
        "resource_request": {"max_cores": 1, "max_memory_gb": 1},
        "resource_actual": {"max_cores": 1, "max_memory_gb": 1},
        "source_selected_inp": str(tmp_path / "sp.inp"),
        "selected_inp": str(tmp_path / "generation" / "sp.inp"),
        "selected_input_path": str(tmp_path / "sp.inp"),
        "selected_input_xyz": "",
        "execution_snapshot": snapshot,
        "reaction_dir": str(tmp_path.resolve()),
    }
    assert list(metadata)[2:4] == ["job_type", "detail_kind"]
    assert list(tmp_path.iterdir()) == []


_H2 = "* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"


@pytest.mark.parametrize(
    ("job_dir_name", "inp_name", "route", "job_type", "detail_kind", "label"),
    [
        # The confirmed defect: a method-only SP under an IRC/TS-named job.
        (
            "MeOPh_OH_TSD_IRC_F_sp_continuous_01",
            "sp.inp",
            "! wB97X-D3BJ def2-TZVP TightSCF",
            "other",
            "sp",
            "SP",
        ),
        (
            "TS_IRC_named",
            "calc.inp",
            "! B3LYP def2-SVP\n%maxcore 1000\n! TightSCF",
            "other",
            "sp",
            "SP",
        ),
        # The route outranks a misleading file name.
        ("sp_named", "sp.inp", "! B3LYP def2-SVP IRC", "other", "irc", "IRC"),
        ("irc_named", "irc.inp", "! B3LYP def2-SVP IRC", "other", "irc", "IRC"),
        # Coarse types still get a presentation detail_kind from the same read.
        ("IRC_named_opt", "irc.inp", "! B3LYP def2-SVP Opt", "opt", "opt", "Opt"),
        ("ts_irc", "ts.inp", "! B3LYP def2-SVP OptTS Freq IRC", "ts", "ts+freq+irc", "TS+Freq+IRC"),
        ("freq_named", "freq.inp", "! B3LYP def2-SVP Freq", "freq", "freq", "Freq"),
        # Frequencies requested by %freq never read as a single point
        # (ORCA 6.1 manual: AnFreq/NumFreq default false, true requests them).
        (
            "IRC_sp_anfreq",
            "sp.inp",
            "! HF STO-3G\n%freq AnFreq true end",
            "other",
            "unknown",
            "Unknown",
        ),
        (
            "IRC_sp_numfreq",
            "sp.inp",
            "! HF STO-3G\n%freq\n  NumFreq true\nend",
            "other",
            "unknown",
            "Unknown",
        ),
        (
            "IRC_sp_anfreq_off",
            "sp.inp",
            "! HF STO-3G\n%freq AnFreq false end",
            "other",
            "sp",
            "SP",
        ),
        (
            "IRC_sp_numfreq_off",
            "sp.inp",
            "! HF STO-3G\n%freq NumFreq false end",
            "other",
            "sp",
            "SP",
        ),
        # A %geom TS search and the EnergyGrad run type are no single point.
        (
            "IRC_sp_ts_search",
            "sp.inp",
            "! HF STO-3G\n%geom TS_search EF end",
            "other",
            "unknown",
            "Unknown",
        ),
        (
            "IRC_sp_energygrad",
            "sp.inp",
            "! HF STO-3G EnergyGrad",
            "other",
            "unknown",
            "Unknown",
        ),
        # A %tddft block alone configures the single point's excited states.
        (
            "ESD_named_tddft",
            "sp.inp",
            "! B3LYP DEF2-SVP TIGHTSCF\n%TDDFT NROOTS 5 IROOT 1 END",
            "other",
            "sp",
            "SP",
        ),
    ],
    ids=[
        "sp-under-irc-ts-job",
        "sp-several-routes",
        "irc-in-sp-inp",
        "irc",
        "opt",
        "optts",
        "freq",
        "anfreq-block",
        "numfreq-block",
        "anfreq-false",
        "numfreq-false",
        "geom-ts-search",
        "energygrad",
        "tddft-only",
    ],
)
def test_submission_records_route_detail_for_the_queue_table_without_changing_job_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_dir_name: str,
    inp_name: str,
    route: str,
    job_type: str,
    detail_kind: str | None,
    label: str,
) -> None:
    from orca_auto.activity import _orca as activity_orca
    from orca_auto.activity_labels import queue_detail_text
    from orca_auto.activity_rendering import queue_list_table

    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    job_dir = tmp_path / job_dir_name
    job_dir.mkdir()
    source = job_dir / inp_name
    payload = f"{route}\n{_H2}".encode()
    source.write_bytes(payload)
    args.path = str(job_dir)
    reads: list[Path] = []
    real_read = run_inp.read_stable_regular_file

    def read_once(path: Path, **kwargs: Any) -> bytes:
        reads.append(Path(path))
        return real_read(path, **kwargs)

    monkeypatch.setattr(run_inp, "read_stable_regular_file", read_once)

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "submitted", result.stderr
    # The detail kind comes from the one read of the source, which stays unchanged.
    assert [path.resolve() for path in reads] == [source.resolve()]
    assert source.read_bytes() == payload
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata["job_type"] == job_type
    assert entry.metadata.get("detail_kind") == detail_kind
    assert Path(entry.metadata["selected_inp"]).name == inp_name
    record = json.loads(
        json.dumps(activity_orca.queue_record(entry, None, allowed_root=tmp_path).to_dict())
    )
    assert record["metadata"]["job_type"] == job_type
    assert record["metadata"].get("detail_kind") == detail_kind
    assert queue_detail_text(record) == label
    table = queue_list_table({"activities": [record], "active_simulations": 0}, max_width=None)
    [row] = table.rows
    # Status, Name, Detail, ID, Elapsed; no cell holds a space here.
    assert row.split()[2:4] == [label, entry.queue_id]


@pytest.mark.parametrize(
    ("inp_text", "dependencies"),
    [
        # The ESD(FLUOR) input execution binding admits with its Hessians.
        (
            "! B3LYP def2-SVP ESD(FLUOR)\n"
            '%esd\n  GSHessian "S0.hess"\n  ESHessian "S1.hess"\nend\n'
            "* xyzfile 0 1 g.xyz\n",
            {
                "g.xyz": "1\ng\nH 0 0 0\n",
                "S0.hess": "$hessian\nS0\n$end\n",
                "S1.hess": "$hessian\nS1\n$end\n",
            },
        ),
        # The ORCA 6.1 manual's vertical-gradient ESD(ABS) example: the ESD
        # module computes excited-state derivatives after the single point.
        (
            "! B3LYP DEF2-SVP TIGHTSCF ESD(ABS)\n"
            "%TDDFT NROOTS 5 IROOT 1 END\n"
            '%ESD\n  GSHESSIAN "BEN.hess"\n  DOHT TRUE\n  HESSFLAG VG # DEFAULT\nEND\n'
            "* XYZFILE 0 1 BEN.xyz\n",
            {"BEN.xyz": "1\nben\nH 0 0 0\n", "BEN.hess": "$hessian\nBEN\n$end\n"},
        ),
    ],
    ids=["esd-fluor-hessians", "esd-abs-vertical-gradient"],
)
def test_submitted_esd_input_keeps_job_type_and_never_renders_sp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inp_text: str,
    dependencies: dict[str, str],
) -> None:
    from orca_auto.activity import _orca as activity_orca
    from orca_auto.activity_labels import queue_detail_text
    from orca_auto.activity_rendering import queue_list_table

    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    # A single-point-looking name: neither it nor the method-only start of the
    # route may make an ESD job read as SP.
    job_dir = tmp_path / "MeOPh_sp_esd"
    job_dir.mkdir()
    for name, text in dependencies.items():
        (job_dir / name).write_text(text, encoding="utf-8")
    source = job_dir / "sp.inp"
    payload = inp_text.encode()
    source.write_bytes(payload)
    args.path = str(job_dir)
    reads: list[Path] = []
    real_read = run_inp.read_stable_regular_file

    def read_once(path: Path, **kwargs: Any) -> bytes:
        reads.append(Path(path))
        return real_read(path, **kwargs)

    monkeypatch.setattr(run_inp, "read_stable_regular_file", read_once)

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "submitted", result.stderr
    assert [path.resolve() for path in reads] == [source.resolve()]
    assert source.read_bytes() == payload
    [entry] = queue_adapter.list_queue(tmp_path)
    execution_dir = Path(entry.metadata["execution_snapshot"]["execution_dir"])
    for name, text in dependencies.items():
        assert (execution_dir / name).read_text(encoding="utf-8") == text
    # The coarse scientific type is untouched; only the display refuses SP.
    assert entry.metadata["job_type"] == "other"
    assert entry.metadata["detail_kind"] == "unknown"
    record = json.loads(
        json.dumps(activity_orca.queue_record(entry, None, allowed_root=tmp_path).to_dict())
    )
    assert record["metadata"]["job_type"] == "other"
    assert record["metadata"]["detail_kind"] == "unknown"
    assert queue_detail_text(record) == "Unknown"
    table = queue_list_table({"activities": [record], "active_simulations": 0}, max_width=None)
    [row] = table.rows
    # Status, Name, Detail, ID, Elapsed; no cell holds a space here.
    assert row.split()[2:4] == ["Unknown", entry.queue_id]
    assert "SP" not in row.split()


@pytest.mark.parametrize("failure_stage", ["metadata", "task_id", "intent_transition"])
@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_submission_cleans_created_snapshot_on_pre_enqueue_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    failure_type: type[BaseException],
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    source = (reaction_dir / "rxn.inp").read_bytes()
    snapshots: list[dict[str, Any]] = []
    original_build = run_inp.build_orca_execution_snapshot

    def build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        snapshot = original_build(*args, **kwargs)
        snapshots.append(snapshot)
        return snapshot

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise failure_type("injected pre-enqueue failure")

    monkeypatch.setattr(run_inp, "build_orca_execution_snapshot", build)
    monkeypatch.setattr(
        run_inp, "run_enqueue_publication", lambda *_a, **_k: pytest.fail("must not enqueue")
    )
    if failure_stage == "metadata":
        monkeypatch.setattr(run_inp, "build_queue_metadata", fail)
    elif failure_stage == "intent_transition":
        monkeypatch.setattr(run_inp, "transition_snapshot_intent", fail)
    else:
        original_token = run_inp.timestamped_token

        def token(prefix: str, **kwargs: Any) -> str:
            if prefix == "orca":
                fail()
            return original_token(prefix, **kwargs)

        monkeypatch.setattr(run_inp, "timestamped_token", token)
    with pytest.raises(failure_type, match="injected pre-enqueue failure"):
        run_inp.create_queued_submission(
            load_config(args.config),
            args,
            reaction_dir,
            selected_inp=reaction_dir / "rxn.inp",
        )
    assert len(snapshots) == 1
    assert not Path(snapshots[0]["execution_dir"]).exists()
    assert not list((tmp_path / ".orca_auto_snapshot_intents").glob("*.json"))
    assert (reaction_dir / "rxn.inp").read_bytes() == source


@pytest.mark.parametrize(("snapshot", "error"), [(None, "TypeError: "), ({}, "KeyError: ")])
def test_internal_snapshot_failure_is_not_invalid_user_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: dict[str, Any] | None,
    error: str,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    source_input = (reaction_dir / "rxn.inp").read_bytes()
    cleanup_calls: list[object] = []
    monkeypatch.setattr(
        run_inp, "build_orca_execution_snapshot", lambda *_args, **_kwargs: snapshot
    )
    monkeypatch.setattr(
        run_inp,
        "cleanup_unowned_orca_execution_snapshot",
        lambda *args, **kwargs: cleanup_calls.append((args, kwargs)),
    )

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "failed"
    assert result.reason == "queue_submission_failed"
    assert result.stderr.startswith(error)
    assert len(cleanup_calls) == 1
    assert queue_adapter.list_queue(tmp_path) == []
    assert not (tmp_path / "queue.json").exists()
    assert (reaction_dir / "rxn.inp").read_bytes() == source_input


def test_enqueue_save_after_commit_recovers_exact_row_and_submits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    original_save = queue_store.save_entries
    first_save = True

    def save_then_raise(root: Path, entries: Any) -> None:
        nonlocal first_save
        original_save(root, entries)
        if first_save:
            first_save = False
            raise RuntimeError("enqueue fsync failed after replace")

    monkeypatch.setattr(queue_store, "save_entries", save_then_raise)

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "submitted"
    assert result.queued_result is not None
    # The recovered row is parked for the worker repair pass instead of being
    # published inline after an unknown enqueue failure.
    assert "parked for worker repair" in (result.queued_result.worker_info.detail or "")
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING

    cfg = load_config(args.config)
    assert publication_repair.repair_queue_publication(cfg, tmp_path, entry)
    [repaired] = queue_adapter.list_queue(tmp_path)
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


def test_submission_normalizes_resources_only_in_private_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    source_inp = reaction_dir / "rxn.inp"
    source_payload = source_inp.read_bytes()

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "submitted"
    assert source_inp.read_bytes() == source_payload
    [entry] = queue_adapter.list_queue(tmp_path)
    private_input = Path(entry.metadata["selected_inp"])
    private_text = private_input.read_text(encoding="utf-8")
    assert "%pal" in private_text
    assert "nprocs 2" in private_text
    assert "%maxcore 2048" in private_text
    assert entry.metadata["resource_request"] == {
        "max_cores": 2,
        "max_memory_gb": 4,
    }


def test_notification_delivery_failure_does_not_park_queue_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(
        tmp_path,
        monkeypatch,
        messenger=MessengerConfig(
            discord=DiscordConfig(bot_token="synthetic-token", default_channel_id="123")
        ),
    )
    monkeypatch.setattr(
        queue_notifications, "notify_queue_enqueued_event", lambda *_args, **_kwargs: False
    )

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "submitted"
    assert result.queued_result is not None
    cfg = load_config(args.config)
    queue_notifications.notify_queued_jobs(cfg)
    for thread in threading.enumerate():
        if thread.name == "orca-queued-notification":
            thread.join(timeout=5)
    assert not result.queued_result.worker_info.detail
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    assert queue_entry_is_claimable(entry)
    assert entry.metadata[queue_entries.QUEUED_NOTIFICATION_PENDING_KEY] is False


def test_truncated_discord_response_does_not_park_queue_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(
        tmp_path,
        monkeypatch,
        messenger=MessengerConfig(
            discord=DiscordConfig(
                bot_token="synthetic-token",
                default_channel_id="123",
                max_attempts=1,
            )
        ),
    )

    class _TruncatedResponse:
        status = 200

        def getcode(self) -> int:
            return self.status

        def read(self) -> bytes:
            raise IncompleteRead(b'{"id":')

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> Literal[False]:
            return False

    monkeypatch.setattr(
        queue_notifications, "notify_queue_enqueued_event", notify_queue_enqueued_event
    )
    monkeypatch.setattr(
        discord_bot_mod,
        "urlopen",
        lambda *_args, **_kwargs: _TruncatedResponse(),
    )

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "submitted"
    assert result.queued_result is not None
    cfg = load_config(args.config)
    queue_notifications.notify_queued_jobs(cfg)
    for thread in threading.enumerate():
        if thread.name == "orca-queued-notification":
            thread.join(timeout=5)
    assert not result.queued_result.worker_info.detail
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    assert queue_entry_is_claimable(entry)
    assert entry.metadata[queue_entries.QUEUED_NOTIFICATION_PENDING_KEY] is False


def test_submission_rejects_distinct_sources_with_same_basename_before_enqueue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    reactant = reaction_dir / "reactant" / "input.xyz"
    product = reaction_dir / "product" / "input.xyz"
    reactant.parent.mkdir()
    product.parent.mkdir()
    payload = "1\nsame endpoint\nH 0 0 0\n"
    reactant.write_text(payload, encoding="utf-8")
    product.write_text(payload, encoding="utf-8")
    (reaction_dir / "rxn.inp").write_text(
        '! NEB-TS\n%neb\n  Product "product/input.xyz"\nend\n* xyzfile 0 1 reactant/input.xyz\n',
        encoding="utf-8",
    )

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "failed"
    assert result.reason == "invalid_submission_input"
    assert "different source paths use the same basename" in result.stderr
    assert "input.xyz" in result.stderr
    assert "product/input.xyz" in result.stderr
    assert "reactant/input.xyz" in result.stderr
    assert queue_adapter.list_queue(tmp_path) == []
    assert not [path for path in reaction_dir.iterdir() if is_visible_generation_name(path.name)]
    intent_root = tmp_path / ".orca_auto_snapshot_intents"
    assert not list(intent_root.glob("*.json"))


def test_closed_job_directory_resubmits_to_a_new_sibling_generation_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    first_result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))
    assert first_result.status == "submitted"
    [first] = queue_adapter.list_queue(tmp_path)
    assert queue_adapter.mark_completed(tmp_path, first.queue_id)
    assert queue_adapter.update_metadata(
        tmp_path,
        first.queue_id,
        {queue_entries.TERMINAL_REPLAY_METADATA_KEY: None},
    )

    second_result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert second_result.status == "submitted"
    first_after, second = queue_adapter.list_queue(tmp_path)
    first_generation = Path(first_after.metadata["execution_snapshot"]["execution_dir"])
    second_generation = Path(second.metadata["execution_snapshot"]["execution_dir"])
    assert first_generation != second_generation
    assert first_generation.parent == second_generation.parent == reaction_dir.resolve()
    assert first_generation.is_dir()
    assert second_generation.is_dir()
    assert queue_entries.queue_entry_force(second) is False


def test_complete_transition_after_commit_returns_submitted_with_truthful_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    original_save = queue_store.save_entries
    raised = False

    def save_complete_then_raise(root: Path, entries: Any) -> None:
        nonlocal raised
        original_save(root, entries)
        if not raised and any(
            entry.metadata.get(QUEUE_RECORD_SYNC_KEY) == QUEUE_RECORD_SYNC_COMPLETE
            for entry in entries
        ):
            raised = True
            raise RuntimeError("complete fsync failed after replace")

    monkeypatch.setattr(queue_store, "save_entries", save_complete_then_raise)

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "submitted"
    assert result.queued_result is not None
    # The COMPLETE committed before its durability barrier reported failure:
    # the row is durably COMPLETE (the token-gated park refused to touch it)
    # and the submitter defers honestly to the worker repair pass, which will
    # short-circuit on the durable COMPLETE.
    assert "worker repair will publish" in (result.queued_result.worker_info.detail or "")
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


def test_enqueue_save_without_commit_fails_cleanly_without_queue_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)

    def raise_without_save(_root: Path, _entries: Any) -> None:
        raise RuntimeError("enqueue write failed before commit")

    monkeypatch.setattr(queue_store, "save_entries", raise_without_save)

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "failed"
    assert result.reason == "queue_submission_failed"
    assert queue_adapter.list_queue(tmp_path) == []
    assert not (reaction_dir / ".orca_auto_orca_executions").exists()
    assert not [path for path in reaction_dir.iterdir() if is_visible_generation_name(path.name)]


def test_public_run_dir_guard_aborts_orca_before_durable_queue_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    stages: list[str] = []

    def reject_publication(stage: str) -> None:
        stages.append(stage)
        raise RuntimeError("run-dir target moved into reserved smoke results")

    with use_run_dir_publication_guard(reject_publication):
        result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "failed"
    assert result.reason == "queue_submission_failed"
    assert stages == ["ORCA target mutation preflight"]
    assert queue_adapter.list_queue(tmp_path) == []
    assert not (tmp_path / "queue.json").exists()
    assert not (tmp_path / "job_locations.json").exists()
    assert not (reaction_dir / "job_state.json").exists()
    assert not (reaction_dir / "job_report.json").exists()
    assert not [path for path in reaction_dir.iterdir() if is_visible_generation_name(path.name)]


@pytest.mark.parametrize(
    "guard_error",
    [
        pytest.param(
            RuntimeError("run-dir target moved into reserved smoke results"),
            id="runtime-error",
        ),
        pytest.param(
            KeyboardInterrupt("run-dir guard interrupted after commit"),
            id="keyboard-interrupt",
        ),
        pytest.param(
            SystemExit("run-dir guard exited after commit"),
            id="system-exit",
        ),
    ],
)
def test_public_run_dir_guard_compensates_orca_post_commit_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard_error: BaseException,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    stages: list[str] = []

    def reject_after_commit(stage: str) -> None:
        stages.append(stage)
        if stage.endswith("post-commit"):
            raise guard_error

    with use_run_dir_publication_guard(reject_after_commit):
        result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "failed"
    assert result.reason == "queue_submission_failed"
    assert str(guard_error) in result.stderr
    assert stages == [
        "ORCA target mutation preflight",
        "ORCA durable queue pre-commit",
        "ORCA durable queue post-commit",
    ]
    assert queue_adapter.list_queue(tmp_path) == []
    assert not (tmp_path / "job_locations.json").exists()
    assert not (reaction_dir / "job_state.json").exists()
    assert not (reaction_dir / "job_report.json").exists()
    assert not [path for path in reaction_dir.iterdir() if is_visible_generation_name(path.name)]


@pytest.mark.parametrize(
    "compensation_error",
    [
        pytest.param(
            OSError("queue compensation write failed before replace"),
            id="os-error",
        ),
        pytest.param(
            KeyboardInterrupt("queue compensation interrupted before replace"),
            id="keyboard-interrupt",
        ),
        pytest.param(
            SystemExit("queue compensation exited before replace"),
            id="system-exit",
        ),
    ],
)
def test_orca_compensation_failure_fences_row_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compensation_error: BaseException,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    stages: list[str] = []
    save_count = 0
    original_save = queue_store.save_entries

    def fail_compensation_before_replace(root: Path, entries: Any) -> None:
        nonlocal save_count
        save_count += 1
        if save_count == 2:
            raise compensation_error
        original_save(root, entries)

    def reject_after_commit(stage: str) -> None:
        stages.append(stage)
        if stage.endswith("post-commit"):
            raise RuntimeError("run-dir target moved into reserved smoke results")

    def reject_normal_recovery(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("guard-origin compensation failure must not use normal enqueue recovery")

    def reject_publication(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("guard-origin compensation failure must not publish a queued record")

    monkeypatch.setattr(queue_store, "save_entries", fail_compensation_before_replace)
    monkeypatch.setattr(enqueue_publication, "_recover_committed_enqueue", reject_normal_recovery)
    monkeypatch.setattr(enqueue_publication, "upsert_row_job_record", reject_publication)

    with use_run_dir_publication_guard(reject_after_commit):
        result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "failed"
    assert result.reason == "queue_enqueue_outcome_unknown"
    assert "run-dir target moved into reserved smoke results" in result.stderr
    assert "queue compensation outcome=not_restored" in result.stderr
    assert str(compensation_error) in result.stderr
    assert stages == [
        "ORCA target mutation preflight",
        "ORCA durable queue pre-commit",
        "ORCA durable queue post-commit",
    ]
    assert save_count == 3
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.status == QueueStatus.FAILED
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED
    assert entry.metadata[queue_entries.TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY] is True
    assert entry.metadata.get(queue_entries.TERMINAL_REPLAY_METADATA_KEY) is None
    assert "queue_after_commit_guard_failed" in entry.error
    assert queue_entry_is_claimable(entry) is False
    assert not (tmp_path / "job_locations.json").exists()
    assert not (reaction_dir / "job_state.json").exists()
    assert not (reaction_dir / "job_report.json").exists()
    assert Path(entry.metadata["execution_snapshot"]["execution_dir"]).is_dir()


def test_orca_adapter_rejects_fractional_priority_before_persistence(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()

    with pytest.raises(ValueError, match="priority must be an integer"):
        queue_adapter.enqueue(tmp_path, str(reaction_dir), priority=1.5)  # type: ignore[arg-type]

    assert queue_adapter.list_queue(tmp_path) == []


def test_ambiguous_postcommit_rows_fail_closed_and_remain_unclaimable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    original_save = queue_store.save_entries
    first_save = True

    def save_ambiguous_then_raise(root: Path, entries: Any) -> None:
        nonlocal first_save
        if first_save:
            first_save = False
            duplicate = replace(entries[-1], queue_id="q_ambiguous_duplicate")
            original_save(root, [*entries, duplicate])
            raise RuntimeError("enqueue fsync failed with duplicate durable rows")
        original_save(root, entries)

    monkeypatch.setattr(queue_store, "save_entries", save_ambiguous_then_raise)

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "failed"
    assert result.reason == "queue_enqueue_outcome_unknown"
    entries = queue_adapter.list_queue(tmp_path)
    assert len(entries) == 2
    assert all(entry.status == QueueStatus.CANCELLED for entry in entries)
    assert all(
        entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED for entry in entries
    )
    assert claim_next_entry(tmp_path) is None


def test_duplicate_error_after_commit_is_recovered_as_same_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    original_enqueue = queue_adapter.enqueue

    def enqueue_then_report_duplicate(*enqueue_args: Any, **enqueue_kwargs: Any) -> Any:
        entry = original_enqueue(*enqueue_args, **enqueue_kwargs)
        raise queue_adapter.DuplicateEntryError(str(reaction_dir), entry)

    monkeypatch.setattr(queue_adapter, "enqueue", enqueue_then_report_duplicate)

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "submitted"
    assert result.queued_result is not None
    assert "DuplicateEntryError" in (result.queued_result.worker_info.detail or "")
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING

    cfg = load_config(args.config)
    assert publication_repair.repair_queue_publication(cfg, tmp_path, entry)
    [repaired] = queue_adapter.list_queue(tmp_path)
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


def test_cancellation_waits_for_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    publication_started = threading.Event()
    allow_publication = threading.Event()
    cancel_finished = threading.Event()
    original_upsert = enqueue_publication.upsert_row_job_record
    submission_result: list[Any] = []
    cancellation_result: list[Any] = []

    def blocking_upsert(*upsert_args: Any, **upsert_kwargs: Any) -> None:
        publication_started.set()
        assert allow_publication.wait(timeout=5)
        original_upsert(*upsert_args, **upsert_kwargs)

    monkeypatch.setattr(enqueue_publication, "upsert_row_job_record", blocking_upsert)

    submit_thread = threading.Thread(
        target=lambda: submission_result.append(
            run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))
        )
    )
    submit_thread.start()
    assert publication_started.wait(timeout=5)
    [preparing_entry] = queue_adapter.list_queue(tmp_path)

    def cancel_entry() -> None:
        cancellation_result.append(queue_adapter.cancel(tmp_path, preparing_entry.queue_id))
        cancel_finished.set()

    cancel_thread = threading.Thread(target=cancel_entry)
    cancel_thread.start()
    assert not cancel_finished.wait(timeout=0.1)
    allow_publication.set()
    submit_thread.join(timeout=5)
    cancel_thread.join(timeout=5)

    assert submission_result[0].status == "submitted"
    assert cancellation_result[0].status == QueueStatus.CANCELLED
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.status == QueueStatus.CANCELLED
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


def test_submit_reports_an_unjudgeable_dead_running_row_as_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orca_auto.orca.queue.orphans import DeadRunningRowUnjudgeableError

    target = SimpleNamespace(
        cfg=None,
        allowed_root=tmp_path,
        reaction_dir=tmp_path / "job",
        selected_inp=tmp_path / "job" / "job.inp",
    )
    message = (
        f"{tmp_path / 'job'} has a RUNNING queue row left by a dead worker, and whether a "
        f"live slot still protects it cannot be judged: Admission slot file is not valid JSON: "
        f"{tmp_path / 'admission_slots.json'}. Repair or remove "
        f"{tmp_path / 'admission_slots.json'} before resubmitting."
    )

    def raise_unjudgeable(*_args: Any, **_kwargs: Any) -> Any:
        raise DeadRunningRowUnjudgeableError(message)

    monkeypatch.setattr(
        submission_mod, "resolve_submission_target", lambda *_args, **_kwargs: target
    )
    monkeypatch.setattr(submission_mod, "find_submission_conflict", lambda *_args: None)
    monkeypatch.setattr(submission_mod, "create_queued_submission", raise_unjudgeable)

    result = submission_mod.submit_reaction_dir_to_queue(
        SimpleNamespace(), cfg=make_app_cfg(tmp_path)
    )

    assert result.status == "failed"
    assert result.reason == "submission_conflict"
    assert result.stderr == message


def test_unjudgeable_dead_running_row_during_submit_leaves_no_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The directory's RUNNING row appears after the conflict check; its dead
    # worker's slot protection cannot be read, so the enqueue fails closed.
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    queue_adapter.enqueue(tmp_path, str(reaction_dir), task_id="task-dead-worker")
    running = claim_next_entry(tmp_path)
    assert running is not None and running.status == QueueStatus.RUNNING
    admission_file = AdmissionStore.for_root(admission_dir(tmp_path)).path
    admission_file.parent.mkdir(parents=True, exist_ok=True)
    admission_file.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(run_inp, "find_submission_conflict", lambda *_args: None)

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "failed"
    assert result.reason == "submission_conflict"
    assert str(admission_file) in result.stderr
    assert not [path for path in reaction_dir.iterdir() if is_visible_generation_name(path.name)]
    assert not list((tmp_path / ".orca_auto_snapshot_intents").glob("*.json"))
    assert queue_adapter.list_queue(tmp_path) == [running]


def test_an_input_edited_during_submission_yields_one_consistent_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The selected input is read once. Saves that land after that read, where
    # earlier versions read the file again, reach neither the queue metadata
    # nor the snapshot digests nor the bound copy.
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    selected = reaction_dir / "rxn.inp"
    original = b"! Opt\n%pal nprocs 2 end\n%maxcore 1000\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    selected.write_bytes(original)
    edits = iter(
        [
            b"! SP\n%pal nprocs 1 end\n%maxcore 500\n* xyz 0 1\nO 0 0 0\nH 0 0 0.96\n*\n",
            b"! Freq\n%pal nprocs 1 end\n%maxcore 500\n* xyz 0 1\nC 0 0 0\nO 0 0 1.13\n*\n",
        ]
    )
    real_read = run_inp.read_stable_regular_file
    real_build = run_inp.build_orca_execution_snapshot

    def read_then_edit(path: Path, **kwargs: Any) -> bytes:
        payload = real_read(path, **kwargs)
        selected.write_bytes(next(edits))
        return payload

    def edit_then_build(*build_args: Any, **build_kwargs: Any) -> dict[str, Any]:
        selected.write_bytes(next(edits))
        return real_build(*build_args, **build_kwargs)

    monkeypatch.setattr(run_inp, "read_stable_regular_file", read_then_edit)
    monkeypatch.setattr(run_inp, "build_orca_execution_snapshot", edit_then_build)

    result = run_inp.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))

    assert result.status == "submitted", result.stderr
    assert next(edits, None) is None
    [entry] = queue_adapter.list_queue(tmp_path)
    metadata = entry.metadata
    snapshot = metadata["execution_snapshot"]
    assert (metadata["job_type"], metadata["molecule_key"]) == ("opt", "H2")
    assert metadata["resource_request"] == {"max_cores": 2, "max_memory_gb": 2}
    assert snapshot["resource_request"] == metadata["resource_request"]
    assert snapshot["source_inputs"]["selected_source"]["sha256"] == (
        hashlib.sha256(original).hexdigest()
    )
    assert snapshot["source_inputs"]["selected_source"]["size_bytes"] == len(original)
    assert Path(metadata["selected_inp"]).read_bytes() == original
    assert b"Freq" in selected.read_bytes()
