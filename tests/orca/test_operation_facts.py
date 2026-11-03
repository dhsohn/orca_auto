"""``operation_facts``: the declarations an input makes about its operation."""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.execution_binding._inputs import _runtime_outputs
from orca_auto.orca.input_blocks import GRADIENT_RUN_TYPES, SINGLE_POINT_RUN_TYPES
from orca_auto.orca.input_validation import (
    validate_consistent_operation,
    validate_supported_method_run_type,
)
from orca_auto.orca.operation_facts import MethodRunType, OperationFacts, parse_operation_facts
from orca_auto.orca.queue_detail import queue_detail_kind

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


@pytest.mark.parametrize("run_type", sorted(GRADIENT_RUN_TYPES))
def test_every_gradient_run_type_is_admitted_reserved_and_named_alike(
    tmp_path: Path, run_type: str
) -> None:
    # One shared alias list: submission admits the run type, the binding
    # reserves the .engrad it writes and the Detail names the operation. A new
    # alias in GRADIENT_RUN_TYPES reaches all three.
    lines = f"! HF STO-3G\n%method RunTyp {run_type.upper()} end\n{_H2}".splitlines()
    facts = parse_operation_facts(lines, inp_path=tmp_path / "job.inp")

    validate_supported_method_run_type(facts, label="job")
    validate_consistent_operation(facts, label="job")
    assert _runtime_outputs(facts).engrad_is_output
    assert queue_detail_kind(tmp_path / "job.inp", lines) == "unsupported"


@pytest.mark.parametrize("run_type", sorted(SINGLE_POINT_RUN_TYPES))
def test_single_point_run_types_are_admitted_without_a_gradient_output(
    tmp_path: Path, run_type: str
) -> None:
    lines = f"! HF STO-3G\n%method RunTyp {run_type} end\n{_H2}".splitlines()
    facts = parse_operation_facts(lines, inp_path=tmp_path / "job.inp")

    validate_supported_method_run_type(facts, label="job")
    assert not _runtime_outputs(facts).engrad_is_output
