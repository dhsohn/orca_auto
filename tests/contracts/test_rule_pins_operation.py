"""Rule pin: what each consumer decides about the operation an input requests.

One row per input. Each column is a separate consumer's answer for the same
lines: whether submission admits it, the completion mode and geometry scope
completion verifies, the outputs the execution binding reserves, the queue
Detail kind and the coarse ``job_type``. The consumers read the input through
one shared reader of the operation facts, so a change to that reader that moves
any consumer shows up here as a table diff. The table is
``pins/operation_consumers.json``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.orca.completion_rules import completion_mode, geometry_scope
from orca_auto.orca.execution_binding._inputs import _runtime_outputs
from orca_auto.orca.input_syntax import orca_route_lines
from orca_auto.orca.job_type import job_type_from_routes
from orca_auto.orca.operation_facts import parse_operation_facts
from orca_auto.orca.queue_detail import queue_detail_kind
from orca_auto.orca.route_rules import route_facts
from tests.conftest import build_submitted_snapshot, write_fake_orca
from tests.contracts.normalize import assert_pin

_H2 = "* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
# Route and block settings ahead of one inline H2 geometry.
SCENARIOS = {
    "sp-method-only": "! HF STO-3G",
    "sp-keyword": "! HF STO-3G SP",
    "commented-opt": "! HF STO-3G # Opt",
    "freq-route": "! HF STO-3G Freq",
    "freq-block": "! HF STO-3G\n%freq AnFreq true end",
    "freq-block-false": "! HF STO-3G\n%freq NumFreq false end",
    "opt": "! HF STO-3G Opt",
    "opt-freq": "! HF STO-3G Opt Freq",
    "tightopt-second-route-line": "! HF STO-3G\n! TightOpt",
    "opth-freq": "! HF STO-3G OptH Freq",
    "opt-constrained": "! HF STO-3G Opt\n%geom Constraints {B 0 1 C} end end",
    "relaxed-scan": "! HF STO-3G Opt\n%geom Scan B 0 1 = 0.7, 0.9, 3 end end",
    "optts": "! HF STO-3G OptTS",
    "optts-freq": "! HF STO-3G OptTS Freq",
    "optts-ts-search": "! HF STO-3G OptTS Freq\n%geom TS_search EF end",
    "opt-ts-search": "! HF STO-3G Opt Freq\n%geom TS_search EF end",
    "sp-ts-search": "! HF STO-3G\n%geom TS_search EF end",
    "irc": "! HF STO-3G IRC",
    "md": "! HF STO-3G MD",
    "engrad-route": "! HF STO-3G EnGrad",
    "runtyp-gradient": "! HF STO-3G\n%method RunTyp Gradient end",
    "runtyp-energy-sp": "! HF STO-3G SP\n%method RunTyp Energy end",
    "runtyp-energy-block-first": "%method RunTyp Energy end\n! HF STO-3G",
    "runtyp-energy-with-opt": "! HF STO-3G Opt\n%method RunTyp Energy end",
    "runtyp-opt": "! HF STO-3G\n%method RunTyp Opt end",
    "runtyp-quoted": '! HF STO-3G\n%method RunTyp "SP" end',
}


def _route_outputs(lines: list[str]) -> Any:
    """The binding's runtime outputs for ``lines``, through the shared operation facts."""
    return _runtime_outputs(parse_operation_facts(lines, inp_path=Path("job.inp")))


def _admission(tmp_path: Path, name: str, text: str) -> str:
    job_dir = tmp_path / name
    job_dir.mkdir()
    selected = job_dir / "h2.inp"
    selected.write_text(text, encoding="utf-8")
    try:
        build_submitted_snapshot(
            job_dir,
            selected,
            selected_input_xyz="",
            resource_request={"max_cores": 1, "max_memory_gb": 1},
            orca_executable=write_fake_orca(tmp_path / f"{name}-orca"),
        )
    except ValueError as exc:
        return f"rejected: {str(exc).removeprefix('ORCA selected input ')}"
    return "admitted"


def _row(tmp_path: Path, name: str, settings: str) -> dict[str, Any]:
    text = f"{settings}\n{_H2}"
    lines = text.splitlines()
    inp = tmp_path / f"{name}.inp"
    inp.write_text(text, encoding="utf-8")
    route = route_facts(inp)
    mode = completion_mode(route)
    outputs = _route_outputs(lines)
    return {
        "submission": _admission(tmp_path, name, text),
        "completion": {
            "kind": mode.kind,
            "require_frequency": mode.require_frequency,
            "require_irc": mode.require_irc,
            "geometry_scope": geometry_scope(route),
        },
        "binding_outputs": {
            "engrad": outputs.engrad_is_output,
            "hess": outputs.hessian_requested,
            "same_stem_xyz": outputs.same_stem_xyz_is_output,
            "neb": outputs.neb_requested,
            "neb_preopt_ends": outputs.neb_preopt_ends,
        },
        "detail_kind": queue_detail_kind(inp, lines),
        "job_type": job_type_from_routes(orca_route_lines(lines)),
    }


def test_operation_consumers(tmp_path: Path) -> None:
    table = {name: _row(tmp_path, name, settings) for name, settings in SCENARIOS.items()}
    assert len(table) == len(SCENARIOS)
    assert_pin("operation_consumers.json", table)
