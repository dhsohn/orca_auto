from __future__ import annotations

import pytest

from orca_auto.flow._orca_stage_materialization import render_orca_input


def test_render_orca_input_refuses_a_blank_route_line() -> None:
    with pytest.raises(ValueError, match="route_line has no active route"):
        render_orca_input(
            route_line="",
            charge=0,
            multiplicity=1,
            max_cores=1,
            max_memory_gb=1,
            xyz_filename="geom.xyz",
        )


def test_render_orca_input_keeps_the_caller_route_and_adds_no_implicit_route() -> None:
    body = render_orca_input(
        route_line="! Opt r2scan-3c TightSCF",
        charge=0,
        multiplicity=1,
        max_cores=4,
        max_memory_gb=8,
        xyz_filename="geom.xyz",
    )

    assert body.startswith("! Opt r2scan-3c TightSCF\n")
    assert body.count("!") == 1
