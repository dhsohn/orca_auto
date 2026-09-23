from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core import machine_observation
from orca_auto.orca import state_reading
from orca_auto.orca.report.publication import write_report_json
from orca_auto.orca.state import new_state, save_state
from orca_auto.orca.state_reading import load_report_json
from tests.engine_artifact_helpers import bind_report_generation


@pytest.fixture
def report_generation(tmp_path: Path) -> tuple[Path, Path]:
    selected = tmp_path / "sample.inp"
    selected.write_text("! HF STO-3G\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    state = dict(new_state(tmp_path, selected))
    generation = bind_report_generation(tmp_path, state)
    output = generation / "sample.out"
    output.write_text("****ORCA TERMINATED NORMALLY****\n")
    state.update(
        run_id="report-verification-run",
        status="completed",
        attempts=[{"index": 1, "inp_path": state["selected_inp"], "out_path": str(output)}],
        final_result={
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "normal_termination",
            "last_out_path": str(output),
        },
    )
    save_state(tmp_path, state)
    report = write_report_json(tmp_path, state)
    assert report is not None
    assert load_report_json(generation, require_consumable_success=True) is not None
    return generation, report


def test_machine_report_owned_fields_have_expected_values(
    report_generation: tuple[Path, Path],
) -> None:
    _, report = report_generation
    data = json.loads(report.read_text())["payload"]["data"]
    assert data["summary"] == {
        "status": "completed",
        "reason": "normal_termination",
        "analyzer_status": "completed",
        "attempt_count": 1,
    }
    assert data["results"] == {
        "run_id": "report-verification-run",
        "reason": "normal_termination",
        "analyzer_status": "completed",
        "attempt_count": 1,
        "resumed": False,
        "skipped_execution": False,
        "runner_error": "",
    }


@pytest.mark.parametrize("remove", [False, True])
@pytest.mark.parametrize(
    ("section", "field", "wrong"),
    [
        ("summary", "status", "failed"),
        ("summary", "reason", "error_termination"),
        ("summary", "analyzer_status", "incomplete"),
        ("summary", "attempt_count", 999),
        ("results", "run_id", "another-run"),
        ("results", "reason", "error_termination"),
        ("results", "analyzer_status", "incomplete"),
        ("results", "attempt_count", 999),
        ("results", "resumed", True),
        ("results", "skipped_execution", True),
        ("results", "runner_error", "unexpected error"),
    ],
)
def test_machine_report_rejects_inconsistent_or_missing_owned_fields(
    report_generation: tuple[Path, Path], section: str, field: str, wrong: Any, remove: bool
) -> None:
    generation, report = report_generation
    observation = json.loads(report.read_text())
    fields = observation["payload"]["data"][section]
    assert fields[field] != wrong
    if remove:
        del fields[field]
    else:
        fields[field] = wrong
    report.write_text(json.dumps(observation))
    assert load_report_json(generation, require_consumable_success=True) is None


def test_machine_report_accepts_additive_domain_fields(
    report_generation: tuple[Path, Path],
) -> None:
    generation, report = report_generation
    observation = json.loads(report.read_text())
    for section in ("summary", "results"):
        observation["payload"]["data"][section]["future_field"] = {"value": 1}
    report.write_text(json.dumps(observation))
    assert load_report_json(generation, require_consumable_success=True) is not None


def test_report_hashes_each_available_file_once_per_load(
    report_generation: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    generation, report = report_generation
    observation = json.loads(report.read_text())
    observation["artifacts"]["another-log-reference"] = {
        **observation["artifacts"]["orca-output"],
        "required": False,
        "role": "auxiliary",
    }
    report.write_text(json.dumps(observation))
    reads: Counter[str] = Counter()
    real_consume = machine_observation.ReceiptDigest.consume

    def count_hash(self: Any, stream: Any) -> None:
        reads[Path(stream.name).name] += 1
        real_consume(self, stream)

    monkeypatch.setattr(machine_observation.ReceiptDigest, "consume", count_hash)
    monkeypatch.setattr(
        "orca_auto.core.engine_runner.executable_identity",
        lambda *_args: pytest.fail("report verification must reuse the input receipt"),
    )
    assert load_report_json(generation, require_consumable_success=True) is not None
    assert reads == {"sample.inp": 1, "sample.out": 1}
    # A new read must observe new bytes rather than reuse a process-wide cache.
    assert load_report_json(generation, require_consumable_success=True) is not None
    assert reads == {"sample.inp": 2, "sample.out": 2}
    (generation / "sample.out").write_text("changed output\n")
    assert load_report_json(generation) is None
    assert reads == {"sample.inp": 2, "sample.out": 3}


@pytest.mark.parametrize("filename", ["sample.inp", "sample.out"])
@pytest.mark.parametrize("change", ["rewrite", "replace", "symlink", "hardlink", "remove"])
def test_report_rejects_artifact_changes_after_hashing(
    report_generation: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    change: str,
) -> None:
    generation, _ = report_generation
    path = generation / filename
    original = state_reading.read_verified_artifacts
    mutation_ran = False

    def mutate_after_hashing(*args: Any, **kwargs: Any) -> Any:
        nonlocal mutation_ran
        target = original(*args, **kwargs)
        assert target is not None
        payload = path.read_bytes()
        if change == "rewrite":
            before = path.stat()
            path.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        elif change == "replace":
            replacement = generation / "replacement"
            replacement.write_bytes(payload)
            replacement.replace(path)
        elif change == "symlink":
            external = generation.parent / "external-artifact"
            external.write_bytes(payload)
            path.unlink()
            path.symlink_to(external)
        elif change == "hardlink":
            os.link(path, generation / "another-link")
        else:
            path.unlink()
        mutation_ran = True
        return target

    monkeypatch.setattr(state_reading, "read_verified_artifacts", mutate_after_hashing)
    assert load_report_json(generation, require_consumable_success=True) is None
    assert mutation_ran


@pytest.mark.parametrize("artifact_id", ["input", "orca-output"])
def test_report_receipt_must_bind_the_state_selected_path(
    report_generation: tuple[Path, Path], artifact_id: str
) -> None:
    generation, report = report_generation
    observation = json.loads(report.read_text())
    receipt = observation["artifacts"][artifact_id]
    original = generation / receipt["path"]
    other = generation / ("other" + original.suffix)
    other.write_bytes(original.read_bytes())
    receipt["path"] = other.name
    report.write_text(json.dumps(observation))
    assert load_report_json(generation, require_consumable_success=True) is None


@pytest.mark.parametrize("mutation_stage", ["ownership", "output_hash"])
@pytest.mark.parametrize("input_aliases", [False, True])
def test_final_input_hash_observes_same_tick_writes(
    report_generation: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    mutation_stage: str,
    input_aliases: bool,
) -> None:
    generation, report = report_generation
    selected = generation / "sample.inp"
    if input_aliases:
        observation = json.loads(report.read_text())
        receipt = observation["artifacts"]["input"]
        for name, path in (("early-relative", selected.name), ("early-absolute", str(selected))):
            observation["artifacts"][name] = {**receipt, "path": path, "required": False}
        report.write_text(json.dumps(observation))
    # WSL can keep identical stat fields across fast same-size writes. The
    # final content binding must not depend solely on a changed timestamp.
    monkeypatch.setattr(machine_observation.VerifiedArtifact, "is_unchanged", lambda self: True)
    assert load_report_json(generation) is not None
    mutations: list[bytes] = []

    def change_input() -> None:
        original = selected.read_bytes()
        changed = bytes([original[0] ^ 1]) + original[1:]
        selected.write_bytes(changed)
        mutations.append(changed)

    if mutation_stage == "ownership":
        owner = state_reading.verified_generation_artifact_target

        def after_ownership(*args: Any, **kwargs: Any) -> Any:
            target = owner(*args, **kwargs)
            assert target is not None
            change_input()
            return target

        monkeypatch.setattr(state_reading, "verified_generation_artifact_target", after_ownership)
    else:
        consume = machine_observation.ReceiptDigest.consume

        def after_output(self: Any, stream: Any) -> None:
            consume(self, stream)
            if Path(stream.name).name == "sample.out":
                change_input()

        monkeypatch.setattr(machine_observation.ReceiptDigest, "consume", after_output)
    assert load_report_json(generation) is None
    assert len(mutations) == 1
    assert selected.read_bytes() == mutations[0]
