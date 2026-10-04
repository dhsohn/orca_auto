"""One TS publication, three surfaces: machine.json, job_report.html and si_block.md.

Each case publishes through the real ``write_report_files`` into a bound
generation, so machine science, the HTML report and the SI block are produced
by their production consumers from the same receipted input and output bytes.

The inputs and outputs here are SYNTHETIC test fixtures (``si_out_text`` from
``tests/orca_output_helpers.py`` plus small literal inputs); no authentic ORCA
bytes are used or changed. A TS search keeps its operation identity (``TS``
report, ``OptTS`` route) whatever its constraints: the title names the
operation and never by itself certifies a first-order saddle. Only an
unconstrained TS search whose final frequency section has exactly one
imaginary mode may carry that claim, in machine.json or in human wording.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.report.publication import write_report_files
from tests.engine_artifact_helpers import bind_report_generation
from tests.machine_contract_helpers import validate_common_machine
from tests.orca_output_helpers import si_out_text

_ROUTE = "wB97X-D3 def2-TZVP OptTS Freq"
_GEOMETRY = "* xyz 0 1\nC 0 0 0\nH 0 0 1.1\n*\n"
# Synthetic inputs. The constraint block is the form test_machine_science.py
# already pins as a constrained TS (geometry_scope "partial").
_UNCONSTRAINED_INP = f"! {_ROUTE}\n{_GEOMETRY}"
_CONSTRAINED_INP = f"! {_ROUTE}\n%geom Constraints {{ B 0 1 C }} end end\n{_GEOMETRY}"

_CONSTRAINED_WORDING = "constrained ts search"
_UNVERIFIED_SADDLE_WORDING = "first-order saddle unverified"


def _publish(
    tmp_path: Path,
    name: str,
    *,
    inp_text: str,
    out_text: str,
    status: str,
    reason: str,
) -> dict[str, Any]:
    """Publish one synthetic job through ``write_report_files``; return all three surfaces."""
    job_dir = tmp_path / name
    job_dir.mkdir()
    (job_dir / "rxn.inp").write_text(inp_text, encoding="utf-8")
    state: dict[str, Any] = {
        "job_id": f"job_{name}",
        "run_id": f"run_{name}",
        "reaction_dir": str(job_dir),
        "selected_inp": str(job_dir / "rxn.inp"),
        "status": status,
        "started_at": "2026-07-03T01:00:00+00:00",
        "updated_at": "2026-07-03T02:15:30+00:00",
    }
    generation = bind_report_generation(job_dir, state)
    # The output lives in the generation so its receipt binds the same bytes
    # the HTML and SI collectors read.
    out_path = generation / "rxn.out"
    out_path.write_text(out_text, encoding="utf-8")
    state["attempts"] = [
        {
            "index": 1,
            "inp_path": state["selected_inp"],
            "out_path": str(out_path),
            "return_code": 0,
            "analyzer_status": "completed" if status == "completed" else "incomplete",
            "analyzer_reason": reason,
            "markers": {},
            "patch_actions": [],
            "started_at": "2026-07-03T01:00:00+00:00",
            "ended_at": "2026-07-03T02:15:00+00:00",
        }
    ]
    state["final_result"] = {
        "status": status,
        "analyzer_status": "completed" if status == "completed" else "incomplete",
        "reason": reason,
        "completed_at": "2026-07-03T02:15:30+00:00",
        "last_out_path": str(out_path),
    }

    reports = write_report_files(job_dir, state)

    machine_path = Path(reports["report_json"])
    html_path = generation / "job_report.html"
    si_path = generation / "si_block.md"
    return {
        "reports": reports,
        "machine_path": machine_path,
        "machine": json.loads(machine_path.read_text(encoding="utf-8")),
        "science": json.loads(machine_path.read_text(encoding="utf-8"))["payload"]["data"][
            "results"
        ]["science"],
        "html": html_path.read_text(encoding="utf-8") if html_path.is_file() else None,
        "si": si_path.read_text(encoding="utf-8") if si_path.is_file() else None,
    }


def _assert_ts_operation_identity(published: dict[str, Any]) -> None:
    """The page stays a TS search report; constraints never relabel it SP or Partial Opt."""
    html = published["html"]
    assert html is not None
    assert "TS report" in html
    assert "SP report" not in html
    assert "Partial Opt report" not in html
    assert "OptTS" in html


def test_constrained_optts_stays_a_ts_search_with_an_unverified_saddle(tmp_path: Path) -> None:
    published = _publish(
        tmp_path,
        "constrained_optts",
        inp_text=_CONSTRAINED_INP,
        out_text=si_out_text(route=_ROUTE, freqs=(-512.3, 120.0), thermo=True),
        status="completed",
        reason="ts_criteria_met",
    )

    # machine.json: the completion criteria (exactly one imaginary mode) are
    # met and kept, but the restricted geometry earns no saddle label.
    validate_common_machine(published["machine_path"])
    science = published["science"]
    assert science["status"] == "verified"
    assert science["reason"] == "ts_criteria_met"
    assert science["frequencies_available"] is True
    assert science["imaginary_frequency_count"] == 1
    assert science["geometry_scope"] == "partial"
    assert science["stationary_point"] == "unverified"

    # HTML: still a TS search, the scientific numbers stay, and the wording
    # bounds the claim instead of calling one imaginary mode "as expected".
    _assert_ts_operation_identity(published)
    html = published["html"]
    assert "-512.3" in html
    assert "as expected for a TS" not in html
    assert _CONSTRAINED_WORDING in html.lower()
    assert _UNVERIFIED_SADDLE_WORDING in html.lower()

    # SI: the TS record keeps route, energy, Nimag and coordinates, and warns
    # that the saddle is unverified.
    si = published["si"]
    assert si is not None
    assert published["reports"]["si_block"].endswith("si_block.md")
    si_lines = si.splitlines()
    assert "OptTS" in si_lines[1]
    assert "-1234.567890 Eh" in si
    assert "Nimag = 1" in si
    assert "C       0.000000     1.234567    -0.987654" in si
    warnings = [line for line in si_lines if line.startswith("⚠")]
    assert any(
        _CONSTRAINED_WORDING in line.lower() and _UNVERIFIED_SADDLE_WORDING in line.lower()
        for line in warnings
    ), warnings
    assert "single point" not in si.lower()


def test_unconstrained_optts_with_one_final_imaginary_mode_keeps_its_saddle(
    tmp_path: Path,
) -> None:
    published = _publish(
        tmp_path,
        "unconstrained_optts",
        inp_text=_UNCONSTRAINED_INP,
        out_text=si_out_text(route=_ROUTE, freqs=(-512.3, 120.0), thermo=True),
        status="completed",
        reason="ts_criteria_met",
    )

    validate_common_machine(published["machine_path"])
    science = published["science"]
    assert science["status"] == "verified"
    assert science["imaginary_frequency_count"] == 1
    assert science["geometry_scope"] == "transition_state"
    assert science["stationary_point"] == "first_order_saddle"

    _assert_ts_operation_identity(published)
    html = published["html"]
    assert "as expected for a TS" in html
    assert _CONSTRAINED_WORDING not in html.lower()
    assert _UNVERIFIED_SADDLE_WORDING not in html.lower()

    si = published["si"]
    assert si is not None
    assert "Nimag = 1" in si
    assert "⚠" not in si
    assert _CONSTRAINED_WORDING not in si.lower()


@pytest.mark.parametrize("inp_text", [_UNCONSTRAINED_INP, _CONSTRAINED_INP])
@pytest.mark.parametrize(
    ("freqs", "reason", "science_status", "imaginary"),
    [
        # No final frequency section: the evidence is unavailable.
        ((), "frequency_evidence_missing", "unknown", None),
        # A final frequency section that contradicts a TS: two imaginary modes.
        ((-512.3, -300.0, 120.0), "ts_criteria_failed", "failed", 2),
    ],
    ids=["frequencies-unavailable", "two-imaginary-modes"],
)
def test_failed_or_unavailable_ts_evidence_supports_no_stationary_claim(
    tmp_path: Path,
    inp_text: str,
    freqs: tuple[float, ...],
    reason: str,
    science_status: str,
    imaginary: int | None,
) -> None:
    constrained = inp_text == _CONSTRAINED_INP
    published = _publish(
        tmp_path,
        "failed_optts",
        inp_text=inp_text,
        out_text=si_out_text(route=_ROUTE, freqs=freqs, thermo=bool(freqs)),
        status="failed",
        reason=reason,
    )

    science = published["science"]
    assert published["machine"]["lifecycle"]["outcome"] == "failed"
    assert published["machine"]["handoff"]["status"] == "blocked"
    assert science["status"] == science_status
    assert science["reason"] == reason
    assert science["imaginary_frequency_count"] == imaginary
    assert science["frequencies_available"] is (imaginary is not None)
    assert science["geometry_scope"] == ("partial" if constrained else "transition_state")
    assert science["stationary_point"] == "unverified"

    # No SI record for a run that did not complete.
    assert "si_block" not in published["reports"]
    assert published["si"] is None

    # The diagnostic HTML remains a TS search report but asserts nothing
    # stronger than the evidence: no "as expected" for a failed count or a
    # missing section, and no saddle claim beyond the constrained warning.
    _assert_ts_operation_identity(published)
    html = published["html"]
    assert "as expected for a TS" not in html
    assert "first_order_saddle" not in html
    if constrained:
        assert _CONSTRAINED_WORDING in html.lower()
        assert _UNVERIFIED_SADDLE_WORDING in html.lower()
    else:
        assert _CONSTRAINED_WORDING not in html.lower()
    if imaginary is not None and not constrained:
        # The unconstrained count is still compared with the TS criterion.
        assert "expected 1 for a TS" in html
