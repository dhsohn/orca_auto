from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.input_blocks import (
    find_block_range,
    find_geometry_block,
    find_geometry_start,
    iter_blocks,
    set_block_key_value,
)
from orca_auto.orca.input_references import set_moinp
from orca_auto.orca.input_syntax import ensure_route_keywords
from orca_auto.orca.input_validation import validate_unambiguous_orca_directives
from orca_auto.orca.resource_directives import read_maxcore, read_nprocs


def test_mutators_replace_active_directives_after_closed_comments() -> None:
    lines = [
        "# Freq is commentary # ! SP",
        "# block provenance # %pal",
        "# stale value # nprocs 999",
        "end",
        '# old checkpoint # %moinp "old.gbw"',
        '%moinp "older.gbw"',
        "* xyz 0 1",
        "H 0 0 0",
        "*",
    ]

    assert ensure_route_keywords(lines, ["Freq"])
    assert set_block_key_value(lines, "pal", "nprocs", "4")
    assert set_moinp(lines, Path("new.gbw"), Path.cwd())

    assert lines[0] == "! SP Freq"
    assert read_nprocs(lines) == 4
    assert sum(line.startswith("%pal") for line in lines) == 1
    assert '%moinp "new.gbw"' in lines
    assert sum(line.startswith("%moinp") for line in lines) == 1
    assert not any("999" in line or "old" in line for line in lines)

    inline_lines = ["# hidden # %pal nprocs 999 end", "* xyz 0 1", "H 0 0 0", "*"]
    assert set_block_key_value(inline_lines, "pal", "nprocs", "4")
    assert inline_lines[0] == "%pal nprocs 4 end"
    assert read_nprocs(inline_lines) == 4


def test_set_moinp_updates_existing_scf_declaration_without_duplicate() -> None:
    lines = [
        "! Opt MORead",
        '%scf Guess MOInp "old.gbw" end',
        "* xyz 0 1",
        "H 0 0 0",
        "*",
    ]

    assert set_moinp(lines, Path("new.gbw"), Path.cwd())

    assert 'MOInp "new.gbw"' in lines[1]
    assert not any(line.lower().startswith("%moinp") for line in lines)


def test_block_mutator_rejects_duplicate_blocks_and_keys() -> None:
    with pytest.raises(ValueError, match="duplicate %pal blocks"):
        set_block_key_value(
            ["%pal nprocs 4 end", "# hidden # %pal nprocs 999 end"], "pal", "nprocs", "4"
        )
    duplicate_inline = ["# hidden # %pal nprocs 4 nprocs 999 end"]
    assert read_nprocs(duplicate_inline) == 999
    with pytest.raises(ValueError, match="duplicate nprocs"):
        set_block_key_value(duplicate_inline, "pal", "nprocs", "4")


def test_find_block_range_does_not_mutate_lines() -> None:
    """find_block_range must not append 'end' to the shared lines list.

    Before the fix, calling find_block_range on an unclosed block would
    append 'end' to lines, corrupting subsequent block lookups. This test
    verifies repeated reads of unclosed blocks do NOT change the line count.
    """
    lines = [
        "! OptTS Freq IRC",
        "",
        "%pal",
        "  nprocs 8",
        "",
        "%scf",
        "  MaxIter 125",
        "",
        "* xyz 0 1",
        "H 0 0 0",
        "H 0 0 0.74",
        "*",
    ]
    original_len = len(lines)
    assert find_block_range(lines, "pal") is not None
    assert len(lines) == original_len

    # find_block_range for %scf should still return correct unclosed range
    rng = find_block_range(lines, "scf")
    assert rng is not None
    start, _end, needs_close = rng
    assert start == 5
    assert needs_close
    assert len(lines) == original_len


def test_geom_keys_are_inserted_outside_nested_scan_block() -> None:
    lines = [
        "! Opt B3LYP def2-SVP Freq",
        "%geom",
        "  MaxIter 200",
        "  Scan",
        "    B 4 20 = 1.86, 3.40, 32",
        "  end",
        "end",
        "* xyzfile 0 1 input.xyz",
    ]

    assert set_block_key_value(lines, "geom", "Calc_Hess", "true")

    assert lines == [
        "! Opt B3LYP def2-SVP Freq",
        "%geom",
        "  MaxIter 200",
        "  Scan",
        "    B 4 20 = 1.86, 3.40, 32",
        "  end",
        "  Calc_Hess true",
        "end",
        "* xyzfile 0 1 input.xyz",
    ]


@pytest.mark.parametrize(
    "geom",
    [
        ["%geom Constraints", "  {B 2 3 C}", "  end"],
        ["%geom", "  Constraints {B 2 3 C} end"],
        ["%geom Constraints {B 2 3 C} end"],
    ],
    ids=["header", "one-line", "header-one-line"],
)
def test_nested_sub_block_on_a_shared_row_does_not_close_the_block(geom: list[str]) -> None:
    lines = [
        *geom,
        "  Scan B 0 1 = 1.0, 2.0, 5 end",
        "  MaxIter 50",
        "end",
        "* xyz 0 1",
        "H 0 0 0",
        "*",
    ]

    assert find_block_range(lines, "geom") == (0, len(geom) + 2, False)
    block = next(iter_blocks(lines, "geom"))
    assert [row.text for row in block.rows][-2:] == ["Scan B 0 1 = 1.0, 2.0, 5 end", "MaxIter 50"]


