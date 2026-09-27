"""Golden report bytes for every report kind.

Each case writes a synthetic ORCA input (``tests/orca_output_helpers.py``),
binds it into an execution generation through the submission snapshot, writes
the synthetic output into that generation and publishes through
``write_report_files``. ``job_report.html`` and ``si_block.md`` are compared as
normalized text, ``machine.json`` and ``execution_provenance.json`` as key
trees, all against ``golden/reports/<case>/``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.artifacts import (
    EXECUTION_PROVENANCE_FILE,
    RUN_REPORT_HTML_FILE,
    RUN_REPORT_JSON_FILE,
    SI_BLOCK_MD_FILE,
)
from orca_auto.orca.execution_binding import (
    orca_execution_provenance,
)
from orca_auto.orca.report.publication import write_report_files
from tests.conftest import build_submitted_snapshot, write_fake_orca
from tests.contracts.normalize import Normalizer, assert_golden, key_tree, read_json
from tests.orca_output_helpers import (
    FREQ_TS_BLOCK,
    si_out_text,
    sp_out_text,
    write_irc_inp,
    write_irc_out,
    write_neb_inp,
    write_neb_out,
    write_opt_inp,
    write_opt_out,
    write_scan_inp,
    write_scan_out,
)

_HOO_XYZ = "3\n\nH 0.0 0.0 0.0\nO 1.2 0.0 0.0\nO 3.0 0.0 0.0\n"


@dataclasses.dataclass(frozen=True)
class _Case:
    write_inp: Callable[[Path], object]
    write_out: Callable[[Path], object]
    reason: str
    xyz_files: tuple[str, ...] = ()


def _write_text(text: str) -> Callable[[Path], object]:
    return lambda path: path.write_text(text, encoding="utf-8")


_CASES = {
    "opt": _Case(
        lambda path: write_opt_inp(path, "! Opt B3LYP def2-SVP"),
        write_opt_out,
        "normal_termination",
        ("input.xyz",),
    ),
    "optts": _Case(
        lambda path: write_opt_inp(path, "! OptTS B3LYP def2-SVP Freq"),
        lambda path: write_opt_out(path, freq_block=FREQ_TS_BLOCK),
        "ts_criteria_met",
        ("input.xyz",),
    ),
    "partial_opt": _Case(
        lambda path: write_opt_inp(path, "! MECP-Opt Freq B3LYP def2-SVP"),
        lambda path: write_opt_out(path, freq_block=FREQ_TS_BLOCK),
        "normal_termination",
        ("input.xyz",),
    ),
    "sp": _Case(
        _write_text("! wB97M-V def2-TZVPP CPCM(toluene)\n* xyz 0 1\nC 0 0 0\n*\n"),
        _write_text(sp_out_text()),
        "normal_termination",
    ),
    "freq": _Case(
        _write_text("! B3LYP def2-SVP Freq\n* xyz 0 1\nC 0 0 0\n*\n"),
        _write_text(sp_out_text(route="B3LYP def2-SVP Freq", freq_block=True, thermo=True)),
        "normal_termination",
    ),
    "optts_thermo": _Case(
        _write_text("! wB97X-D3 def2-TZVP CPCM(toluene) OptTS Freq\n* xyz 0 1\nC 0 0 0\n*\n"),
        _write_text(si_out_text(freqs=(-512.3, 120.0), thermo=True)),
        "ts_criteria_met",
    ),
    "relaxed_scan": _Case(write_scan_inp, write_scan_out, "ts_criteria_met", ("input.xyz",)),
    "neb_ts": _Case(
        write_neb_inp, write_neb_out, "ts_criteria_met", ("reactant.xyz", "product.xyz")
    ),
    "irc": _Case(
        lambda path: write_irc_inp(path, "! B3LYP def2-SVP IRC"),
        lambda path: write_irc_out(path, route="! B3LYP def2-SVP IRC"),
        "normal_termination",
    ),
}


def _completed_state(
    job_dir: Path, selected: Path, out_path: Path, reason: str, provenance: dict[str, Any]
) -> dict[str, Any]:
    return {
        "job_id": f"job_{job_dir.name}",
        "run_id": f"run_{job_dir.name}",
        "reaction_dir": str(job_dir),
        "selected_inp": str(selected),
        "status": "completed",
        "started_at": "2026-07-03T01:00:00+00:00",
        "updated_at": "2026-07-03T04:00:00+00:00",
        "attempts": [
            {
                "index": 1,
                "inp_path": str(selected),
                "out_path": str(out_path),
                "return_code": 0,
                "analyzer_status": "completed",
                "analyzer_reason": reason,
                "markers": {},
                "patch_actions": [],
                "started_at": "2026-07-03T01:00:00+00:00",
                "ended_at": "2026-07-03T03:48:00+00:00",
            }
        ],
        "final_result": {
            "status": "completed",
            "analyzer_status": "completed",
            "reason": reason,
            "completed_at": "2026-07-03T03:48:30+00:00",
            "last_out_path": str(out_path),
        },
        "execution_provenance": provenance,
    }


@pytest.mark.parametrize("name", list(_CASES))
def test_report_outputs(tmp_path: Path, name: str) -> None:
    case = _CASES[name]
    job_dir = tmp_path / name
    job_dir.mkdir()
    source = job_dir / "rxn.inp"
    case.write_inp(source)
    for xyz in case.xyz_files:
        (job_dir / xyz).write_text(_HOO_XYZ, encoding="utf-8")
    snapshot = build_submitted_snapshot(
        job_dir,
        source,
        selected_input_xyz="",
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        orca_executable=write_fake_orca(tmp_path / "orca"),
    )
    generation = Path(snapshot["execution_dir"])
    selected = Path(snapshot["selected_inp"])
    out_path = generation / "rxn.out"
    case.write_out(out_path)
    state = _completed_state(
        job_dir, selected, out_path, case.reason, orca_execution_provenance(snapshot)
    )

    reports = write_report_files(job_dir, state)

    n = Normalizer({tmp_path: "<tmp>"})
    assert_golden(f"reports/{name}/reports.json", n(reports))
    for filename in (RUN_REPORT_HTML_FILE, SI_BLOCK_MD_FILE):
        path = generation / filename
        if path.exists():
            assert_golden(f"reports/{name}/{filename}", n.text(path.read_text(encoding="utf-8")))
    for filename in (RUN_REPORT_JSON_FILE, EXECUTION_PROVENANCE_FILE):
        stem = filename.removesuffix(".json")
        assert_golden(
            f"reports/{name}/{stem}_keys.json", key_tree(read_json(generation / filename))
        )
