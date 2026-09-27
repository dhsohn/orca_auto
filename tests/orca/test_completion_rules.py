from __future__ import annotations

from pathlib import Path

from orca_auto.orca.completion_rules import CompletionMode, detect_completion_mode


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
    assert mode.kind == "opt"
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


def test_detect_completion_mode_defaults_to_opt_when_no_ts_keyword(tmp_path: Path) -> None:
    mode = _detect(tmp_path, "! SP IRC\n* xyz 0 1\n")
    assert mode.kind == "opt"
    assert mode.require_irc


def test_detect_completion_mode_without_a_route_line_is_plain_opt(tmp_path: Path) -> None:
    mode = _detect(tmp_path, "\n# comment only\n* xyz 0 1\nH 0 0 0\n")
    assert mode.kind == "opt"
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


def test_detect_completion_mode_defaults_to_opt_when_no_route_line_is_present(
    tmp_path: Path,
) -> None:
    inp = tmp_path / "rxn.inp"
    inp.write_text(
        "\n# comment only\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )

    mode = detect_completion_mode(inp)

    assert mode.kind == "opt"
    assert mode.require_irc is False
