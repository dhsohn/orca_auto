"""Storage and historical readers share fences while retaining their own policies."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.engine_runner import executable_identity
from orca_auto.core.queue.engine.input_snapshot import bind_direct_generation_owner
from orca_auto.orca import state_reading
from orca_auto.orca.job_locations import _runtime_context as runtime

CASES = [
    ("valid", (True, True, True)),
    ("normalized_payload", (True, True, True)),
    ("string_identity", (True, False, False)),
    ("float_identity", (True, False, False)),
    ("negative_identity", (False, False, False)),
    ("zero_inode", (False, False, False)),
    ("bool_identity", (False, False, False)),
    ("missing_identity", (False, False, False)),
    ("wrong_identity", (False, False, False)),
    ("wrong_owner", (False, False, False)),
    ("missing_owner", (False, False, False)),
    ("wrong_content_hash", (True, False, False)),
    ("changed_input", (True, False, False)),
    ("non_inp_input", (True, False, False)),
    ("nested_input", (False, False, False)),
    ("symlink_input", (False, False, False)),
    ("hardlink_input", (False, False, False)),
    ("missing_input", (False, False, False)),
    ("outside_input", (False, False, False)),
    ("generation_symlink", (False, False, False)),
    ("parent_symlink", (False, False, False)),
    ("missing_generation", (False, False, False)),
    ("noncanonical_generation", (False, False, False)),
    ("wrong_generation_name", (True, False, True)),
    ("spaced_generation_name", (True, False, True)),
    ("newline_generation_name", (True, False, True)),
    ("unsupported_snapshot_version", (True, False, True)),
    ("wrong_job_identity", (True, False, True)),
    ("queue_input_mismatch", (True, False, True)),
    ("snapshot_input_mismatch", (True, False, True)),
    ("payload_input_mismatch", (False, True, False)),
    ("outside_index", (True, False, False)),
    ("artifact_job_mismatch", (True, True, False)),
    ("job_symlink", (True, False, True)),
]


def _case_inputs(root: Path, case: str) -> tuple[Path, dict[str, Any], Any, Any, dict[str, Any]]:
    job = root / "job"
    generation = job / "20260923-120000-01234567"
    generation.mkdir(parents=True)
    selected = generation / "job.inp"
    selected.write_text("! HF STO-3G\n* xyz 0 1\nH 0 0 0\nH 0 0 1\n*\n")
    job_stat = job.stat()
    gen_stat = generation.stat()
    token = "generation-policy-test-owner"
    bind_direct_generation_owner(
        job,
        namespace=generation.name,
        expected_job_identity=(job_stat.st_dev, job_stat.st_ino),
        expected_generation_identity=(gen_stat.st_dev, gen_stat.st_ino),
        owner_token=token,
    )
    identity: dict[str, Any] = {"device": gen_stat.st_dev, "inode": gen_stat.st_ino}
    bound = executable_identity(selected)
    provenance: dict[str, Any] = {
        "execution_dir": str(generation),
        "execution_dir_identity": identity,
        "generation_owner_token": token,
        "bound_selected_identity": bound,
    }
    snapshot: dict[str, Any] = {
        "version": 2,
        "generation_name": generation.name,
        "job_dir_identity": {"device": job_stat.st_dev, "inode": job_stat.st_ino},
        "execution_dir": str(generation),
        "execution_dir_identity": identity,
        "snapshot_intent_token": token,
        "selected_inp": str(selected),
        "bound_selected_identity": bound,
    }
    payload: dict[str, Any] = {"selected_inp": str(selected), "execution_provenance": provenance}
    metadata = {
        "reaction_dir": str(job),
        "selected_inp": str(selected),
        "execution_snapshot": snapshot,
    }
    artifact = SimpleNamespace(job_dir=generation, state=payload, report=None)
    index_root = root

    if case == "normalized_payload":
        payload = {
            "input": {"primary_path": str(selected)},
            "engine_payload": {"execution_provenance": provenance},
        }
        artifact.state = payload
    elif case in {"string_identity", "float_identity"}:
        identity.update(
            {
                key: str(value) if case == "string_identity" else float(value)
                for key, value in identity.items()
            }
        )
    elif case in {"negative_identity", "zero_inode", "bool_identity", "wrong_identity"}:
        identity["inode"] = {
            "negative_identity": -1,
            "zero_inode": 0,
            "bool_identity": True,
            "wrong_identity": gen_stat.st_ino + 1,
        }[case]
    elif case == "missing_identity":
        identity.clear()
    elif case in {"wrong_owner", "missing_owner"}:
        provenance["generation_owner_token"] = snapshot["snapshot_intent_token"] = (
            "wrong" if case == "wrong_owner" else ""
        )
    elif case == "wrong_content_hash":
        bound["sha256"] = "0" * 64
    elif case == "changed_input":
        selected.write_text("changed after binding\n")
    elif case in {"non_inp_input", "nested_input", "symlink_input", "outside_input"}:
        replacement = {
            "non_inp_input": generation / "job.txt",
            "nested_input": generation / "nested" / "job.inp",
            "symlink_input": generation / "alias.inp",
            "outside_input": root / "outside.inp",
        }[case]
        replacement.parent.mkdir(exist_ok=True)
        if case == "symlink_input":
            replacement.symlink_to(selected)
            bound["path"] = str(replacement)
        else:
            selected.rename(replacement)
            bound.update(executable_identity(replacement))
        payload["selected_inp"] = metadata["selected_inp"] = snapshot["selected_inp"] = str(
            replacement
        )
    elif case == "hardlink_input":
        os.link(selected, generation / "second.inp")
    elif case == "missing_input":
        selected.unlink()
    elif case in {
        "generation_symlink",
        "parent_symlink",
        "missing_generation",
        "noncanonical_generation",
    }:
        if case == "generation_symlink":
            alternative = job / "20260923-120001-01234567"
            alternative.symlink_to(generation, target_is_directory=True)
        elif case == "parent_symlink":
            alias = root / "alias"
            alias.symlink_to(job, target_is_directory=True)
            alternative = alias / generation.name
        elif case == "missing_generation":
            alternative = job / "20260923-120001-01234567"
        else:
            alternative = generation / ".." / generation.name
        provenance["execution_dir"] = snapshot["execution_dir"] = str(alternative)
    elif case in {"wrong_generation_name", "spaced_generation_name", "newline_generation_name"}:
        snapshot["generation_name"] = {
            "wrong_generation_name": "20260923-120001-01234567",
            "spaced_generation_name": f" {generation.name} ",
            "newline_generation_name": generation.name + "\n",
        }[case]
    elif case == "unsupported_snapshot_version":
        snapshot["version"] = 1
    elif case == "wrong_job_identity":
        snapshot["job_dir_identity"]["inode"] += 1
    elif case == "queue_input_mismatch":
        metadata["selected_inp"] = str(generation / "other.inp")
    elif case == "snapshot_input_mismatch":
        snapshot["selected_inp"] = str(generation / "other.inp")
    elif case == "payload_input_mismatch":
        payload["selected_inp"] = str(generation / "other.inp")
    elif case == "outside_index":
        index_root = root / "other"
    elif case == "artifact_job_mismatch":
        artifact.job_dir = root
    elif case == "job_symlink":
        alias = root / "alias"
        alias.symlink_to(job, target_is_directory=True)
        job = alias
        metadata["reaction_dir"] = str(alias)

    inputs = runtime._RuntimeInputs(
        index_root=index_root,
        target="",
        queue_id="",
        run_id="",
        reaction_dir="",
        queue_entry={"metadata": metadata},
    )
    return job, payload, inputs, artifact, provenance


@pytest.mark.parametrize(("case", "expected"), CASES, ids=[case for case, _ in CASES])
def test_generation_validation_policies(
    tmp_path: Path, case: str, expected: tuple[bool, bool, bool]
) -> None:
    job, payload, inputs, artifact, provenance = _case_inputs(tmp_path, case)
    actual = (
        state_reading.verified_generation_artifact_target(job, payload) is not None,
        runtime._execution_generation(inputs) is not None,
        runtime._provenance_execution_generation(inputs, artifact, provenance) is not None,
    )
    assert actual == expected
