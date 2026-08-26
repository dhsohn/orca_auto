from __future__ import annotations

import pytest

from orca_auto.flow._orca_stage_materialization import ensure_route_line, render_orca_input


@pytest.mark.parametrize("route_line", ["", "   ", "\n\n", "# only a comment"])
def test_blank_route_line_refuses_instead_of_substituting_a_level_of_theory(
    route_line: str,
) -> None:
    # A durable payload without an active route must fail its cycle rather than
    # render at whatever level of theory happens to be hard-coded here.
    with pytest.raises(ValueError, match="route_line has no active route"):
        ensure_route_line(route_line)


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