def test_block_scanner_owns_termination_and_comment_rules() -> None:
    # Inline ``end`` closes the block on its header line; the following
    # %scf body must not leak into the %pal rows.
    inline = ["%pal nprocs 8 end", "%scf", "  MaxIter 100", "end", "* xyz 0 1", "H 0 0 0", "*"]
    assert read_nprocs(inline) == 8
    assert find_block_range(inline, "pal") == (0, 0, False)
    assert find_block_range(inline, "scf") == (1, 3, False)

    # ``end`` inside comments never closes a block; trailing comments are cut.
    commented = ["%pal", "  # end #", "  # end", "  nprocs 6 # cores", "end"]
    assert read_nprocs(commented) == 6
    assert find_block_range(commented, "pal") == (0, 4, False)

    # The geometry header and the next %directive cut an unterminated block.
    unterminated = ["%pal", "  nprocs 4", "* xyz 0 1", "H 0 0 0", "*", "%maxcore 512"]
    assert read_nprocs(unterminated) == 4
    assert read_maxcore(unterminated) == 512
    assert find_block_range(unterminated, "pal") == (0, 2, True)
    cut_by_directive = ["%pal", "  nprocs 4", "%maxcore 512"]
    assert find_block_range(cut_by_directive, "pal") == (0, 2, True)
    assert [[row.text for row in block.rows] for block in iter_blocks(cut_by_directive, "pal")] == [
        ["nprocs 4"]
    ]


def test_geometry_scanners_share_the_comment_tokenizer() -> None:
    lines = [
        "! Freq",
        "# header note # * xyz 0 1 # charge, mult",
        "# fragment A",
        "H 0 0 0 # first",
        "  # fragment B #",
        "H 0 0 0.74",
        "* # done",
        "%maxcore 512",
    ]
    block = find_geometry_block(lines)
    assert block is not None
    assert block.kind == "xyz"
    assert block.atom_rows == ((3, "H 0 0 0"), (5, "H 0 0 0.74"))
    assert block.terminator_index == 6
    assert find_geometry_start(lines) == 1

    quoted = find_geometry_block(['* xyzfile 0 1 "my mol.xyz" # reference'])
    assert quoted is not None
    assert (quoted.kind, quoted.reference) == ("xyzfile", "my mol.xyz")


def test_set_block_key_value_updates_a_body_row_that_carries_the_closing_end() -> None:
    lines = ["! Opt", "%pal", " nprocs 8 end", "* xyz 0 1", "H 0 0 0", "*"]
    assert set_block_key_value(lines, "pal", "nprocs", "4") is True
    assert lines == ["! Opt", "%pal", "  nprocs 4 end", "* xyz 0 1", "H 0 0 0", "*"]
    assert set_block_key_value(lines, "pal", "nprocs", "4") is False


def test_validate_unambiguous_directives_counts_pal_nprocs_via_the_shared_block_rule() -> None:
    def rejects(lines: list[str], fragment: str) -> None:
        with pytest.raises(ValueError, match=f"ambiguous duplicate ORCA directives: .*{fragment}"):
            validate_unambiguous_orca_directives(lines, label="job.inp")

    # Duplicate blocks and duplicate ``nprocs`` rows inside one block stay rejected,
    # whether the second copy hides behind a closed ``# ... #`` comment or not.
    rejects(["%pal nprocs 4 end", "# hidden # %pal nprocs 8 end"], "%pal blocks")
    rejects(["%pal nprocs 4 nprocs 8 end"], "%pal nprocs")
    rejects(["%pal", "  nprocs 4", "  nprocs 8", "end"], "%pal nprocs")
    rejects(["%pal", "  # end #", "  nprocs 4 # cores", "  nprocs 8", "end"], "%pal nprocs")
    rejects(["! SP PAL4", "%pal nprocs 4 end"], "mixed %pal and PAL route shorthands")
    rejects(["%pal nprocs 8 nprocs = 16 end"], "%pal nprocs")
    rejects(["%pal", "  nprocs 8", "  nprocs=16", "end"], "%pal nprocs")
    rejects(["%pal", "  nprocs = 8", "  nprocs = 16", "end"], "%pal nprocs")
    # An unterminated block is cut by the geometry section; a later %pal is a duplicate.
    rejects(["%pal", "  nprocs 4", "* xyz 0 1", "H 0 0 0", "*", "%pal nprocs 8 end"], "%pal blocks")

    # Tokens after the closing ``end`` are not %pal content (ORCA does not parse
    # them as such and ``read_nprocs`` ignores them), so they are not duplicates.
    for after_end in (
        ["%pal nprocs 4 end nprocs 8"],
        ["%pal", "  nprocs 4 end", "  nprocs 8"],
        ["%pal nprocs 4 end", "nprocs 8"],
    ):
        validate_unambiguous_orca_directives(after_end, label="job.inp")
        assert read_nprocs(after_end) == 4
    validate_unambiguous_orca_directives(["%pal", "  nprocs 4", "end", "%maxcore 512"], label="x")
