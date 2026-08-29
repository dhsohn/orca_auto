from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

GEOM_HEADER_RE = re.compile(
    r"^\s*\*\s+(xyzfile|xyz)\s+(-?\d+)\s+(\d+)(?:\s+(.*))?$",
    re.IGNORECASE,
)
COORDS_BLOCK_RE = re.compile(r"^\s*%\s*coords\b", re.IGNORECASE)
BLOCK_START_RE = re.compile(r"^\s*%([A-Za-z0-9_\-]+)")
MOINP_RE = re.compile(r"^\s*%moinp\b", re.IGNORECASE)
MAXCORE_DIRECTIVE_RE = re.compile(r"^\s*%maxcore\b", re.IGNORECASE)
NPROCS_DIRECTIVE_RE = re.compile(r"\bnprocs\s+\d+\b", re.IGNORECASE)
PAL_ROUTE_TOKEN_RE = re.compile(r"\APAL\d+\Z", re.IGNORECASE)
_SAFE_UNQUOTED_ORCA_PATH_RE = re.compile(r"^[A-Za-z0-9._/+\-]+$")
NESTED_BLOCK_NAMES = frozenset({"scan", "constraints"})


@dataclass(frozen=True)
class OrcaLineToken:
    value: str
    start: int
    end: int
    quoted: bool = False


def validate_supported_xyz_geometry_syntax(
    lines: list[str],
    *,
    label: str,
) -> None:
    """Fail closed for geometry forms whose atom count and dependencies are not bound."""

    validate_unambiguous_orca_directives(lines, label=label)
    geometry_block_count = 0
    inline_geometry_open = False
    for line in lines:
        stripped = line.strip()
        tokens = orca_line_tokens(line)
        if not tokens:
            continue
        first_lower = tokens[0].value.lower()
        if any(
            token.value.lower() in {"compound", "compound_file"}
            or token.value.lower().startswith("%compound")
            for token in orca_route_tokens(line)
            if not token.quoted
        ):
            raise ValueError(f"{label} uses an unsupported multiple-job geometry construct")
        spaced_percent_keyword = (
            f"%{tokens[1].value.lower()}" if len(tokens) >= 2 and tokens[0].value == "%" else ""
        )
        if (
            COORDS_BLOCK_RE.match(stripped)
            or first_lower == "%coords"
            or spaced_percent_keyword == "%coords"
        ):
            raise ValueError(f"{label} uses an unsupported %coords geometry block")
        if (
            first_lower in {"$new_job", "$newjob", "compound", "compound_file"}
            or first_lower.startswith("%compound")
            or spaced_percent_keyword.startswith("%compound")
        ):
            raise ValueError(f"{label} uses an unsupported multiple-job geometry construct")
        first = tokens[0].value
        if stripped == "*" or (first == "*" and len(tokens) == 1):
            if not inline_geometry_open:
                raise ValueError(f"{label} has an unexpected ORCA geometry terminator")
            inline_geometry_open = False
            continue
        if first == "*" or first.startswith("*"):
            if inline_geometry_open:
                raise ValueError(f"{label} has an unterminated inline ORCA geometry block")
            match = GEOM_HEADER_RE.match(stripped)
            if match is None or match.group(1).lower() not in {"xyz", "xyzfile"}:
                raise ValueError(f"{label} uses an unsupported ORCA geometry format")
            geometry_type = match.group(1).lower()
            expected_token_count = 5 if geometry_type == "xyzfile" else 4
            if len(tokens) != expected_token_count:
                raise ValueError(f"{label} has an invalid {geometry_type} geometry header")
            geometry_block_count += 1
            if geometry_block_count > 1:
                raise ValueError(f"{label} uses unsupported multiple ORCA geometry blocks")
            inline_geometry_open = geometry_type == "xyz"
    if geometry_block_count != 1:
        raise ValueError(f"{label} must define exactly one supported ORCA geometry block")
    if inline_geometry_open:
        raise ValueError(f"{label} has an unterminated inline ORCA geometry block")


