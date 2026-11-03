"""``operation_facts``: the declarations an input makes about its operation."""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.operation_facts import MethodRunType, OperationFacts, parse_operation_facts

_H2 = "* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"


def _facts(settings: str) -> OperationFacts:
    return parse_operation_facts(f"{settings}\n{_H2}".splitlines(), inp_path=Path("job.inp"))


@pytest.mark.parametrize(
    ("settings", "run_types"),
    [
        ("! HF", ()),
        ("! HF\n%method RunTyp Energy end", (MethodRunType("Energy"),)),
        ("%method RunTyp = Gradient end\n! HF", (MethodRunType("Gradient"),)),
        ('! HF\n%method RunTyp "SP" end', (MethodRunType("SP", quoted=True),)),
        ("! HF\n%method RunTyp end", (MethodRunType(None),)),
        (
            "! HF\n%method RunTyp SP RunTyp Opt end",
            (MethodRunType("SP"), MethodRunType("Opt")),
        ),
        ("! HF\n%method # RunTyp Opt # end", ()),
        ("! HF\n%scf RunTyp Opt end", ()),
    ],
)
def test_method_run_types_keep_every_declaration_in_order(
    settings: str, run_types: tuple[MethodRunType, ...]
) -> None:
    assert _facts(settings).method_run_types == run_types


@pytest.mark.parametrize(
    ("settings", "declares"),
    [
        ("! HF Opt\n%geom TS_search EF end", True),
        ("! HF Opt\n%geom\n  TS_search = EF\nend", True),
        ("! HF Opt\n%geom TS_search end", True),
        ("! HF Opt\n%geom # TS_search EF # end", False),
        ('! HF Opt\n%geom "TS_search" EF end', False),
        ("! HF Opt\n%geom MaxIter 50 end TS_search EF", False),
        ("! HF Opt\n%method TS_search EF end", False),
    ],
)
def test_ts_search_is_a_geom_body_key(settings: str, declares: bool) -> None:
    assert _facts(settings).declares_ts_search is declares


@pytest.mark.parametrize(
    ("settings", "engrad", "neb", "goat"),
    [
        ("! HF", False, False, False),
        ("! HF EnGrad", True, False, False),
        ("! HF TightOpt", True, False, False),
        ("! HF RigidBodyOpt", False, False, False),
        ("! HF IRC", True, False, False),
        ("! XTB GOAT", True, False, True),
        ("! XTB NEB-TS", False, True, False),
        ("! XTB ZOOM-NEB-CI", False, True, False),
        ("! HF # EnGrad", False, False, False),
    ],
)
def test_route_keyword_facts(settings: str, engrad: bool, neb: bool, goat: bool) -> None:
    facts = _facts(settings)

    assert facts.route_writes_engrad is engrad
    assert facts.neb_route is neb
    assert facts.goat_route is goat


@pytest.mark.parametrize(
    ("neb_block", "preopt"),
    [
        ("", False),
        ("%neb PreOpt false end", False),
        ("%neb PreOpt_Ends true end", True),
        ('%neb PreOpt "false" end', True),
        ("%neb PreOpt end", True),
        ("%neb Monitor_Internals { B 0 1 } end PreOpt false end", True),
    ],
)
def test_neb_end_point_preoptimization(neb_block: str, preopt: bool) -> None:
    assert _facts(f"! XTB NEB-TS\n{neb_block}").neb_preopt_ends is preopt


def test_route_facts_are_the_completion_route_facts() -> None:
    facts = _facts("! HF STO-3G Opt Freq\n%freq AnFreq true end")

    assert facts.route.is_opt
    assert facts.route.requests_frequency
    assert facts.route.inp_path == Path("job.inp")
