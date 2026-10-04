"""Optional HTML/SI reports: deliberate absence versus a generation failure.

``job_report.html`` and ``si_block.md`` are optional (``required: false``):
neither their absence nor their failure blocks science, delivery or handoff.
A report the job type does not have is simply absent. A report the job type
has but that could not be rendered or written before the first terminal
publication is recorded in the producer results as
``payload.data.results.report_generation`` with fixed, content-free outcomes
keyed by the optional artifact ID; exception text never leaks. A failed report
gets no artifact receipt, so nothing is presented as available. The map is
only published when some optional report failed, and a published terminal
observation is never regenerated, rewritten or backfilled.

Inputs and outputs are SYNTHETIC (``sp_out_text`` from
``tests/orca_output_helpers.py`` plus small literal inputs). Failures are
injected only at the renderer or the confined writer; publication, receipts,
the machine builder and the validator run for real.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.report import publication as publication_module
from orca_auto.orca.report import si as si_module
from orca_auto.orca.report.publication import write_report_files, write_report_json
from orca_auto.orca.state import normalized_payload_from_state
from tests.engine_artifact_helpers import bind_report_generation
from tests.machine_contract_helpers import validate_common_machine
from tests.orca_output_helpers import sp_out_text

_SP_INP = "! wB97M-V def2-TZVPP CPCM(toluene)\n* xyz 0 1\nC 0 0 0\nH 0 0 1.1\n*\n"
# Plain MD has neither an HTML report nor an SI block (docs/ARCHITECTURE.md).
_MD_INP = "! MD B3LYP def2-SVP\n* xyz 0 1\nC 0 0 0\nH 0 0 1.1\n*\n"
_SECRET = "SECRET-renderer-detail /home/raw/research/path"
_OPTIONAL_IDS = ("human-report", "supporting-information")


def _job(tmp_path: Path, name: str, inp_text: str) -> tuple[Path, Path, dict[str, Any]]:
    """A completed synthetic job whose input and output live in a bound generation."""
    job_dir = tmp_path / name
    job_dir.mkdir()
    (job_dir / "rxn.inp").write_text(inp_text, encoding="utf-8")
    state: dict[str, Any] = {
        "job_id": f"job_{name}",
        "run_id": f"run_{name}",
        "reaction_dir": str(job_dir),
        "selected_inp": str(job_dir / "rxn.inp"),
        "status": "completed",
        "started_at": "2026-07-03T01:00:00+00:00",
        "updated_at": "2026-07-03T02:15:30+00:00",
    }
    generation = bind_report_generation(job_dir, state)
    out_path = generation / "rxn.out"
    out_path.write_text(sp_out_text(), encoding="utf-8")
    state["attempts"] = [
        {
            "index": 1,
            "inp_path": state["selected_inp"],
            "out_path": str(out_path),
            "return_code": 0,
            "analyzer_status": "completed",
            "analyzer_reason": "normal_termination",
            "markers": {},
            "patch_actions": [],
            "started_at": "2026-07-03T01:00:00+00:00",
            "ended_at": "2026-07-03T02:15:00+00:00",
        }
    ]
    state["final_result"] = {
        "status": "completed",
        "analyzer_status": "completed",
        "reason": "normal_termination",
        "completed_at": "2026-07-03T02:15:30+00:00",
        "last_out_path": str(out_path),
    }
    return job_dir, generation, state


def _machine(generation: Path) -> tuple[str, dict[str, Any]]:
    text = (generation / "machine.json").read_text(encoding="utf-8")
    return text, json.loads(text)


def _results(observation: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = observation["payload"]["data"]["results"]
    return results


def _generation_bytes(generation: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in generation.iterdir() if path.is_file()}


def _assert_science_and_handoff_unblocked(observation: dict[str, Any]) -> None:
    assert _results(observation)["science"]["status"] == "verified"
    assert observation["lifecycle"]["outcome"] == "succeeded"
    assert observation["delivery"] == {"status": "complete", "codes": []}
    assert observation["handoff"] == {"status": "ready", "codes": []}


def _fail_renderers(monkeypatch: pytest.MonkeyPatch, *, html: bool, si: bool) -> None:
    def broken(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError(_SECRET)

    if html:
        monkeypatch.setattr(publication_module, "compose_job_report_html", broken)
    if si:
        monkeypatch.setattr(si_module, "render_si_block_md", broken)


def test_job_type_without_reports_publishes_no_receipt_and_no_failure(tmp_path: Path) -> None:
    job_dir, generation, state = _job(tmp_path, "md_job", _MD_INP)

    reports = write_report_files(job_dir, state)

    assert set(reports) == {"report_json"}
    _, observation = _machine(generation)
    assert not set(_OPTIONAL_IDS) & set(observation["artifacts"])
    assert "report_generation" not in _results(observation)
    _assert_science_and_handoff_unblocked(observation)
    validate_common_machine(generation / "machine.json")


def test_produced_reports_keep_available_optional_receipts_and_no_failure_map(
    tmp_path: Path,
) -> None:
    job_dir, generation, state = _job(tmp_path, "sp_ok", _SP_INP)

    reports = write_report_files(job_dir, state)

    assert {"report_html", "si_block", "report_json"} <= set(reports)
    _, observation = _machine(generation)
    for artifact_id in _OPTIONAL_IDS:
        receipt = observation["artifacts"][artifact_id]
        assert receipt["status"] == "available"
        assert receipt["required"] is False
    assert "report_generation" not in _results(observation)
    _assert_science_and_handoff_unblocked(observation)


def test_renderer_exceptions_are_recorded_as_generation_failures_without_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir, generation, state = _job(tmp_path, "sp_broken", _SP_INP)
    _fail_renderers(monkeypatch, html=True, si=True)

    reports = write_report_files(job_dir, state)

    assert set(reports) == {"report_json"}
    text, observation = _machine(generation)
    # No receipt pretends a failed report is available.
    assert not set(_OPTIONAL_IDS) & set(observation["artifacts"])
    assert _results(observation)["report_generation"] == {
        "human-report": "generation-failed",
        "supporting-information": "generation-failed",
    }
    assert _SECRET not in text
    assert "RuntimeError" not in text
    assert all(_SECRET not in value for value in reports.values())
    # Optional failure blocks neither science, delivery nor handoff, and adds
    # no envelope code.
    _assert_science_and_handoff_unblocked(observation)
    validate_common_machine(generation / "machine.json")


def test_one_failed_report_does_not_hide_the_other_produced_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir, generation, state = _job(tmp_path, "sp_si_broken", _SP_INP)
    _fail_renderers(monkeypatch, html=False, si=True)

    reports = write_report_files(job_dir, state)

    assert "report_html" in reports and "si_block" not in reports
    _, observation = _machine(generation)
    assert observation["artifacts"]["human-report"]["status"] == "available"
    assert "supporting-information" not in observation["artifacts"]
    assert _results(observation)["report_generation"] == {
        "human-report": "produced",
        "supporting-information": "generation-failed",
    }
    _assert_science_and_handoff_unblocked(observation)


def test_failed_si_collector_beside_a_job_type_without_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir, generation, state = _job(tmp_path, "md_si_broken", _MD_INP)

    def broken_collector(*_args: object, **_kwargs: object) -> object:
        raise OSError(_SECRET)

    # MD deliberately has no HTML report; only the SI collector fails.
    monkeypatch.setattr(si_module.evidence, "collect_structure_evidence", broken_collector)

    write_report_files(job_dir, state)

    text, observation = _machine(generation)
    assert _results(observation)["report_generation"] == {
        "human-report": "not-applicable",
        "supporting-information": "generation-failed",
    }
    assert _SECRET not in text
    _assert_science_and_handoff_unblocked(observation)


def test_write_failure_keeps_a_stale_preterminal_report_unadopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir, generation, state = _job(tmp_path, "sp_write_broken", _SP_INP)
    stale = generation / "job_report.html"
    stale.write_bytes(b"<p>stale preterminal report</p>\n")
    real_write = publication_module.atomic_write_confined_bytes

    def html_write_fails(root: Path, path: Path, *args: Any, **kwargs: Any) -> Any:
        if Path(path).name == "job_report.html":
            raise OSError(_SECRET)
        return real_write(root, path, *args, **kwargs)

    monkeypatch.setattr(publication_module, "atomic_write_confined_bytes", html_write_fails)

    first = write_report_files(job_dir, state)

    assert "report_html" not in first
    text, observation = _machine(generation)
    assert "human-report" not in observation["artifacts"]
    assert _results(observation)["report_generation"]["human-report"] == "generation-failed"
    assert _SECRET not in text
    # The stale file is left as it was (not destroyed) but never adopted, on
    # first publication or on re-entry.
    assert stale.read_bytes() == b"<p>stale preterminal report</p>\n"
    monkeypatch.setattr(publication_module, "atomic_write_confined_bytes", real_write)
    again = write_report_files(job_dir, state)
    assert "report_html" not in again
    assert again["report_json"] == first["report_json"]


def test_invalid_optional_receipt_stays_optional_and_nonblocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir, generation, state = _job(tmp_path, "sp_invalid_receipt", _SP_INP)
    real_writer = publication_module.write_job_html_report

    def hardlinked_report(*args: Any, **kwargs: Any) -> Path | None:
        path = real_writer(*args, **kwargs)
        assert path is not None
        # A second link makes the receipt writer reject the file as invalid.
        os.link(path, tmp_path / "extra-link.html")
        return path

    monkeypatch.setattr(publication_module, "write_job_html_report", hardlinked_report)

    first = write_report_files(job_dir, state)

    _, observation = _machine(generation)
    receipt = observation["artifacts"]["human-report"]
    assert receipt["status"] == "invalid"
    assert receipt["required"] is False
    assert receipt["path"] is None
    # The renderer itself succeeded; the receipt, not a failure map, says invalid.
    assert "report_generation" not in _results(observation)
    _assert_science_and_handoff_unblocked(observation)
    # The returned paths follow the published receipts: the HTML whose receipt
    # is invalid is not listed, the receipted SI block is.
    assert observation["artifacts"]["supporting-information"]["status"] == "available"
    assert observation["artifacts"]["supporting-information"]["required"] is False
    assert "report_html" not in first
    assert "si_block" in first

    published = _generation_bytes(generation)

    def must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a terminal generation must not regenerate optional reports")

    monkeypatch.setattr(publication_module, "write_job_html_report", must_not_run)
    monkeypatch.setattr(publication_module, "write_si_block", must_not_run)

    again = write_report_files(job_dir, state)

    # Re-entry lists the same reports and leaves every terminal byte as published.
    assert again == first
    assert _generation_bytes(generation) == published


def test_first_terminal_failure_snapshot_is_immutable_on_reentry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir, generation, state = _job(tmp_path, "sp_reentry", _SP_INP)
    _fail_renderers(monkeypatch, html=True, si=True)
    first = write_report_files(job_dir, state)
    published = _generation_bytes(generation)
    assert "report_generation" in _results(_machine(generation)[1])

    def must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a terminal generation must not regenerate optional reports")

    monkeypatch.setattr(publication_module, "write_job_html_report", must_not_run)
    monkeypatch.setattr(publication_module, "write_si_block", must_not_run)
    changed = dict(state)
    changed["updated_at"] = "2026-07-04T00:00:00+00:00"

    second = write_report_files(job_dir, changed)

    assert second == first
    assert _generation_bytes(generation) == published


def test_legacy_terminal_document_without_diagnostics_is_never_backfilled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir, generation, state = _job(tmp_path, "sp_legacy", _SP_INP)
    # A terminal observation written by a direct (legacy) caller: no report
    # receipts and no report_generation map.
    legacy_path = write_report_json(job_dir, normalized_payload_from_state(job_dir, state))
    assert legacy_path is not None
    legacy_bytes = legacy_path.read_bytes()
    legacy = json.loads(legacy_bytes)
    assert "report_generation" not in _results(legacy)
    assert not set(_OPTIONAL_IDS) & set(legacy["artifacts"])
    stale = generation / "si_block.md"
    stale.write_bytes(b"stale si\n")
    published = _generation_bytes(generation)

    def must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a legacy terminal generation must not be backfilled")

    monkeypatch.setattr(publication_module, "write_job_html_report", must_not_run)
    monkeypatch.setattr(publication_module, "write_si_block", must_not_run)

    reports = write_report_files(job_dir, state)

    assert legacy_path.read_bytes() == legacy_bytes
    assert _generation_bytes(generation) == published
    # The unreceipted stale file is not reported as a published SI block.
    assert reports == {"report_json": str(legacy_path)}
