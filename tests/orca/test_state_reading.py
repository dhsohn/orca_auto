"""state_reading verifies a payload's generation target against the on-disk fence."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.queue.engine.input_snapshot import bind_direct_generation_owner
from orca_auto.orca import state_reading
from orca_auto.orca.engine_runner import executable_identity

CASES = [
    ("valid", True),
    ("normalized_payload", True),
    ("string_identity", True),
    ("float_identity", True),
    ("negative_identity", False),
    ("zero_inode", False),
    ("bool_identity", False),
    ("missing_identity", False),
    ("wrong_identity", False),
    ("wrong_owner", False),
    ("missing_owner", False),
    ("wrong_content_hash", True),
    ("changed_input", True),
    ("non_inp_input", True),
    ("nested_input", False),
    ("symlink_input", False),
    ("hardlink_input", False),
    ("missing_input", False),
    ("outside_input", False),
    ("generation_symlink", False),
    ("parent_symlink", False),
    ("missing_generation", False),
    ("noncanonical_generation", False),
    ("payload_input_mismatch", False),
    ("job_symlink", True),
]


def _case_inputs(root: Path, case: str) -> tuple[Path, dict[str, Any]]:
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
    payload: dict[str, Any] = {"selected_inp": str(selected), "execution_provenance": provenance}

    if case == "normalized_payload":
        payload = {
            "input": {"primary_path": str(selected)},
            "engine_payload": {"execution_provenance": provenance},
        }
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
        provenance["generation_owner_token"] = "wrong" if case == "wrong_owner" else ""
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
        payload["selected_inp"] = str(replacement)
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
        provenance["execution_dir"] = str(alternative)
    elif case == "payload_input_mismatch":
        payload["selected_inp"] = str(generation / "other.inp")
    elif case == "job_symlink":
        alias = root / "alias"
        alias.symlink_to(job, target_is_directory=True)
        job = alias
    return job, payload


@pytest.mark.parametrize(("case", "expected"), CASES, ids=[case for case, _ in CASES])
def test_verified_generation_artifact_target_policies(
    tmp_path: Path, case: str, expected: bool
) -> None:
    job, payload = _case_inputs(tmp_path, case)
    assert (state_reading.verified_generation_artifact_target(job, payload) is not None) is expected
