from __future__ import annotations

import pytest

from orca_auto.orca.input_references import (
    neb_file_reference_context,
    scan_orca_file_references,
)
from orca_auto.orca.input_syntax import orca_line_tokens

_GEOMETRY = ["* xyz 0 1", "H 0 0 0", "*"]


def _references(lines: list[str]) -> list[tuple[str, str]]:
    return [(reference.kind, reference.value) for reference in scan_orca_file_references(lines)]


@pytest.mark.parametrize("separator", [" ", " = "])
def test_both_passes_read_a_directive_value_the_same_way(separator: str) -> None:
    # The value of a file key is marked in the first pass, so the second pass
    # never reads a file named like a directive as one.
    lines = ["! SP", "%geom", f"  InHessName{separator}progress.hess", "end", *_GEOMETRY]

    assert _references(lines) == [("auxiliary", "progress.hess")]


@pytest.mark.parametrize("line", ['%moinp %base "x.gbw"', '%geom InHessName %base "a.hess" end'])
def test_a_rejected_directive_used_as_a_value_marks_no_file(line: str) -> None:
    with pytest.raises(ValueError, match="Unsupported ORCA file reference"):
        scan_orca_file_references(["! SP", line, *_GEOMETRY])


def test_base_directive_is_rejected() -> None:
    with pytest.raises(ValueError, match="external program directive: %base"):
        scan_orca_file_references(["! SP", '%base "job"', *_GEOMETRY])


def test_xyzfile_geometry_counts_as_a_reference() -> None:
    assert _references(["! SP", "* xyzfile 0 1 progress.xyz"]) == [("geometry", "progress.xyz")]


@pytest.mark.parametrize(
    ("line", "in_block", "expected"),
    [
        ('%neb Product "p.xyz" end', False, ({1}, False)),
        ('% neb TS "t.xyz"', False, ({2}, True)),
        ('  Product = "p.xyz"', True, ({0}, True)),
        ("end", True, (set(), False)),
        ('%geom Product "p.xyz" end', True, (set(), False)),
        ('Product "p.xyz"', False, (set(), False)),
    ],
)
def test_neb_context_reads_the_shared_percent_header(
    line: str, in_block: bool, expected: tuple[set[int], bool]
) -> None:
    assert neb_file_reference_context(orca_line_tokens(line), in_neb_block=in_block) == expected


def test_md_filename_value_may_follow_an_equals_sign() -> None:
    lines = ["! MD", "%md", '  Dump Position Stride 1 Filename = "traj.xyz"', "end", *_GEOMETRY]
    assert _references(lines) == []
    with pytest.raises(ValueError, match="plain basename"):
        scan_orca_file_references(
            ["! MD", "%md", '  Dump Filename = "../traj.xyz"', "end", *_GEOMETRY]
        )