def validate_unambiguous_orca_directives(lines: list[str], *, label: str) -> None:
    """Reject duplicate resource/checkpoint directives with unclear ORCA precedence."""

    maxcore_count = 0
    moinp_count = len(orca_moinp_references(lines))
    pal_block_count = 0
    pal_nprocs_count = 0
    pal_route_count = 0
    in_pal_block = False
    for line in lines:
        active_directive = active_orca_directive_text(line)
        if MAXCORE_DIRECTIVE_RE.match(active_directive):
            maxcore_count += 1
        block_match = BLOCK_START_RE.match(active_directive)
        if block_match is not None:
            in_pal_block = block_match.group(1).lower() == "pal"
            if in_pal_block:
                pal_block_count += 1
                pal_nprocs_count += len(NPROCS_DIRECTIVE_RE.findall(active_directive))
                if any(
                    not token.quoted and token.value.lower() == "end"
                    for token in orca_line_tokens(active_directive, start=block_match.end())
                ):
                    in_pal_block = False
        elif in_pal_block:
            active_text = active_orca_line_text(line)
            if active_text.strip().lower() == "end":
                in_pal_block = False
            else:
                pal_nprocs_count += len(NPROCS_DIRECTIVE_RE.findall(active_text))

        pal_route_count += sum(
            1
            for token in orca_route_tokens(line)
            if not token.quoted and PAL_ROUTE_TOKEN_RE.fullmatch(token.value)
        )

    duplicate_labels = [
        name
        for name, count in (
            ("%maxcore", maxcore_count),
            ("%moinp", moinp_count),
            ("%pal blocks", pal_block_count),
            ("%pal nprocs", pal_nprocs_count),
            ("PAL route shorthands", pal_route_count),
        )
        if count > 1
    ]
    if pal_block_count and pal_route_count:
        duplicate_labels.append("mixed %pal and PAL route shorthands")
    if duplicate_labels:
        raise ValueError(
            f"{label} has ambiguous duplicate ORCA directives: {', '.join(duplicate_labels)}"
        )


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


def nonempty_file(path: Path) -> bool:
    """True when ``path`` exists with size > 0; False when missing or unreadable."""
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


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

    tokens = orca_line_tokens(line)
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

    tokens = orca_route_tokens(line)
    active_tokens = orca_line_tokens(line)
    if not active_tokens or not active_tokens[0].value.startswith("!"):
        return None
    suffix = " ".join(token.value for token in tokens)
    return f"! {suffix}".rstrip()


