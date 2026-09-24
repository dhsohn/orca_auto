"""Structural units of an ORCA input: the geometry section and ``%name`` blocks.

Sits on :mod:`.input_syntax` (tokens, comments, route lines) and provides the
two primitives every input rewriter and scanner shares: locating the single
``* xyz`` / ``* xyzfile`` geometry block, and walking ``%name ... end`` blocks
under the package-wide block-termination rule of :class:`OrcaBlock`. Editing
helpers here (``set_block_key_value``, ``replace_geometry_with_xyzfile``)
change one block at a time and never look at external file references; that
is :mod:`.input_references`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .input_syntax import (
    OrcaLineToken,
    active_orca_directive_text,
    active_orca_line_text,
    orca_line_tokens,
)

GEOM_HEADER_RE = re.compile(
    r"^\s*\*\s+(xyzfile|xyz)\s+(-?\d+)\s+(\d+)(?:\s+(.*))?$",
    re.IGNORECASE,
)
BLOCK_START_RE = re.compile(r"^\s*%([A-Za-z0-9_\-]+)")
NESTED_BLOCK_NAMES = frozenset({"scan", "constraints"})


@dataclass(frozen=True)
class OrcaGeometryBlock:
    """The first ``* xyz`` / ``* xyzfile`` geometry block of an ORCA input.

    Every field is read through :func:`orca_line_tokens`, so ``#`` comments
    (closed ``# ... #`` and trailing) never count as atoms and never hide a
    header or the closing ``*``. ``atom_rows`` holds ``(line_index, active
    text)`` for each inline atom row; comment-only and blank lines are
    omitted. ``terminator_index`` is the line index of the closing ``*`` and
    ``None`` for ``xyzfile`` headers and unterminated inline blocks.
    """

    header_index: int
    kind: str  # "xyz" | "xyzfile"
    charge: int
    multiplicity: int
    reference: str | None
    atom_rows: tuple[tuple[int, str], ...]
    terminator_index: int | None


def geometry_header_match(line: str) -> re.Match[str] | None:
    """Match ``GEOM_HEADER_RE`` against the active (comment-free) text of ``line``."""

    return GEOM_HEADER_RE.match(active_orca_line_text(line))


def find_geometry_block(lines: Sequence[str]) -> OrcaGeometryBlock | None:
    """Locate the first geometry header and, for ``xyz``, its atom rows."""

    for header_index, line in enumerate(lines):
        match = geometry_header_match(line)
        if match is None:
            continue
        kind = match.group(1).lower()
        charge = int(match.group(2))
        multiplicity = int(match.group(3))
        if kind == "xyzfile":
            # Same token position as input_references' geometry reference, so a
            # quoted or comment-suffixed filename resolves identically.
            tokens = orca_line_tokens(line)
            reference = tokens[4].value if len(tokens) >= 5 else None
            return OrcaGeometryBlock(header_index, kind, charge, multiplicity, reference, (), None)
        atom_rows: list[tuple[int, str]] = []
        for index in range(header_index + 1, len(lines)):
            text = active_orca_line_text(lines[index]).strip()
            if not text:
                continue
            if text == "*":
                return OrcaGeometryBlock(
                    header_index, kind, charge, multiplicity, None, tuple(atom_rows), index
                )
            atom_rows.append((index, text))
        return OrcaGeometryBlock(
            header_index, kind, charge, multiplicity, None, tuple(atom_rows), None
        )
    return None


def find_geometry_start(lines: list[str]) -> int | None:
    block = find_geometry_block(lines)
    return None if block is None else block.header_index


def geometry_range(lines: list[str]) -> tuple[int, int, int, int] | None:
    """Return ``(start, end, charge, multiplicity)`` of the first geometry block."""

    block = find_geometry_block(lines)
    if block is None:
        return None
    if block.kind == "xyzfile":
        end = block.header_index + 1
    elif block.terminator_index is not None:
        end = block.terminator_index + 1
    else:
        end = len(lines)
    return block.header_index, end, block.charge, block.multiplicity


def replace_geometry_with_xyzfile(lines: list[str], geom_file: Path, base_dir: Path) -> bool:
    geo = geometry_range(lines)
    if geo is None:
        return False
    start, end, charge, mult = geo
    geom_resolved = geom_file.resolve()
    base_resolved = base_dir.resolve()
    try:
        rel = geom_resolved.relative_to(base_resolved)
    except ValueError:
        rel = geom_resolved
    ref = str(rel).replace("\\", "/")
    if " " in ref:
        ref = f'"{ref}"'
    lines[start:end] = [f"* xyzfile {charge} {mult} {ref}"]
    return True


@dataclass(frozen=True)
class OrcaBlockRow:
    """One active body row of a ``%block``: its line index and non-comment tokens."""

    line_index: int
    tokens: tuple[OrcaLineToken, ...]

    @property
    def text(self) -> str:
        return " ".join(token.value for token in self.tokens)


@dataclass(frozen=True)
class OrcaBlock:
    """One ``%name`` block found by :func:`iter_blocks`.

    This is the shared block-termination rule of the package: a block closes at
    the first unquoted ``end`` token outside a nested ``scan``/``constraints``
    sub-block -- on the header line itself (``%pal nprocs 8 end``) or on a
    later line -- and an unterminated block is cut by the next ``%`` directive,
    by the geometry section (``*``), or by end of input. ``rows`` are the
    active body rows in order, starting with the header remainder when it
    carries tokens; tokens after the closing ``end`` are not body rows.

    ``end`` is the index of the line carrying the closing ``end`` token
    (``start`` for an inline-closed block). When ``closed`` is False it is the
    index of the line that cut the block or ``len(lines)``, i.e. where an
    ``end`` line has to be inserted to close the block.
    """

    name: str
    start: int
    end: int
    closed: bool
    rows: tuple[OrcaBlockRow, ...]

    @property
    def needs_close(self) -> bool:
        return not self.closed


def percent_directive_header(tokens: list[OrcaLineToken]) -> tuple[str, int] | None:
    """``(block name, body start index)`` for a ``%name`` / ``% name`` line, else ``None``."""

    if not tokens or tokens[0].quoted:
        return None
    if tokens[0].value.startswith("%") and tokens[0].value != "%":
        return tokens[0].value[1:].lower(), 1
    if len(tokens) >= 2 and tokens[0].value == "%" and not tokens[1].quoted:
        return tokens[1].value.lower(), 2
    return None


def _unquoted_end_index(tokens: Sequence[OrcaLineToken], start: int) -> int:
    return next(
        (
            index
            for index in range(start, len(tokens))
            if not tokens[index].quoted and tokens[index].value.lower() == "end"
        ),
        len(tokens),
    )


def _scan_block(
    lines: Sequence[str],
    *,
    name: str,
    start: int,
    header_tokens: list[OrcaLineToken],
    body_start: int,
) -> OrcaBlock:
    rows: list[OrcaBlockRow] = []
    end_index = _unquoted_end_index(header_tokens, body_start)
    if header_tokens[body_start:end_index]:
        rows.append(OrcaBlockRow(start, tuple(header_tokens[body_start:end_index])))
    if end_index < len(header_tokens):
        return OrcaBlock(name, start, start, True, tuple(rows))
    nested_depth = 0
    for index in range(start + 1, len(lines)):
        tokens = orca_line_tokens(lines[index])
        if not tokens:
            continue
        first = tokens[0]
        if percent_directive_header(tokens) is not None or (
            not first.quoted and first.value.startswith("*")
        ):
            return OrcaBlock(name, start, index, False, tuple(rows))
        if len(tokens) == 1 and not first.quoted and first.value.lower() in NESTED_BLOCK_NAMES:
            nested_depth += 1
            rows.append(OrcaBlockRow(index, tuple(tokens)))
            continue
        end_index = _unquoted_end_index(tokens, 0)
        if end_index == len(tokens) or nested_depth > 0:
            if end_index < len(tokens):
                nested_depth -= 1
            rows.append(OrcaBlockRow(index, tuple(tokens)))
            continue
        if end_index > 0:
            rows.append(OrcaBlockRow(index, tuple(tokens[:end_index])))
        return OrcaBlock(name, start, index, True, tuple(rows))
    return OrcaBlock(name, start, len(lines), False, tuple(rows))


def iter_blocks(lines: Sequence[str], block_name: str) -> Iterator[OrcaBlock]:
    """Yield every ``%block_name`` block of ``lines`` in order (see :class:`OrcaBlock`)."""

    name = block_name.lower()
    index = 0
    while index < len(lines):
        tokens = orca_line_tokens(lines[index])
        header = percent_directive_header(tokens)
        if header is None or header[0] != name:
            index += 1
            continue
        block = _scan_block(
            lines,
            name=name,
            start=index,
            header_tokens=tokens,
            body_start=header[1],
        )
        yield block
        index = block.end + 1 if block.closed else max(block.end, index + 1)


def find_block(lines: Sequence[str], block_name: str) -> OrcaBlock | None:
    """Return the first ``%block_name`` block, or ``None``."""

    return next(iter_blocks(lines, block_name), None)


def find_block_range(lines: list[str], block_name: str) -> tuple[int, int, bool] | None:
    """Return ``(start, end, needs_close)`` of the first ``%block_name`` block."""

    block = find_block(lines, block_name)
    if block is None:
        return None
    return block.start, block.end, block.needs_close


def set_block_key_value(lines: list[str], block_name: str, key: str, value: str) -> bool:
    block_starts = [
        index
        for index, line in enumerate(lines)
        if (match := BLOCK_START_RE.match(active_orca_directive_text(line))) is not None
        and match.group(1).lower() == block_name.lower()
    ]
    if len(block_starts) > 1:
        raise ValueError(f"ORCA input has duplicate %{block_name} blocks")

    rng = find_block_range(lines, block_name)
    if rng is not None:
        start, end, _needs_close = rng
        start_match = BLOCK_START_RE.match(active_orca_directive_text(lines[start]))
        assert start_match is not None
        inline_tokens = orca_line_tokens(
            active_orca_directive_text(lines[start]),
            start=start_match.end(),
        )
        key_occurrences = sum(
            1 for token in inline_tokens if not token.quoted and token.value.lower() == key.lower()
        )
        block = find_block(lines, block_name)
        body_rows = tuple(row for row in block.rows if row.line_index != start) if block else ()
        key_occurrences += sum(
            1
            for row in body_rows
            if not row.tokens[0].quoted and row.tokens[0].value.lower() == key.lower()
        )
        if key_occurrences > 1:
            raise ValueError(f"ORCA %{block_name} block has duplicate {key} directives")

    inline_result = _set_inline_block_key_value(lines, block_name, key, value)
    if inline_result is not None:
        return inline_result

    key_lower = key.lower()

    if rng is None:
        insert_at = find_geometry_start(lines)
        if insert_at is None:
            insert_at = len(lines)
        new_block = [f"%{block_name}", f"  {key} {value}", "end", ""]
        lines[insert_at:insert_at] = new_block
        return True

    start, end, needs_close = rng
    canonical_start = active_orca_directive_text(lines[start])
    changed = False
    if lines[start] != canonical_start:
        lines[start] = canonical_start
        changed = True
    if needs_close:
        lines.insert(end, "end")
    replaced = False
    found = find_block(lines, block_name)
    body_rows = tuple(row for row in found.rows if row.line_index != start) if found else ()
    for row in body_rows:
        if row.tokens[0].quoted or row.tokens[0].value.lower() != key_lower:
            continue
        # A body row may carry the closing ``end`` (``nprocs 8 end``); keep it.
        closes_block = found is not None and found.closed and row.line_index == found.end
        new_line = f"  {key} {value}" + (" end" if closes_block else "")
        if lines[row.line_index] != new_line:
            lines[row.line_index] = new_line
            changed = True
        replaced = True

    if not replaced:
        lines.insert(end, f"  {key} {value}")
        changed = True
    return changed


def _set_inline_block_key_value(
    lines: list[str], block_name: str, key: str, value: str
) -> bool | None:
    """Update a value carried on the ``%block`` start line when present."""

    for index, line in enumerate(lines):
        active_line = active_orca_directive_text(line)
        block_match = BLOCK_START_RE.match(active_line)
        if block_match is None or block_match.group(1).lower() != block_name.lower():
            continue
        remainder_start = block_match.end()
        tokens = orca_line_tokens(active_line, start=remainder_start)
        end_token = next(
            (token for token in tokens if not token.quoted and token.value.lower() == "end"),
            None,
        )
        body_tokens = [
            token for token in tokens if end_token is None or token.start < end_token.start
        ]
        key_index = next(
            (
                token_index
                for token_index, token in enumerate(body_tokens)
                if not token.quoted and token.value.lower() == key.lower()
            ),
            None,
        )
        if key_index is not None:
            value_index = key_index + 1
            if value_index < len(body_tokens) and body_tokens[value_index].value == "=":
                value_index += 1
            if value_index >= len(body_tokens):
                return None
            key_token = body_tokens[key_index]
            value_token = body_tokens[value_index]
            updated = (
                active_line[: key_token.start] + f"{key} {value}" + active_line[value_token.end :]
            )
        elif end_token is not None:
            body = active_line[remainder_start : end_token.start]
            separator = "" if not body or body[-1].isspace() else " "
            updated_body = f"{body}{separator}{key} {value} "
            updated = active_line[:remainder_start] + updated_body + active_line[end_token.start :]
        else:
            return None
        if updated == active_line:
            return False
        lines[index] = updated
        return True
    return None
