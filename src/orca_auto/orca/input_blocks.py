"""Structural units of an ORCA input: the geometry section and ``%name`` blocks.

Sits on :mod:`.input_syntax` (tokens, comments, route lines) and provides the
two primitives every input rewriter and scanner shares: locating the single
``* xyz`` / ``* xyzfile`` geometry block, and walking ``%name ... end`` blocks
under the package-wide block-termination rule of :class:`OrcaBlock`. It also
reads the ``%geom`` ``Scan`` coordinates (``scan_coordinate_rows``) that the
route classification and the relaxed-scan report share. The editing helper
here (``set_block_key_value``) changes one block at a time and never looks at
external file references; that is :mod:`.input_references`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import TypeAlias

from .input_syntax import (
    OrcaLineToken,
    active_orca_directive_text,
    active_text_from_tokens,
    orca_line_tokens,
    route_line_from_tokens,
    route_tokens_from,
    value_token_index,
)

GEOM_HEADER_RE = re.compile(
    r"^\s*\*\s+(xyzfile|xyz)\s+(-?\d+)\s+(\d+)(?:\s+(.*))?$",
    re.IGNORECASE,
)
BLOCK_START_RE = re.compile(r"^\s*%([A-Za-z0-9_\-]+)")
NESTED_BLOCK_NAMES = frozenset(
    {
        "scan",
        "constraints",
        "modify_internal",
        "constrainfragments",
        "connectfragments",
        "fixfrags",
        "relaxhfrags",
        "relaxfrags",
        "rigidfrags",
    }
)


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


def xyzfile_reference_token(tokens: Sequence[OrcaLineToken]) -> OrcaLineToken | None:
    """The file token of a ``* xyzfile charge multiplicity file`` line, else ``None``."""

    if len(tokens) < 5 or tokens[0].value != "*" or tokens[1].value.lower() != "xyzfile":
        return None
    return tokens[4]


def geometry_header_match(
    line: str, tokens: Sequence[OrcaLineToken] | None = None
) -> re.Match[str] | None:
    """Match ``GEOM_HEADER_RE`` against the active (comment-free) text of ``line``.

    ``tokens`` are the line's tokens when the caller has already read them.
    """

    return GEOM_HEADER_RE.match(
        active_text_from_tokens(line, orca_line_tokens(line) if tokens is None else tokens)
    )


def find_geometry_block(lines: InputSource) -> OrcaGeometryBlock | None:
    """Locate the first geometry header and, for ``xyz``, its atom rows."""

    return parse_input(lines).geometry


def _find_geometry_block(
    lines: Sequence[str], tokens: Sequence[Sequence[OrcaLineToken]]
) -> OrcaGeometryBlock | None:
    for header_index, line in enumerate(lines):
        match = geometry_header_match(line, tokens[header_index])
        if match is None:
            continue
        kind = match.group(1).lower()
        charge = int(match.group(2))
        multiplicity = int(match.group(3))
        if kind == "xyzfile":
            file_token = xyzfile_reference_token(tokens[header_index])
            reference = file_token.value if file_token is not None else None
            return OrcaGeometryBlock(header_index, kind, charge, multiplicity, reference, (), None)
        atom_rows: list[tuple[int, str]] = []
        for index in range(header_index + 1, len(lines)):
            text = active_text_from_tokens(lines[index], tokens[index]).strip()
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


def find_geometry_start(lines: InputSource) -> int | None:
    block = find_geometry_block(lines)
    return None if block is None else block.header_index


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
    the first unquoted ``end`` token outside a nested geometry
    sub-block -- on the header line itself (``%pal nprocs 8 end``) or on a
    later line. A token in NESTED_BLOCK_NAMES opens a sub-block wherever it
    stands (``%geom Constraints``, ``Constraints {B 0 1 C} end``), and each
    ``end`` closes the innermost open sub-block first. An unterminated block is
    cut by the next ``%`` directive, by the geometry section (``*``), or by end
    of input. ``rows`` are the active body rows in order, starting with the
    header remainder when it carries tokens; tokens after the closing ``end``
    are not body rows.

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


def percent_directive_header(tokens: Sequence[OrcaLineToken]) -> tuple[str, int] | None:
    """``(block name, body start index)`` for a ``%name`` / ``% name`` line, else ``None``."""

    if not tokens or tokens[0].quoted:
        return None
    if tokens[0].value.startswith("%") and tokens[0].value != "%":
        return tokens[0].value[1:].lower(), 1
    if len(tokens) >= 2 and tokens[0].value == "%" and not tokens[1].quoted:
        return tokens[1].value.lower(), 2
    return None


def _closing_end_index(
    tokens: Sequence[OrcaLineToken], start: int, nested_depth: int
) -> tuple[int, int]:
    """Index of the block-closing ``end`` (``len(tokens)`` if none) and the depth after the row."""

    for index in range(start, len(tokens)):
        token = tokens[index]
        if token.quoted:
            continue
        word = token.value.lower()
        if word in NESTED_BLOCK_NAMES:
            nested_depth += 1
        elif word == "end":
            if nested_depth == 0:
                return index, 0
            nested_depth -= 1
    return len(tokens), nested_depth


def _scan_block(
    tokens_by_line: Sequence[Sequence[OrcaLineToken]],
    *,
    name: str,
    start: int,
    body_start: int,
) -> OrcaBlock:
    header_tokens = tokens_by_line[start]
    rows: list[OrcaBlockRow] = []
    end_index, nested_depth = _closing_end_index(header_tokens, body_start, 0)
    if header_tokens[body_start:end_index]:
        rows.append(OrcaBlockRow(start, tuple(header_tokens[body_start:end_index])))
    if end_index < len(header_tokens):
        return OrcaBlock(name, start, start, True, tuple(rows))
    for index in range(start + 1, len(tokens_by_line)):
        tokens = tokens_by_line[index]
        if not tokens:
            continue
        first = tokens[0]
        if percent_directive_header(tokens) is not None or (
            not first.quoted and first.value.startswith("*")
        ):
            return OrcaBlock(name, start, index, False, tuple(rows))
        end_index, nested_depth = _closing_end_index(tokens, 0, nested_depth)
        if end_index > 0:
            rows.append(OrcaBlockRow(index, tuple(tokens[:end_index])))
        if end_index < len(tokens):
            return OrcaBlock(name, start, index, True, tuple(rows))
    return OrcaBlock(name, start, len(tokens_by_line), False, tuple(rows))


def _scan_blocks(tokens_by_line: Sequence[Sequence[OrcaLineToken]]) -> tuple[OrcaBlock, ...]:
    """Every ``%name`` block in input order, in one pass (see :class:`OrcaBlock`).

    A block's body never holds another ``%`` header (one cuts the block), so
    one pass over all headers yields, per name, exactly the blocks a walk for
    that name alone would find.
    """

    blocks: list[OrcaBlock] = []
    index = 0
    while index < len(tokens_by_line):
        header = percent_directive_header(tokens_by_line[index])
        if header is None:
            index += 1
            continue
        block = _scan_block(tokens_by_line, name=header[0], start=index, body_start=header[1])
        blocks.append(block)
        index = block.end + 1 if block.closed else max(block.end, index + 1)
    return tuple(blocks)


def iter_blocks(lines: InputSource, block_name: str) -> Iterator[OrcaBlock]:
    """Yield every ``%block_name`` block of ``lines`` in order (see :class:`OrcaBlock`)."""

    return iter(parse_input(lines).blocks_named(block_name))


# The block keys that declare an operation: the %freq frequency switches, the
# %geom TS search switch and the %method run type.
OPERATION_BLOCK_KEYS: dict[str, frozenset[str]] = {
    "freq": frozenset({"anfreq", "numfreq"}),
    "geom": frozenset({"ts_search"}),
    "method": frozenset({"runtyp"}),
}
# The ``%method RunTyp`` spellings of the two single-point jobs, lower case: a
# plain energy, and an energy with a gradient (which writes <basename>.engrad,
# as the EnGrad and NumGrad route keywords do). Submission admits exactly
# these, the binding reserves the gradient output and the queue Detail names
# the gradient run types; adding an alias here reaches all three.
SINGLE_POINT_RUN_TYPES = frozenset({"energy", "sp"})
GRADIENT_RUN_TYPES = frozenset({"gradient", "engrad", "energygrad", "numgrad"})


@dataclass(frozen=True)
class BlockDeclaration:
    """One operation key (:data:`OPERATION_BLOCK_KEYS`) in a ``%block`` body.

    Facts only: whether the key or its value is quoted, whether the block
    closed, and where the key stands. Each reader decides what an uncertain
    declaration means (completion skips a quoted key, the queue Detail calls it
    unknown, submission rejects an unreadable run type).
    """

    block: str
    key: str  # lower case
    key_quoted: bool
    line_index: int
    column: int
    value: OrcaLineToken | None  # None: the key has no value
    block_closed: bool


def block_operation_declarations(lines: InputSource) -> tuple[BlockDeclaration, ...]:
    """Every operation key of every ``%freq``/``%geom``/``%method`` body, in input order per block.

    Blocks of one name come in input order and keys in body order, so a reader
    that lets the last value win sees ORCA's order. Comments and tokens after
    a block's closing ``end`` are not body tokens.
    """

    return parse_input(lines).operation_declarations


def _block_operation_declarations(parsed: ParsedInput) -> tuple[BlockDeclaration, ...]:
    declarations: list[BlockDeclaration] = []
    for name, keys in OPERATION_BLOCK_KEYS.items():
        for block in parsed.blocks_named(name):
            located = [(row.line_index, token) for row in block.rows for token in row.tokens]
            tokens = [token for _line_index, token in located]
            for index, (line_index, token) in enumerate(located):
                key = token.value.lower()
                if key not in keys:
                    continue
                value_index = value_token_index(tokens, index)
                declarations.append(
                    BlockDeclaration(
                        block=name,
                        key=key,
                        key_quoted=token.quoted,
                        line_index=line_index,
                        column=token.start,
                        value=tokens[value_index] if value_index < len(tokens) else None,
                        block_closed=block.closed,
                    )
                )
    return tuple(declarations)


def find_block(lines: InputSource, block_name: str) -> OrcaBlock | None:
    """Return the first ``%block_name`` block, or ``None``."""

    return next(iter_blocks(lines, block_name), None)


def find_block_range(lines: list[str], block_name: str) -> tuple[int, int, bool] | None:
    """Return ``(start, end, needs_close)`` of the first ``%block_name`` block."""

    block = find_block(lines, block_name)
    if block is None:
        return None
    return block.start, block.end, block.needs_close


def scan_coordinate_rows(lines: InputSource) -> list[str] | None:
    """Coordinate texts of the ``%geom`` ``Scan`` sub-blocks; ``None`` when there is none.

    A ``Scan`` sub-block counts even when no coordinate in it can be read.
    Walks each ``%geom`` header up to the next ``%`` directive, route line, or
    geometry section, past the block's own closing ``end``: the shared block
    rule may not know every ORCA sub-block. ``Scan`` may share its row with a
    coordinate or its ``end``.
    """
    rows = parse_input(lines).scan_coordinate_rows
    return None if rows is None else list(rows)


def _scan_coordinate_rows(
    tokens_by_line: Sequence[Sequence[OrcaLineToken]],
) -> tuple[str, ...] | None:
    rows: list[str] = []
    found = in_geom = in_scan = False
    for line_tokens in tokens_by_line:
        tokens = line_tokens
        if not tokens:
            continue
        header = percent_directive_header(tokens)
        if header is not None:
            in_geom = header[0] == "geom"
            in_scan = False
            tokens = tokens[header[1] :]
        elif not tokens[0].quoted and tokens[0].value.startswith(("*", "!")):
            in_geom = in_scan = False
        if not in_geom:
            continue
        if not in_scan:
            scan_index = _unquoted_word_index(tokens, "scan")
            if scan_index is None:
                continue
            found = in_scan = True
            tokens = tokens[scan_index + 1 :]
        end_index = _unquoted_word_index(tokens, "end")
        if end_index is not None:
            in_scan = False
            tokens = tokens[:end_index]
        if tokens:
            rows.append(" ".join(token.value for token in tokens))
    return tuple(rows) if found else None


def _unquoted_word_index(tokens: Sequence[OrcaLineToken], word: str) -> int | None:
    return next(
        (
            index
            for index, token in enumerate(tokens)
            if not token.quoted and token.value.lower() == word
        ),
        None,
    )


@dataclass(frozen=True, eq=False)
class ParsedInput:
    """One version of an ORCA input, tokenized once.

    ``lines`` are the input's lines as read and ``tokens`` the
    :func:`~.input_syntax.orca_line_tokens` of each line, with their source
    spans. The structural readings (route tokens and lines, ``%`` blocks,
    the geometry section, the operation declarations, the ``Scan``
    coordinates) are derived from those tokens on first use and kept, so every
    reader of one input version shares one reading and keeps the source
    positions it needs for diagnostics. A rewritten input is a new version:
    parse it again, never patch this one.
    """

    lines: tuple[str, ...]
    tokens: tuple[tuple[OrcaLineToken, ...], ...]

    @cached_property
    def route_tokens(self) -> tuple[tuple[OrcaLineToken, ...], ...]:
        """Per line, the tokens after its ``!`` route marker (none for other lines)."""
        return tuple(tuple(route_tokens_from(tokens)) for tokens in self.tokens)

    @cached_property
    def route_lines(self) -> tuple[str, ...]:
        """Every canonical active route line (``orca_route_lines``)."""
        return tuple(
            route for tokens in self.tokens if (route := route_line_from_tokens(tokens)) is not None
        )

    @cached_property
    def blocks(self) -> tuple[OrcaBlock, ...]:
        """Every ``%name`` block in input order."""
        return _scan_blocks(self.tokens)

    def blocks_named(self, block_name: str) -> tuple[OrcaBlock, ...]:
        name = block_name.lower()
        return tuple(block for block in self.blocks if block.name == name)

    @cached_property
    def geometry(self) -> OrcaGeometryBlock | None:
        return _find_geometry_block(self.lines, self.tokens)

    @cached_property
    def operation_declarations(self) -> tuple[BlockDeclaration, ...]:
        return _block_operation_declarations(self)

    @cached_property
    def scan_coordinate_rows(self) -> tuple[str, ...] | None:
        return _scan_coordinate_rows(self.tokens)

    def scan_coordinate_rows_between(self, start: int, stop: int) -> tuple[str, ...] | None:
        """:func:`scan_coordinate_rows` of the lines ``start`` to ``stop`` only."""
        return _scan_coordinate_rows(self.tokens[start:stop])


InputSource: TypeAlias = "Sequence[str] | ParsedInput"


def parse_input(lines: InputSource) -> ParsedInput:
    """Tokenize ``lines`` once; a :class:`ParsedInput` is returned unchanged."""

    if isinstance(lines, ParsedInput):
        return lines
    source = tuple(lines)
    return ParsedInput(source, tuple(tuple(orca_line_tokens(line)) for line in source))


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
            value_index = value_token_index(body_tokens, key_index)
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
