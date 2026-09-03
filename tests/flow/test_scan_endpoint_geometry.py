"""The scan endpoint geometry follows ORCA's step numbering, not the row count."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.flow.orchestration import scan_orca_materialization as som


def _scan_stage(inp: Path, out: Path) -> dict[str, Any]:
    return {
        "stage_id": "scan_forward_01",
        "task": {"payload": {"selected_inp": str(inp)}},
        "output_artifacts": [{"kind": "orca_last_out", "path": str(out)}],
    }


def test_scan_endpoint_uses_the_last_retained_row_index(tmp_path: Path) -> None:
    # Row 2 of the surface is refused (a positive energy is not a total
    # energy), so three rows are retained but the last step is 4. The
    # endpoint geometry is `.004.xyz`; counting rows would pick `.003.xyz`.
    inp = tmp_path / "scan.inp"
    inp.write_text("! ScanTS B3LYP def2-SVP\n", encoding="utf-8")
    out = tmp_path / "scan.out"
    out.write_text(
        "\n".join(
            [
                "The Calculated Surface using the 'Actual Energy'",
                "   1.86000000 -100.00000000",
                "   1.91000000 458.839",
                "   1.96000000 -99.80000000",
                "   2.01000000 -99.90000000",
                "The Calculated Surface using the SCF energy",
                "   1.86000000 -101.00000000",
            ]
        ),
        encoding="utf-8",
    )
    for step in (1, 3, 4):
        (tmp_path / f"scan.{step:03d}.xyz").write_text("1\n\nH 0 0 0\n", encoding="utf-8")

    assert som._scan_endpoint_xyz(_scan_stage(inp, out)) == tmp_path / "scan.004.xyz"


def test_scan_endpoint_without_a_surface_or_scan_spec_is_none(tmp_path: Path) -> None:
    # No table and no `scan_coordinate` metadata: nothing to number the endpoint by.
    inp = tmp_path / "scan.inp"
    inp.write_text("! ScanTS B3LYP def2-SVP\n", encoding="utf-8")
    out = tmp_path / "scan.out"
    out.write_text("no surface here\n", encoding="utf-8")
    stage = _scan_stage(inp, out)

    assert som._scan_endpoint_xyz(stage) is None
