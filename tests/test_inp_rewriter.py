from __future__ import annotations

import os
from pathlib import Path

import pytest

from orca_auto.orca.inp_rewriter import (
    _latest_geometry_file,
    ensure_submission_resource_request,
    prepare_checkpoint_restart_input,
    prepare_submission_resource_request,
    read_resource_request_from_input,
)
from orca_auto.orca.input_blocks import (
    find_block_range,
    find_geometry_block,
    find_geometry_start,
    geometry_range,
    iter_blocks,
    replace_geometry_with_xyzfile,
    set_block_key_value,
)
from orca_auto.orca.input_references import set_moinp
from orca_auto.orca.input_syntax import ensure_route_keywords
from orca_auto.orca.input_validation import validate_unambiguous_orca_directives
from orca_auto.orca.resource_directives import read_maxcore, read_nprocs

BASE_INP = """! OptTS Freq IRC

%pal
  nprocs 8
end

* xyz 0 1
H 0 0 0
H 0 0 0.74
*
"""


def _write_inp(tmp_path: Path, text: str) -> Path:
    inp = tmp_path / "rxn.inp"
    inp.write_text(text, encoding="utf-8")
    return inp


def test_prepare_submission_resource_request_rejects_invalid_utf8(tmp_path: Path) -> None:
    inp = tmp_path / "rxn.inp"
    payload = b"! Opt\n%pal nprocs 2 end\n%maxcore 1024\n\xff\n"
    inp.write_bytes(payload)

    with pytest.raises(ValueError, match="UTF-8"):
        prepare_submission_resource_request(inp, default_max_cores=2, default_max_memory_gb=2)

    assert inp.read_bytes() == payload


def test_ensure_submission_resource_request_injects_missing_directives(tmp_path: Path) -> None:
    inp = _write_inp(tmp_path, "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")

    resource_request, actions = ensure_submission_resource_request(
        inp, default_max_cores=8, default_max_memory_gb=32
    )
    text = inp.read_text(encoding="utf-8")

    assert resource_request == {"max_cores": 8, "max_memory_gb": 32}
    assert actions == ["pal_nprocs_injected", "maxcore_injected"]
    assert "%pal" in text
    assert "nprocs 8" in text
    assert "%maxcore 4096" in text


def test_ensure_submission_resource_request_preserves_existing_nprocs(tmp_path: Path) -> None:
    inp = _write_inp(tmp_path, "! Opt\n%pal\n  nprocs 12\nend\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")

    resource_request, actions = ensure_submission_resource_request(
        inp, default_max_cores=8, default_max_memory_gb=32
    )
    text = inp.read_text(encoding="utf-8")

    assert resource_request == {"max_cores": 12, "max_memory_gb": 32}
    assert actions == ["maxcore_injected"]
    assert "nprocs 12" in text
    assert "%maxcore 2730" in text


def test_ensure_submission_resource_request_honors_pal_route_shorthand(tmp_path: Path) -> None:
    # "! Opt PAL4" already requests 4 processes via ORCA's route shorthand, so
    # no conflicting %pal nprocs block should be injected and the resource
    # request must reflect 4 cores (not the default_max_cores).
    inp = _write_inp(tmp_path, "! Opt PAL4\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")

    resource_request, actions = ensure_submission_resource_request(
        inp, default_max_cores=8, default_max_memory_gb=32
    )
    text = inp.read_text(encoding="utf-8")

    assert resource_request["max_cores"] == 4
    assert "pal_nprocs_injected" not in actions
    assert "%pal" not in text


def test_read_resource_request_from_input_uses_inp_values(tmp_path: Path) -> None:
    inp = _write_inp(
        tmp_path,
        "! Opt\n%pal\n  nprocs 6\nend\n%maxcore 3072\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
    )

    assert read_resource_request_from_input(inp) == {"max_cores": 6, "max_memory_gb": 18}


def test_prepare_checkpoint_restart_input_keeps_original_input_untouched(tmp_path: Path) -> None:
    src = _write_inp(tmp_path, BASE_INP)
    dst = tmp_path / "rxn.resume.inp"
    original = src.read_text(encoding="utf-8")
    (tmp_path / "rxn.gbw").write_bytes(b"checkpoint")
    (tmp_path / "rxn.xyz").write_text("2\n\nH 0 0 0\nH 0 0 0.75\n", encoding="utf-8")

    prepared, actions = prepare_checkpoint_restart_input(src, dst, tmp_path)
    out = dst.read_text(encoding="utf-8")

    assert prepared == dst
    assert src.read_text(encoding="utf-8") == original
    assert "checkpoint_restart_from_rxn.gbw" in actions
    assert "route_add_moread" in actions
    assert "moinp_set" in actions
    assert "geometry_restart_from_rxn.xyz" in actions
    assert '%moinp "rxn.gbw"' in out
    assert "* xyzfile 0 1 rxn.xyz" in out


