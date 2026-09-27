from __future__ import annotations

import pytest

from orca_auto.orca.input_syntax import (
    orca_line_tokens,
    orca_route_line,
    orca_route_tokens,
    render_orca_input,
    value_token_index,
)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("nprocs 8", "8"),
        ("nprocs = 8", "8"),
        ("nprocs=8", "8"),
        ('MOInp "guess.gbw"', "guess.gbw"),
        ('MOInp = "guess.gbw"', "guess.gbw"),
    ],
)
def test_value_token_index_skips_one_optional_equals(line: str, expected: str) -> None:
    tokens = orca_line_tokens(line)
    assert tokens[value_token_index(tokens, 0)].value == expected


@pytest.mark.parametrize("line", ["nprocs", "nprocs ="])
def test_value_token_index_points_past_the_tokens_without_a_value(line: str) -> None:
    tokens = orca_line_tokens(line)
    assert value_token_index(tokens, 0) >= len(tokens)


def test_render_orca_input_ends_with_one_newline() -> None:
    assert render_orca_input(["! SP", "* xyz 0 1", "H 0 0 0", "*", "", ""]) == (
        "! SP\n* xyz 0 1\nH 0 0 0\n*\n"
    )
    assert render_orca_input([]) == "\n"


@pytest.mark.parametrize(
    ("line", "route", "values"),
    [
        ("! Opt Freq # TS guess", "! Opt Freq", ["Opt", "Freq"]),
        ("!Opt Freq", "! Opt Freq", ["Opt", "Freq"]),
        ("!", "!", []),
        ("# ! Opt", None, []),
        ("%pal nprocs 4 end", None, []),
    ],
)
def test_route_line_and_tokens_agree(line: str, route: str | None, values: list[str]) -> None:
    assert orca_route_line(line) == route
    assert [token.value for token in orca_route_tokens(line)] == values
