"""``input_blocks.ParsedInput``: one input version, tokenized once and shared.

Each reader of one input version reads the same parse; a rewritten input is a
new version and is parsed again. The tokenizer budget below pins that a
top-level reader tokenizes each line once per version it reads.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca import input_blocks, input_syntax
from orca_auto.orca.input_blocks import OrcaBlock, ParsedInput, parse_input
from orca_auto.orca.input_references import orca_input_requests_moread, scan_orca_file_references
from orca_auto.orca.input_validation import validate_supported_xyz_geometry_syntax
from orca_auto.orca.operation_facts import parse_operation_facts
from orca_auto.orca.queue_detail import queue_detail_kind
from orca_auto.orca.resource_directives import prepare_submission_resource_request

_BLOCK_INPUTS = {
    "inline-closed": "! HF\n%pal nprocs 2 end\n%maxcore 500\n* xyz 0 1\nH 0 0 0\n*\n",
    "multi-line": "! HF Opt\n%geom\n  MaxIter 50\nend\n%freq\n AnFreq true\nend\n",
    "nested": "! HF Opt\n%geom Constraints {B 0 1 C} end\n Scan B 0 1 = 0.7, 0.9, 3 end\nend\n",
    "cut-by-directive": "! HF\n%geom\n MaxIter 50\n%pal nprocs 2 end\n",
    "cut-by-geometry": "! HF\n%method RunTyp SP\n* xyz 0 1\nH 0 0 0\n*\n",
    "repeated-name": "%geom MaxIter 1 end\n! HF Opt\n%geom MaxIter 2 end\n%GEOM TS_search EF end\n",
    "spaced-header": "! HF\n% pal nprocs 2 end\n%  method RunTyp Energy end\n",
    "comments": "! HF # Opt\n%geom # Scan # MaxIter 3 end\n# %freq AnFreq true end\n",
    "unterminated-at-eof": "! HF\n%freq AnFreq true\n",
    "quoted-end": '! HF\n%geom MaxIter "end" 3\nend\n',
}


def _blocks_walked_per_name(parsed: ParsedInput, name: str) -> Iterator[OrcaBlock]:
    """The pre-``ParsedInput`` rule: one walk over the input for each block name."""
    index = 0
    while index < len(parsed.tokens):
        header = input_blocks.percent_directive_header(parsed.tokens[index])
        if header is None or header[0] != name:
            index += 1
            continue
        block = input_blocks._scan_block(
            parsed.tokens, name=name, start=index, body_start=header[1]
        )
        yield block
        index = block.end + 1 if block.closed else max(block.end, index + 1)


@pytest.mark.parametrize("case", sorted(_BLOCK_INPUTS))
def test_one_pass_finds_the_blocks_of_a_walk_per_name(case: str) -> None:
    parsed = parse_input(_BLOCK_INPUTS[case].splitlines())
    names = {block.name for block in parsed.blocks} | {"geom", "freq", "method", "pal", "neb"}

    for name in names:
        assert parsed.blocks_named(name) == tuple(_blocks_walked_per_name(parsed, name)), name


def test_parse_input_keeps_a_parse_and_reads_each_structure_once() -> None:
    parsed = parse_input(_BLOCK_INPUTS["inline-closed"].splitlines())

    assert parse_input(parsed) is parsed
    assert parsed.blocks is parsed.blocks
    assert parsed.route_lines == ("! HF",)
    assert parsed.geometry is not None
    assert parsed.geometry.atom_rows == ((4, "H 0 0 0"),)


def _counting_tokenizer(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count every ``orca_line_tokens`` call, whichever module imported it."""
    real: Callable[..., Any] = input_syntax.orca_line_tokens
    calls = [0]

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        return real(*args, **kwargs)

    for name, module in list(sys.modules.items()):
        if name.startswith("orca_auto") and getattr(module, "orca_line_tokens", None) is real:
            monkeypatch.setattr(module, "orca_line_tokens", counted)
    return calls


def _input(atoms: int) -> str:
    rows = "\n".join(f"H 0 0 {index * 0.74:.2f}" for index in range(atoms))
    return f"! HF STO-3G Opt Freq\n%pal nprocs 2 end\n%maxcore 500\n* xyz 0 1\n{rows}\n*\n"


@pytest.mark.parametrize("atoms", [2, 200])
def test_each_reader_tokenizes_each_line_once_per_version(
    monkeypatch: pytest.MonkeyPatch, atoms: int
) -> None:
    text = _input(atoms)
    lines = text.splitlines()
    calls = _counting_tokenizer(monkeypatch)

    def tokenized(read: Callable[[], object]) -> int:
        calls[0] = 0
        read()
        return calls[0]

    # One version each.
    assert tokenized(lambda: parse_operation_facts(lines, inp_path=Path("job.inp"))) == len(lines)
    assert tokenized(
        lambda: prepare_submission_resource_request(
            Path("job.inp"), text.encode(), default_max_cores=1, default_max_memory_gb=1
        )
    ) == len(lines)

    # The binding's validation, operation facts and reference scans share one parse.
    def bind_checks() -> None:
        parsed = parse_input(lines)
        validate_supported_xyz_geometry_syntax(parsed, label="job")
        parse_operation_facts(parsed, inp_path=Path("job.inp"))
        orca_input_requests_moread(parsed)
        scan_orca_file_references(parsed)

    assert tokenized(bind_checks) == len(lines)
    # The Detail reads the source and its presentation version.
    assert tokenized(lambda: queue_detail_kind(Path("job.inp"), lines)) == 2 * len(lines)


def test_submission_reads_its_input_version_through_one_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The job type, Detail, geometry path, molecule key and resource request
    # of a submission all read one parse; the Detail adds its presentation
    # version. Before, each reader tokenized the input again.
    from types import SimpleNamespace

    from orca_auto.orca import submission
    from tests.conftest import make_app_cfg

    text = _input(200)
    selected = tmp_path / "job.inp"
    selected.write_text(text, encoding="utf-8")
    monkeypatch.setattr(submission, "assert_run_dir_publication_allowed", lambda _label: None)
    calls = _counting_tokenizer(monkeypatch)

    prepared = submission._prepare_submission_inputs(
        make_app_cfg(tmp_path), SimpleNamespace(priority=None, force=False), selected
    )

    assert prepared.molecule.key == "H200"
    assert prepared.prepared_input.resource_request == {"max_cores": 2, "max_memory_gb": 1}
    assert calls[0] == 2 * len(text.splitlines())


def test_the_resource_step_parses_afresh_when_the_parse_is_of_other_lines() -> None:
    text = _input(2)
    other = parse_input("! HF\n%pal nprocs 8 end\n%maxcore 4000\n".splitlines())

    prepared = prepare_submission_resource_request(
        Path("job.inp"), text.encode(), default_max_cores=1, default_max_memory_gb=1, parsed=other
    )

    # The 8-core parse of other lines is not reused for this input's 2 cores.
    assert prepared.resource_request == {"max_cores": 2, "max_memory_gb": 1}