def find_route_idx(lines: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        if orca_route_line(line) is not None:
            return idx
    return None


def route_line_indices(lines: list[str]) -> list[int]:
    return [idx for idx, line in enumerate(lines) if orca_route_line(line) is not None]


def file_route_lines(inp_path: Path) -> list[str]:
    """All route (``!``) lines of an ORCA input, stripped; ``[]`` when unreadable.

    ORCA accepts multiple route lines and allows ``%`` blocks before them, so
    callers deciding "does this input request X" must scan every route line,
    not just the first one. ``#`` comments are cut before returning: keyword
    regexes (TS/IRC/OPT/...) run on these lines, and a comment like
    ``# TS guess`` must never reclassify the job.
    """
    try:
        lines = inp_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    return [
        route
        for idx in route_line_indices(lines)
        if (route := orca_route_line(lines[idx])) is not None
    ]


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


def find_geometry_start(lines: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        if GEOM_HEADER_RE.match(line.strip()):
            return idx
    return None


def find_block_range(lines: list[str], block_name: str) -> tuple[int, int, bool] | None:
    name = block_name.lower()
    for i, line in enumerate(lines):
        m = BLOCK_START_RE.match(active_orca_directive_text(line))
        if not m:
            continue
        if m.group(1).lower() != name:
            continue
        nested_depth = 0
        for j in range(i + 1, len(lines)):
            stripped = active_orca_line_text(lines[j]).strip().lower()
            if stripped in NESTED_BLOCK_NAMES:
                nested_depth += 1
                continue
            if stripped == "end":
                if nested_depth > 0:
                    nested_depth -= 1
                    continue
                return i, j, False
        return i, len(lines), True
    return None


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
        key_occurrences += sum(
            1
            for line in lines[start + 1 : end]
            if (tokens := orca_line_tokens(active_orca_line_text(line)))
            and not tokens[0].quoted
            and tokens[0].value.lower() == key.lower()
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
        block = [f"%{block_name}", f"  {key} {value}", "end", ""]
        lines[insert_at:insert_at] = block
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
    for i in range(start + 1, end):
        stripped = active_orca_line_text(lines[i]).strip()
        if not stripped:
            continue
        body_tokens = stripped.split()
        if body_tokens and body_tokens[0].lower() == key_lower:
            new_line = f"  {key} {value}"
            if lines[i] != new_line:
                lines[i] = new_line
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


def set_moinp(lines: list[str], checkpoint: Path, base_dir: Path) -> bool:
    ref = quote_orca_path(format_relative_or_absolute(checkpoint, base_dir))
    new_line = f"%moinp {ref}"
    matches = [
        idx for idx, line in enumerate(lines) if MOINP_RE.match(active_orca_directive_text(line))
    ]
    semantic_references = orca_moinp_references(lines)
    noncanonical_references = [
        reference for reference in semantic_references if reference.line_index not in matches
    ]
    if noncanonical_references:
        if len(semantic_references) != 1:
            raise ValueError("ORCA input has duplicate semantic MOInp declarations")
        reference = noncanonical_references[0]
        current = lines[reference.line_index]
        updated = current[: reference.start] + ref + current[reference.end :]
        if updated == current:
            return False
        lines[reference.line_index] = updated
        return True
    if matches:
        first = matches[0]
        changed = lines[first] != new_line or len(matches) > 1
        lines[first] = new_line
        for idx in reversed(matches[1:]):
            del lines[idx]
        return changed

    insert_at = find_geometry_start(lines)
    if insert_at is None:
        insert_at = len(lines)
    lines.insert(insert_at, new_line)
    return True


def geometry_range(lines: list[str]) -> tuple[int, int, int, int] | None:
    for start, line in enumerate(lines):
        m = GEOM_HEADER_RE.match(line.strip())
        if not m:
            continue
        geom_type = m.group(1).lower()
        charge = int(m.group(2))
        mult = int(m.group(3))
        if geom_type == "xyzfile":
            return start, start + 1, charge, mult
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].strip() == "*":
                end = i + 1
                break
        return start, end, charge, mult
    return None


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
class OrcaFileReference:
    """One external file reference of an ORCA input, with its source span."""

    line_index: int
    value: str
    start: int
    end: int
    kind: str  # "geometry" | "neb_geometry" | "auxiliary"


def _percent_directive_header(tokens: list[OrcaLineToken]) -> tuple[str, int] | None:
    if not tokens or tokens[0].quoted:
        return None
    if tokens[0].value.startswith("%") and tokens[0].value != "%":
        return tokens[0].value[1:].lower(), 1
    if len(tokens) >= 2 and tokens[0].value == "%" and not tokens[1].quoted:
        return tokens[1].value.lower(), 2
    return None


def _scf_body_token_rows(lines: list[str]) -> list[tuple[int, list[OrcaLineToken]]]:
    """Return active ``%scf`` body tokens, stopping each block at ``end``."""

    rows: list[tuple[int, list[OrcaLineToken]]] = []
    in_scf_block = False
    for line_index, line in enumerate(lines):
        tokens = orca_line_tokens(line)
        if not tokens:
            continue
        header = _percent_directive_header(tokens)
        if header is not None:
            block_name, body_start = header
            in_scf_block = block_name == "scf"
            if not in_scf_block:
                continue
        elif in_scf_block:
            body_start = 0
        else:
            continue

        end_index = next(
            (
                index
                for index in range(body_start, len(tokens))
                if not tokens[index].quoted and tokens[index].value.lower() == "end"
            ),
            len(tokens),
        )
        rows.append((line_index, tokens[body_start:end_index]))
        if end_index < len(tokens):
            in_scf_block = False
    return rows


def _reference_after_keyword(
    *,
    line_index: int,
    line: str,
    tokens: list[OrcaLineToken],
    keyword_index: int,
) -> OrcaFileReference:
    value_index = keyword_index + 1
    if value_index < len(tokens) and tokens[value_index].value == "=":
        value_index += 1
    if value_index >= len(tokens):
        raise ValueError(f"Invalid ORCA auxiliary file reference: {line.strip()}")
    value_token = tokens[value_index]
    value = value_token.value.strip()
    if not value or (not value_token.quoted and value.lower() == "end"):
        raise ValueError(f"Invalid ORCA auxiliary file reference: {line.strip()}")
    return OrcaFileReference(
        line_index=line_index,
        value=value,
        start=value_token.start,
        end=value_token.end,
        kind="auxiliary",
    )


def orca_moinp_references(lines: list[str]) -> list[OrcaFileReference]:
    """Return every semantic top-level or ``%scf`` ``MOInp`` occurrence."""

    references: list[OrcaFileReference] = []
    for line_index, line in enumerate(lines):
        tokens = orca_line_tokens(line)
        header = _percent_directive_header(tokens)
        if header is None or header[0] != "moinp":
            continue
        references.append(
            _reference_after_keyword(
                line_index=line_index,
                line=line,
                tokens=tokens,
                keyword_index=header[1] - 1,
            )
        )
    for line_index, body_tokens in _scf_body_token_rows(lines):
        for token_index, token in enumerate(body_tokens):
            if token.quoted or token.value.lower() != "moinp":
                continue
            references.append(
                _reference_after_keyword(
                    line_index=line_index,
                    line=lines[line_index],
                    tokens=body_tokens,
                    keyword_index=token_index,
                )
            )
    return sorted(references, key=lambda reference: (reference.line_index, reference.start))


def orca_input_requests_moread(lines: list[str]) -> bool:
    """Return whether active route or ``%scf`` semantics request orbital reuse."""

    if orca_moinp_references(lines):
        return True
    if any(
        not token.quoted and token.value.lower() == "moread"
        for line in lines
        for token in orca_route_tokens(line)
    ):
        return True
    return any(
        not token.quoted and token.value.lower() == "moread"
        for _line_index, tokens in _scf_body_token_rows(lines)
        for token in tokens
    )
