"""``job_state.json`` is validated once, where it is read from disk.

``state_reading.read_run_state`` classifies a state as valid, missing,
unsupported (a legacy or foreign state) or corrupt. Listings and the index
skip a corrupt state and report the others; a run never overwrites one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orca_auto.cli import main as cli_main
from orca_auto.core.queue import persistence as queue_persistence
from orca_auto.orca.attempt.resume import load_or_create_state
from orca_auto.orca.job_locations import rebuild_job_location_records
from orca_auto.orca.run_snapshot import collect_run_snapshots
from orca_auto.orca.state_reading import (
    CorruptRunStateError,
    load_state,
    read_run_state,
    state_path,
)
from orca_auto.orca.statuses import RunStatus
from tests.conftest import make_app_cfg, make_queue_entry, write_config_file, write_run_state


def _valid_payload(reaction_dir: Path) -> dict[str, Any]:
    write_run_state(reaction_dir, status=RunStatus.COMPLETED, job_id="job-valid")
    payload: dict[str, Any] = json.loads(state_path(reaction_dir).read_text(encoding="utf-8"))
    return payload


def _corrupt(reaction_dir: Path, path: tuple[str, ...], value: Any) -> None:
    payload = _valid_payload(reaction_dir)
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    state_path(reaction_dir).write_text(json.dumps(payload), encoding="utf-8")


_CORRUPT_FIELDS = {
    "attempts-int": (("engine_payload", "attempts"), 42),
    "attempts-string": (("engine_payload", "attempts"), "abc"),
    "attempts-of-ints": (("engine_payload", "attempts"), [1, 2]),
    "scratch-publications-object": (("engine_payload", "scratch_publications"), {}),
    "final-result-list": (("engine_payload", "final_result"), []),
    "provenance-string": (("engine_payload", "execution_provenance"), "x"),
    "job-list": (("job",), []),
    "status-state-list": (("status", "state"), ["completed"]),
    "schema-version-text": (("schema_version",), "one"),
    "schema-version-bool": (("schema_version",), True),
}


@pytest.mark.parametrize("case", sorted(_CORRUPT_FIELDS))
def test_wrongly_typed_v1_state_is_corrupt_not_reshaped(tmp_path: Path, case: str) -> None:
    path, value = _CORRUPT_FIELDS[case]
    _corrupt(tmp_path, path, value)

    read = read_run_state(tmp_path)

    assert read.kind == "corrupt"
    assert read.state is None
    assert read.reason
    assert load_state(tmp_path) is None


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("{not json", "corrupt"),
        ("[]", "corrupt"),
        ('"text"', "corrupt"),
        (json.dumps({"schema_version": 2, "engine": "orca"}), "unsupported"),
        (json.dumps({"schema_version": 1, "engine": "xtb"}), "unsupported"),
        (json.dumps({"run_id": "legacy", "status": "completed"}), "unsupported"),
    ],
)
def test_unreadable_and_foreign_files_are_classified(tmp_path: Path, text: str, kind: str) -> None:
    state_path(tmp_path).write_text(text, encoding="utf-8")

    assert read_run_state(tmp_path).kind == kind
    assert load_state(tmp_path) is None


def test_valid_and_missing_states(tmp_path: Path) -> None:
    assert read_run_state(tmp_path).kind == "missing"
    write_run_state(tmp_path, status=RunStatus.COMPLETED, job_id="job-valid")

    read = read_run_state(tmp_path)

    assert read.kind == "valid"
    assert read.state is not None
    assert read.state["job_id"] == "job-valid"
    assert read.state["attempts"] == []


def _mixed_runs_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    runs = tmp_path / "runs"
    good = runs / "good"
    bad = runs / "bad"
    write_run_state(good, status=RunStatus.COMPLETED, job_id="job-good")
    bad.mkdir(parents=True)
    _corrupt(bad, ("engine_payload", "attempts"), 42)
    return runs, good, bad


def test_listing_skips_a_corrupt_state_and_keeps_the_others(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runs, good, bad = _mixed_runs_root(tmp_path)

    snapshots = collect_run_snapshots(runs)

    assert [snapshot.reaction_dir for snapshot in snapshots] == [good]
    assert f"Skipping corrupt ORCA run state in {bad}" in caplog.text


def test_queue_list_and_index_rebuild_survive_a_corrupt_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs, good, bad = _mixed_runs_root(tmp_path)
    queue_persistence.save_entries(
        runs,
        [
            make_queue_entry(queue_id="q-good", task_id="job-good", reaction_dir=good),
            make_queue_entry(queue_id="q-bad", task_id="job-bad", reaction_dir=bad),
        ],
    )
    config = write_config_file(tmp_path / "orca_auto.yaml", make_app_cfg(runs))

    assert cli_main(["queue", "list", "--json", "--config", str(config)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert sorted(row["activity_id"] for row in listed["activities"]) == ["q-bad", "q-good"]

    assert cli_main(["index", "rebuild", "--dry-run", "--json", "--config", str(config)]) == 0
    rebuilt = json.loads(capsys.readouterr().out)
    assert rebuilt["scanned"] == 2
    assert [row["job_id"] for row in rebuilt["added"]] == ["job-good"]
    # The corrupt directory is reported, not fatal.
    assert rebuilt["skipped"] == [str(bad)]


def test_a_run_never_overwrites_a_corrupt_state(tmp_path: Path) -> None:
    _corrupt(tmp_path, ("engine_payload", "attempts"), 42)
    before = state_path(tmp_path).read_bytes()

    with pytest.raises(CorruptRunStateError, match="corrupt"):
        load_or_create_state(tmp_path, tmp_path / "rxn.inp")

    assert state_path(tmp_path).read_bytes() == before


def test_a_run_never_overwrites_unparseable_state_json(tmp_path: Path) -> None:
    state_path(tmp_path).write_text("{not json", encoding="utf-8")

    with pytest.raises(CorruptRunStateError, match="unreadable JSON"):
        load_or_create_state(tmp_path, tmp_path / "rxn.inp")

    assert state_path(tmp_path).read_text(encoding="utf-8") == "{not json"


def test_a_run_still_replaces_a_legacy_state(tmp_path: Path) -> None:
    inp = tmp_path / "rxn.inp"
    inp.write_text("! Opt\n", encoding="utf-8")
    state_path(tmp_path).write_text(json.dumps({"run_id": "legacy"}), encoding="utf-8")

    state, resumed = load_or_create_state(tmp_path, inp)

    assert resumed is False
    assert read_run_state(tmp_path).kind == "valid"
    assert state["selected_inp"] == str(inp)


# A copy of an identity field elsewhere in the envelope that disagrees with
# the canonical field. The writer never writes one; every reader must refuse
# the file instead of each choosing a different job.
_CONFLICTING_COPIES = {
    "engine-job-id": (("engine_payload", "job_id"), "legacy-engine-job"),
    "top-level-job-id": (("job_id",), "flat-job"),
    "job-task-id": (("job", "task_id"), "other-task"),
    "engine-queue-id": (("engine_payload", "queue_id"), "q-other"),
    "engine-job-dir": (("engine_payload", "job_dir"), "/elsewhere"),
    "engine-status": (("engine_payload", "status"), "running"),
    "engine-selected-inp": (("engine_payload", "selected_inp"), "/elsewhere/rxn.inp"),
    "top-level-run-id": (("run_id",), "run-other"),
}


@pytest.mark.parametrize("case", sorted(_CONFLICTING_COPIES))
def test_a_conflicting_identity_copy_makes_the_state_corrupt(tmp_path: Path, case: str) -> None:
    path, value = _CONFLICTING_COPIES[case]
    write_run_state(tmp_path, status=RunStatus.COMPLETED, job_id="canonical-job", run_id="run-1")
    payload = json.loads(state_path(tmp_path).read_text(encoding="utf-8"))
    target = payload
    for key in path[:-1]:
        target = target.setdefault(key, {})
    target[path[-1]] = value
    state_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    read = read_run_state(tmp_path)

    assert read.kind == "corrupt"
    assert "conflicts with" in read.reason


def test_an_agreeing_identity_copy_is_still_valid(tmp_path: Path) -> None:
    write_run_state(tmp_path, status=RunStatus.COMPLETED, job_id="canonical-job")
    payload = json.loads(state_path(tmp_path).read_text(encoding="utf-8"))
    payload["engine_payload"]["job_id"] = "canonical-job"
    payload["job_id"] = "canonical-job"
    state_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    read = read_run_state(tmp_path)

    assert read.kind == "valid"
    assert read.state is not None
    assert read.state["job_id"] == "canonical-job"


def test_the_reader_and_the_index_rebuild_name_the_same_job(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    valid = runs / "valid"
    write_run_state(valid, status=RunStatus.COMPLETED, job_id="job-valid")
    conflicting = runs / "conflicting"
    write_run_state(conflicting, status=RunStatus.COMPLETED, job_id="canonical-job")
    payload = json.loads(state_path(conflicting).read_text(encoding="utf-8"))
    payload["engine_payload"]["job_id"] = "legacy-engine-job"
    state_path(conflicting).write_text(json.dumps(payload), encoding="utf-8")

    preview = rebuild_job_location_records(runs, apply=False)

    read = read_run_state(valid)
    assert read.state is not None
    assert [row.job_id for row in preview.added] == [read.state["job_id"]]
    # The conflicting state is corrupt for the reader and skipped by the
    # rebuild; neither names ``legacy-engine-job``.
    assert read_run_state(conflicting).kind == "corrupt"
    assert preview.skipped == (str(conflicting),)
