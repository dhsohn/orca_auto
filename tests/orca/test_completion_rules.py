from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.completion_rules import CompletionMode, detect_completion_mode, route_facts


def _detect(tmp_path: Path, text: str) -> CompletionMode:
    inp = tmp_path / "rxn.inp"
    inp.write_text(text, encoding="utf-8")
    return detect_completion_mode(inp)


def test_detect_ts_and_irc(tmp_path: Path) -> None:
    mode = _detect(tmp_path, "! OptTS Freq IRC\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    assert mode.kind == "ts"
    assert mode.require_irc


def test_bare_ts_token_is_not_a_ts_route(tmp_path: Path) -> None:
    # ORCA has no `! TS` keyword; treating one as a TS search would demand
    # Nimag == 1 from jobs that never ran a TS optimization.
    mode = _detect(tmp_path, "! TS Freq B3LYP def2-SVP\n* xyzfile 0 1 input.xyz\n")
    assert mode.kind == "sp"
    assert not mode.require_irc


def test_route_comment_never_reclassifies_the_job(tmp_path: Path) -> None:
    # `#` comments are cut before keyword matching: a plain Opt+Freq
    # minimum with a "TS candidate" note must stay in opt mode, or its
    # Nimag = 0 result would be failed as TS_NOT_FOUND.
    mode = _detect(
        tmp_path,
        "! B3LYP def2-SVP Opt Freq  # TS candidate from scan, IRC later\n"
        "* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
    )
    assert mode.kind == "opt"
    assert not mode.require_irc


def test_detect_opt_without_irc(tmp_path: Path) -> None:
    mode = _detect(tmp_path, "! Opt Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    assert mode.kind == "opt"
    assert not mode.require_irc


def test_detect_completion_mode_skips_blank_and_comment_lines(tmp_path: Path) -> None:
    mode = _detect(tmp_path, "\n\n# comment line\n   \n! NEB-TS IRC TightSCF\n* xyz 0 1\n")
    assert mode.kind == "ts"
    assert mode.require_irc


def test_detect_completion_mode_recognizes_standalone_irc(tmp_path: Path) -> None:
    mode = _detect(tmp_path, "! SP IRC\n* xyz 0 1\n")
    assert mode.kind == "sp"
    assert mode.require_irc


def test_detect_completion_mode_without_a_route_line_is_plain_sp(tmp_path: Path) -> None:
    mode = _detect(tmp_path, "\n# comment only\n* xyz 0 1\nH 0 0 0\n")
    assert mode.kind == "sp"
    assert not mode.require_irc


def test_detect_completion_mode_skips_blank_and_comment_lines_before_route(tmp_path: Path) -> None:
    inp = tmp_path / "rxn.inp"
    inp.write_text(
        "\n# comment\n   \n! NEB-TS IRC\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )

    mode = detect_completion_mode(inp)

    assert mode.kind == "ts"
    assert mode.require_irc is True


def test_detect_completion_mode_scans_ts_irc_keywords_on_later_route_lines(
    tmp_path: Path,
) -> None:
    # ORCA accepts a route split across multiple ! lines, with % blocks between
    # them. A TS/IRC keyword on a later route line must still be detected; reading
    # only the first ! line would misclassify this as Opt mode.
    inp = tmp_path / "rxn.inp"
    inp.write_text(
        "! B3LYP def2-SVP\n%maxcore 2000\n! OptTS Freq IRC\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )

    mode = detect_completion_mode(inp)

    assert mode.kind == "ts"
    assert mode.require_irc is True


def test_detect_completion_mode_defaults_to_sp_when_no_route_line_is_present(
    tmp_path: Path,
) -> None:
    inp = tmp_path / "rxn.inp"
    inp.write_text(
        "\n# comment only\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )

    mode = detect_completion_mode(inp)

    assert mode.kind == "sp"
    assert mode.require_irc is False


_SCAN = "%geom\n  Scan\n    B 0 1 = 1.0, 2.0, 5\n  end\nend\n"
_FLAGS = (
    "is_ts",
    "is_irc",
    "is_neb_ts",
    "is_opt",
    "is_full_opt",
    "is_relaxed_scan",
    "is_non_stationary",
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("! Opt Freq\n", {"is_opt", "is_full_opt"}),
        ("! MECP-Opt\n", {"is_opt"}),
        ("! OptTS Freq\n! IRC\n", {"is_ts", "is_irc"}),
        ("! ZOOM-NEB-TS Freq\n", {"is_ts", "is_neb_ts"}),
        ("! NEB-CI\n", {"is_non_stationary"}),
        ("! MD\n", {"is_non_stationary"}),
        ("! Opt\n" + _SCAN, {"is_opt", "is_full_opt", "is_relaxed_scan"}),
        # A scan block without an optimization is no relaxed scan, and the
        # SCAN functional in a route line is no scan block.
        ("! SP\n" + _SCAN, set()),
        ("! SCAN def2-SVP Opt\n", {"is_opt", "is_full_opt"}),
        ("# no route line\n", set()),
    ],
)
def test_route_facts_classify_every_route_line_and_the_scan_block(
    tmp_path: Path, text: str, expected: set[str]
) -> None:
    inp = tmp_path / "rxn.inp"
    inp.write_text(text + "* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")

    facts = route_facts(inp)

    assert {flag for flag in _FLAGS if getattr(facts, flag)} == expected
    assert facts.route_lines == tuple(line for line in text.splitlines() if line.startswith("!"))


def test_route_facts_of_an_unreadable_input_have_no_route(tmp_path: Path) -> None:
    facts = route_facts(tmp_path / "missing.inp")

    assert facts.route_lines == ()
    assert not any(getattr(facts, flag) for flag in _FLAGS)


@pytest.mark.parametrize(
    ("directives", "full"),
    [
        ("%geom Constraints { B 0 1 C } end end", False),
        ("%geom\n Constraints\n { C 0 C }\n end\nend", False),
        ("%geom optimizehydrogens true end", False),
        ("%geom OptimizeHydrogens = TRUE end", False),
        ("%geom freezehydrogens true end", False),
        ("! RigidBodyOpt", False),
        ("%geom ConstrainFragments { 1 } end end", False),
        ("%geom FixFrags { 1 } end end", False),
        ("%geom RigidFrags { 1 } end end", False),
        ("%geom RelaxHFrags { 1 } end end", False),
        (
            "%geom modify_internal { B 0 1 A } end\n Constraints { B 0 1 C } end end",
            False,
        ),
        ("%geom optimizehydrogens false end", True),
        ("%geom optimizehydrogens true optimizehydrogens false end", True),
        ("%geom Constraints end end", True),
        ("# %geom Constraints { B 0 1 C } end end", True),
        ("%geom # optimizehydrogens true # MaxIter 10 end", True),
        ('%scf MOInp "optimizehydrogens" end', True),
    ],
)
def test_geometry_restrictions_do_not_claim_full_optimization(
    tmp_path: Path, directives: str, full: bool
) -> None:
    from orca_auto.orca.evidence import structure_kind
    from orca_auto.orca.report.composer import collect_html_report_parts

    inp = tmp_path / "job.inp"
    inp.write_text(
        f"! HF STO-3G Opt Freq\n{directives}\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )
    facts = route_facts(inp)
    assert facts.is_opt
    assert facts.is_full_opt is full
    assert structure_kind(facts) == ("min" if full else "sp")
    parts = collect_html_report_parts(
        tmp_path, {"selected_inp": str(inp), "status": "completed", "attempts": []}
    )
    assert parts is not None and parts.opt is not None
    assert parts.opt.kind == ("opt" if full else "partial")


@pytest.mark.parametrize(
    ("route", "kind", "frequency"),
    [
        ("SP", "sp", False),
        ("Opt", "opt", False),
        ("Opt NumFreq", "opt", True),
        ("AnFreq", "sp", True),
        ("OptTS", "ts", True),
        ("IRC", "sp", False),
        ("NEB-CI", "sp", False),
        ("MD", "sp", False),
    ],
)
def test_completion_requirements_follow_requested_operations(
    tmp_path: Path, route: str, kind: str, frequency: bool
) -> None:
    mode = _detect(tmp_path, f"! HF STO-3G {route}\n* xyz 0 1\nH 0 0 0\n*\n")
    assert mode.kind == kind
    assert mode.require_frequency is frequency