def test_prepare_checkpoint_restart_skips_a_zero_filled_checkpoint(tmp_path: Path) -> None:
    src = _write_inp(tmp_path, BASE_INP)
    dst = tmp_path / "rxn.resume.inp"
    # A crash mid-write leaves the checkpoint's blocks unflushed and
    # read back as zeros; seeding it would fail the restarted run.
    (tmp_path / "rxn.gbw").write_bytes(b"\x00" * 4096)
    (tmp_path / "rxn.xyz").write_text("2\n\nH 0 0 0\nH 0 0 0.75\n", encoding="utf-8")

    prepared, actions = prepare_checkpoint_restart_input(src, dst, tmp_path)

    # A torn checkpoint is treated like an absent one: no checkpoint
    # restart input is prepared, so nothing seeds MORead from zeros.
    assert prepared is None
    assert actions == []
    assert not dst.exists()


def test_prepare_checkpoint_restart_falls_back_to_latest_geometry(tmp_path: Path) -> None:
    src = _write_inp(tmp_path, BASE_INP)
    dst = tmp_path / "rxn.resume.inp"
    (tmp_path / "rxn.gbw").write_bytes(b"checkpoint")
    older = tmp_path / "older.xyz"
    latest = tmp_path / "latest_trj.xyz"
    older.write_text("2\n\nH 0 0 0\nH 0 0 0.7\n", encoding="utf-8")
    latest.write_text("2\n\nH 0 0 0\nH 0 0 1.0\n", encoding="utf-8")
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(latest, ns=(2_000_000_000, 2_000_000_000))

    prepared, actions = prepare_checkpoint_restart_input(src, dst, tmp_path)
    out = dst.read_text(encoding="utf-8")

    assert prepared == dst
    assert "no_previous_xyz_file_found" in actions
    assert "geometry_restart_from_latest_trj.xyz" in actions
    assert "* xyzfile 0 1 latest_trj.xyz" in out


def test_latest_geometry_file_breaks_an_exact_mtime_tie_by_name(tmp_path: Path) -> None:
    # Every geometry carries the same nanosecond, so only the name rule can
    # decide. Draining the directory one pick at a time turns a helper that
    # returns a single path into an assertion about the whole ordering, so
    # readdir order matching the name rule by accident would need all eight
    # names to land in reverse-alphabetical order; both creation orders
    # must agree.
    stamp = (1_700_000_000_000_000_000, 1_700_000_000_000_000_000)
    names = ["c.xyz", "h.xyz", "a.xyz", "m.xyz", "b.xyz", "z.xyz", "e.xyz", "q.xyz"]
    for label, order in (("forward", names), ("reverse", list(reversed(names)))):
        root = tmp_path / label
        root.mkdir()
        for name in order:
            path = root / name
            path.write_text("2\n\nH 0 0 0\nH 0 0 0.7\n", encoding="utf-8")
            os.utime(path, ns=stamp)

        # Repeating the call on an unchanged directory must repeat the
        # answer before anything is removed.
        first = _latest_geometry_file(root)
        assert first is not None
        assert first == _latest_geometry_file(root)

        picks: list[str] = []
        while (pick := _latest_geometry_file(root)) is not None:
            picks.append(pick.name)
            pick.unlink()

        assert picks == sorted(names, reverse=True)


def test_prepare_checkpoint_restart_marks_missing_geometry(tmp_path: Path) -> None:
    src = _write_inp(tmp_path, BASE_INP)
    dst = tmp_path / "rxn.resume.inp"
    (tmp_path / "rxn.gbw").write_bytes(b"checkpoint")

    prepared, actions = prepare_checkpoint_restart_input(src, dst, tmp_path)

    assert prepared == dst
    assert "no_previous_xyz_file_found" in actions
    assert "no_geometry_file_found" in actions


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


def test_resource_readers_use_maximum() -> None:
    lines = [
        "%maxcore 1000",
        "# hidden # %maxcore 999999",
        "! SP PAL4 PAL8",
        "* xyz 0 1",
        "H 0 0 0",
        "*",
    ]

    assert read_maxcore(lines) == 999999
    assert read_nprocs(lines) == 8


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
    assert geometry_range(lines) == (1, 7, 0, 1)
    assert replace_geometry_with_xyzfile(lines, Path("/tmp/a.xyz"), Path("/tmp"))
    assert lines == ["! Freq", "* xyzfile 0 1 a.xyz", "%maxcore 512"]

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
