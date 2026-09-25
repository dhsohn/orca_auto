from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    queue_record_sync_metadata,
)
from orca_auto.orca import submission
from orca_auto.orca.queue import adapter, publication_repair
from orca_auto.orca.queue.job_records import tracking_metadata_from_queue_entry
from tests.conftest import make_app_cfg, make_queue_entry
from tests.test_run_inp_submission import _real_submission


def test_repair_reproduces_submission_record_after_source_changes(tmp_path, monkeypatch):
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    result = submission.submit_reaction_dir_to_queue(args)
    assert result.status == "submitted"
    cfg = submission.load_config(args.config)
    index = tmp_path / "job_locations.json"
    [original] = json.loads(index.read_text())
    [entry] = adapter.list_queue(tmp_path)
    selected_input = original["selected_input_xyz"]
    assert selected_input == str(reaction_dir / "rxn.inp")
    (reaction_dir / "rxn.inp").write_text(
        "! Freq\n%pal nprocs 64 end\n%maxcore 8192\n* xyzfile 0 1 replacement.xyz\n"
    )
    # A repair has only the durable row and captured snapshot; both the source
    # and generation input can disappear without changing the recorded facts.
    Path(entry.metadata["selected_inp"]).unlink()
    index.write_text("[]")
    assert adapter.update_metadata(
        tmp_path,
        entry.queue_id,
        {
            "resource_actual": {},
            **queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_REPAIR_PENDING, token="repair", owner_pid=0
            ),
        },
    )
    [entry] = adapter.list_queue(tmp_path)
    assert publication_repair.repair_queue_publication(cfg, tmp_path, entry)
    [repaired] = json.loads(index.read_text())
    for record in [original, repaired]:
        record.pop("updated_at", None)
    assert repaired == original
    assert repaired["resource_request"] == {"max_cores": 2, "max_memory_gb": 4}
    assert repaired["resource_actual"] == repaired["resource_request"]


@pytest.mark.parametrize(
    "metadata,expected",
    [
        (
            {"resource_request": {"max_cores": 3, "max_memory_gb": 6}},
            {"max_cores": 3, "max_memory_gb": 6},
        ),
        (
            {"execution_snapshot": {"resource_request": {"max_cores": 4, "max_memory_gb": 8}}},
            {"max_cores": 4, "max_memory_gb": 8},
        ),
        ({}, {"max_cores": 2, "max_memory_gb": 4}),
    ],
)
def test_record_resource_fallback_uses_captured_facts_then_config(tmp_path, metadata, expected):
    from orca_auto.orca.config import CommonResourceConfig

    cfg = make_app_cfg(
        tmp_path, resources=CommonResourceConfig(max_cores_per_task=2, max_memory_gb_per_task=4)
    )
    entry = make_queue_entry(
        reaction_dir=tmp_path / "job",
        metadata={
            "selected_inp": str(tmp_path / "missing.inp"),
            "resource_actual": {},
            **metadata,
        },
    )
    _, _, _, requested, actual = tracking_metadata_from_queue_entry(cfg, entry)
    assert requested == actual == expected


def test_missing_captured_labels_do_not_read_current_input(tmp_path):
    inp = tmp_path / "changed.inp"
    inp.write_text("# TAG: new_molecule\n! OPT\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    entry = make_queue_entry(reaction_dir=tmp_path, metadata={"selected_inp": str(inp)})
    metadata = tracking_metadata_from_queue_entry(make_app_cfg(tmp_path), entry)
    assert metadata[0] == str(inp)
    assert metadata[1:3] == ("other", "unknown")
    inp.write_text("# TAG: yet_another_molecule\n! FREQ\n")
    assert tracking_metadata_from_queue_entry(make_app_cfg(tmp_path), entry) == metadata
