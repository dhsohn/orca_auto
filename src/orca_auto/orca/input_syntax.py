"""Line-level ORCA input syntax: tokens, comments, route lines, and path quoting.

This is the bottom of the input-handling stack (``input_syntax`` <-
``input_blocks`` <- ``input_references`` and ``resource_directives`` <-
``input_validation``). Nothing here knows about ``%block`` structure, geometry
sections, or external file references; it turns one input line into
comment-free tokens with source spans, finds the value token after a key, and
renders what every rewriter shares: the ``!`` route line, a quoted or bare file
path, and the text of a rewritten input.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_SAFE_UNQUOTED_ORCA_PATH_RE = re.compile(r"^[A-Za-z0-9._/+\-]+$")


@dataclass(frozen=True)
class OrcaLineToken:
    value: str
    start: int
    end: int
    quoted: bool = False


def orca_line_tokens(line: str, *, start: int = 0) -> list[OrcaLineToken]:
    """Return non-comment ORCA tokens with source spans.

    ORCA permits both end-of-line ``#`` comments and ``# ... #`` inline
    comments.  Keeping spans lets callers replace path/value tokens without
    rebuilding the rest of the input line.
    """

    tokens: list[OrcaLineToken] = []
    index = max(0, int(start))
    while index < len(line):
        character = line[index]
        if character.isspace():
            index += 1
            continue
        if character == "#":
            closing = line.find("#", index + 1)
            if closing < 0:
                break
            index = closing + 1
            continue
        if character == "=":
            tokens.append(OrcaLineToken("=", index, index + 1))
            index += 1
            continue

        token_start = index
        if character in {'"', "'"}:
            quote = character
            index += 1
            value_chars: list[str] = []
            while index < len(line):
                character = line[index]
                if character == "\\" and index + 1 < len(line):
                    value_chars.append(line[index + 1])
                    index += 2
                    continue
                if character == quote:
                    index += 1
                    break
                value_chars.append(character)
                index += 1
            tokens.append(OrcaLineToken("".join(value_chars), token_start, index, quoted=True))
            continue

        while index < len(line):
            character = line[index]
            if character.isspace() or character in {"#", "="}:
                break
            index += 1
        tokens.append(OrcaLineToken(line[token_start:index], token_start, index))
    return tokens


def value_token_index(tokens: Sequence[OrcaLineToken], key_index: int) -> int:
    """Index of the value token after ``tokens[key_index]``, past one optional ``=``.

    ORCA accepts ``key value`` and ``key = value``. The result is
    ``len(tokens)`` or more when the key has no value.
    """

    value_index = key_index + 1
    if value_index < len(tokens) and tokens[value_index].value == "=":
        value_index += 1
    return value_index


def render_orca_input(lines: Sequence[str]) -> str:
    """The text of an input from its lines: trailing blank lines dropped, one final newline."""

    return "\n".join(lines).rstrip() + "\n"


def active_orca_line_text(line: str) -> str:
    """Return active ORCA tokens with closed ``# ... #`` comments removed."""

    tokens = orca_line_tokens(line)
    if not tokens:
        return ""
    prefix = line[: tokens[0].start]
    indentation = prefix if not prefix or prefix.isspace() else ""
    return indentation + " ".join(token.value for token in tokens)


def active_orca_directive_text(line: str) -> str:
    """Return canonical active text for a percent-directive line."""

    return re.sub(
        r"\A(?P<indent>\s*)%\s+(?=[A-Za-z])",
        r"\g<indent>%",
        active_orca_line_text(line),
        count=1,
    )


def orca_route_tokens(line: str) -> list[OrcaLineToken]:
    """Return tokens after the first active ORCA ``!`` route marker."""

    return _route_tokens(orca_line_tokens(line))


def _route_tokens(tokens: list[OrcaLineToken]) -> list[OrcaLineToken]:
    if not tokens:
        return []
    first = tokens[0]
    if first.value == "!":
        return tokens[1:]
    if not first.value.startswith("!"):
        return []
    compact_value = first.value[1:]
    if not compact_value:
        return tokens[1:]
    return [
        OrcaLineToken(
            compact_value,
            first.start + 1,
            first.end,
            quoted=first.quoted,
        ),
        *tokens[1:],
    ]


def orca_route_line(line: str) -> str | None:
    """Return one canonical active route line, or ``None`` for a non-route line."""

    tokens = orca_line_tokens(line)
    if not tokens or not tokens[0].value.startswith("!"):
        return None
    suffix = " ".join(token.value for token in _route_tokens(tokens))
    return f"! {suffix}".rstrip()


def find_route_idx(lines: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        if orca_route_line(line) is not None:
            return idx
    return None


def orca_route_lines(lines: list[str]) -> list[str]:
    """All route (``!``) lines of an ORCA input, stripped.

    ORCA accepts multiple route lines and allows ``%`` blocks before them, so
    callers deciding "does this input request X" must scan every route line,
    not just the first one. ``#`` comments are cut before returning: keyword
    regexes (TS/IRC/OPT/...) run on these lines, and a comment like
    ``# TS guess`` must never reclassify the job.
    """
    return [route for line in lines if (route := orca_route_line(line)) is not None]


def file_route_lines(inp_path: Path) -> list[str]:
    """:func:`orca_route_lines` of an input file; ``[]`` when unreadable."""
    try:
        lines = inp_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    return orca_route_lines(lines)


def ensure_route_keywords(lines: list[str], keywords: list[str]) -> bool:
    idx = find_route_idx(lines)
    if idx is None:
        lines.insert(0, "! " + " ".join(keywords))
        return True

    current = orca_route_line(lines[idx])
    if current is None:
        raise ValueError("ORCA route index does not identify an active route line")
    token_set = {token.value.upper() for token in orca_route_tokens(lines[idx])}
    missing = [kw for kw in keywords if kw.upper() not in token_set]
    if not missing:
        return False
    lines[idx] = current + " " + " ".join(missing)
    return True


def format_relative_or_absolute(path: Path, base_dir: Path) -> str:
    resolved = path.resolve()
    base_resolved = base_dir.resolve()
    try:
        ref = resolved.relative_to(base_resolved)
    except ValueError:
        ref = resolved
    return str(ref).replace("\\", "/")


def quote_orca_path(path_text: str) -> str:
    escaped = path_text.replace('"', '\\"')
    return f'"{escaped}"'


def is_safe_unquoted_orca_path(path_text: str) -> bool:
    return bool(path_text and _SAFE_UNQUOTED_ORCA_PATH_RE.fullmatch(path_text))


def unquoted_orca_path(path_text: str) -> str:
    if not is_safe_unquoted_orca_path(path_text):
        raise ValueError(f"Unsafe unquoted ORCA input path reference: {path_text!r}")
    return path_text
