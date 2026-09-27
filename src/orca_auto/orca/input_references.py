"""External file references of an ORCA input: ``MOInp`` checkpoints and the rest.

Sits on :mod:`.input_blocks` and :mod:`.input_syntax`. This module owns every
reading of "which files does this input pull in": the semantic ``MOInp``
occurrences (top-level ``%moinp`` and ``%scf MOInp``) that
:func:`set_moinp` rewrites and :func:`orca_input_requests_moread` reads,
whether a referenced ``.gbw`` checkpoint is intact enough to seed from, and
the fail-closed :func:`scan_orca_file_references` scanner. Execution binding
binds every reference the scanner returns into the generation, and claim-time
verification compares the bound input's references with the snapshot; neither
derives a reference set itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .input_blocks import (
    find_geometry_start,
    iter_blocks,
    percent_directive_header,
    xyzfile_reference_token,
)
from .input_syntax import (
    OrcaLineToken,
    active_orca_directive_text,
    format_relative_or_absolute,
    orca_line_tokens,
    orca_route_tokens,
    quote_orca_path,
    value_token_index,
)

MOINP_RE = re.compile(r"^\s*%moinp\b", re.IGNORECASE)
MAX_ORCA_INPUT_REFERENCES = 128
CHECKPOINT_HEAD_BYTES = 16

_NEB_FILE_REFERENCE_KEYS = frozenset({"product", "ts"})
_SIMPLE_FILE_REFERENCE_KEYS = frozenset({"%moinp", "%pointcharges"})
_BLOCK_FILE_REFERENCE_KEYS = frozenset(
    {
        "eshess",
        "eshessian",
        "gshess",
        "gshessian",
        "hessfile",
        "hess_filename",
        "icfshess",
        "icfshessian",
        "icishess",
        "icishessian",
        "inhessname",
        "ircinithess",
        "iscfshess",
        "iscfshessian",
        "iscishess",
        "iscishessian",
        "moinp",
        "neb_end_xyzfile",
        "neb_restart_xyzfile",
        "neb_ts_xyzfile",
        "product_xyzfile",
        "restart_allxyzfile",
        "rrhessianname",
        "rrhessname",
        "tshess",
        "tshessian",
    }
)
_UNSUPPORTED_FILE_REFERENCE_KEYS = frozenset(
    {
        "%cclib",
        "%ljcoefficients",
        "neb_end_pdbfile",
        "neb_ts_pdbfile",
        "orcafffilename",
        "product_pdbfile",
        "ts_pdbfile",
    }
)
_UNSUPPORTED_EXTERNAL_HOOK_KEYS = frozenset(
    {
        "base",
        "ext_args",
        "ext_params",
        "extargs",
        "extopt",
        "extparams",
        "frag1_methodfile",
        "frag1methodfile",
        "frag2_methodfile",
        "frag2methodfile",
        "gtoauxcname",
        "gtoauxjname",
        "gtoauxjkname",
        "gtoauxname",
        "gtoname",
        "openfile",
        "progcasscf",
        "progcc",
        "progci",
        "progcorr",
        "progepr",
        "progext",
        "progmdci",
        "progmp2",
        "progmrci",
        "prognmr",
        "progplot",
        "progrocis",
        "progscf",
        "progtddft",
        "qm2_customfile",
        "qm2customfile",
        "readfragaux",
        "readfragauxc",
        "readfragauxj",
        "readfragauxjk",
        "readfragbasis",
        "readfragecp",
        "neb_restart_gbwname",
        "restart_gbw_basename",
        "sys_cmd",
        "write2file",
        "xtbinputstring",
        "xtbinputstring2",
        "xtbparamfile",
    }
)
# A quoted value that is absolute, explicitly relative, or ends in a filename
# extension names a file; basis names such as "def2/J" and solvents do not.
_FILE_PATH_VALUE_RE = re.compile(r"(?:^(?:[/\\~]|\.\.?[/\\]))|(?:\.[A-Za-z][A-Za-z0-9_]*$)")
# A quoted name inside an unquoted token, as in the compact ``MO("orb.cube",1,0);``.
_EMBEDDED_QUOTED_RE = re.compile(r"([\"'])(.*?)\1")


@dataclass(frozen=True)
class OrcaFileReference:
    """One external file reference of an ORCA input, with its source span."""

    line_index: int
    value: str
    start: int
    end: int
    kind: str  # "geometry" | "neb_geometry" | "auxiliary"


def nonempty_file(path: Path) -> bool:
    """True when ``path`` exists with size > 0; False when missing or unreadable."""
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def checkpoint_file_looks_intact(path: Path) -> bool:
    """Whether a ``.gbw`` checkpoint is worth seeding orbitals from.

    A crash while ORCA writes its checkpoint can leave a file of the right
    size whose blocks were never flushed: the filesystem then reads them back
    as zeros. ORCA's checkpoint starts with a non-zero header, so a leading
    window without a single non-zero byte is a torn file, not orbitals;
    seeding it with ``MORead`` would make the restarted run fail on a corrupt
    guess instead of degrading to a geometry-only restart. Only the leading
    bytes are inspected; a short non-zero file still counts as a checkpoint.
    """
    if not nonempty_file(path):
        return False
    try:
        with path.open("rb") as handle:
            head = handle.read(CHECKPOINT_HEAD_BYTES)
    except OSError:
        return False
    return any(head)


def _scf_body_token_rows(lines: list[str]) -> list[tuple[int, list[OrcaLineToken]]]:
    """Return active ``%scf`` body tokens per row (see :func:`input_blocks.iter_blocks`)."""

    return [
        (row.line_index, list(row.tokens))
        for block in iter_blocks(lines, "scf")
        for row in block.rows
    ]


def _value_reference(
    line_index: int,
    line: str,
    tokens: list[OrcaLineToken],
    value_index: int,
    kind: str,
) -> OrcaFileReference:
    """The file reference held by ``tokens[value_index]``, or ``ValueError`` without a file name."""

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
        kind=kind,
    )


def orca_moinp_references(lines: list[str]) -> list[OrcaFileReference]:
    """Return every semantic top-level or ``%scf`` ``MOInp`` occurrence."""

    references: list[OrcaFileReference] = []
    for line_index, line in enumerate(lines):
        tokens = orca_line_tokens(line)
        header = percent_directive_header(tokens)
        if header is None or header[0] != "moinp":
            continue
        value_index = value_token_index(tokens, header[1] - 1)
        references.append(_value_reference(line_index, line, tokens, value_index, "auxiliary"))
    for line_index, body_tokens in _scf_body_token_rows(lines):
        for token_index, token in enumerate(body_tokens):
            if token.quoted or token.value.lower() != "moinp":
                continue
            value_index = value_token_index(body_tokens, token_index)
            references.append(
                _value_reference(
                    line_index, lines[line_index], body_tokens, value_index, "auxiliary"
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


def neb_file_reference_context(
    tokens: list[OrcaLineToken],
    *,
    in_neb_block: bool,
) -> tuple[set[int], bool]:
    """Return official ``%neb`` file-key indices and the next block state."""

    header = percent_directive_header(tokens)
    if header is not None:
        block_name, body_start = header
        if block_name != "neb":
            return set(), False
    elif not in_neb_block:
        return set(), False
    else:
        body_start = 0

    end_index = next(
        (
            token_index
            for token_index in range(body_start, len(tokens))
            if not tokens[token_index].quoted and tokens[token_index].value.lower() == "end"
        ),
        len(tokens),
    )
    keyword_indices = {
        token_index
        for token_index in range(body_start, end_index)
        if not tokens[token_index].quoted
        and tokens[token_index].value.lower() in _NEB_FILE_REFERENCE_KEYS
    }
    return keyword_indices, end_index == len(tokens)


def _require_output_basename(value: str, line: str) -> None:
    name = value.strip()
    if not name or name in {".", ".."} or name.startswith("~") or any(c in name for c in "/\\"):
        raise ValueError(f"ORCA output file name must be a plain basename: {line.strip()}")


def _output_file_name_spans(lines: list[str]) -> set[tuple[int, int, int]]:
    """``(line, start, end)`` of quoted names ORCA writes into its working directory.

    Every quoted ``%plots`` value is a file argument (``MO("orb.cube",1,0);``,
    ``ElDens("dens.cube");``); in ``%md`` a quoted value after a ``Filename``
    key is (``Dump ... Filename "traj.xyz"``). Each must be a plain basename so
    ORCA cannot write outside the generation directory.
    """

    spans: set[tuple[int, int, int]] = set()
    for block in iter_blocks(lines, "plots"):
        for row in block.rows:
            line = lines[row.line_index]
            for token in row.tokens:
                if token.quoted:
                    _require_output_basename(token.value, line)
                    spans.add((row.line_index, token.start, token.end))
                    continue
                for match in _EMBEDDED_QUOTED_RE.finditer(token.value):
                    _require_output_basename(match.group(2), line)
                if any(quote in _EMBEDDED_QUOTED_RE.sub("", token.value) for quote in "\"'"):
                    raise ValueError(
                        f"ORCA output file name must be a plain basename: {line.strip()}"
                    )
    for block in iter_blocks(lines, "md"):
        for row in block.rows:
            tokens = row.tokens
            for key_index, key in enumerate(tokens):
                if key.quoted or key.value.lower() != "filename":
                    continue
                value_index = value_token_index(tokens, key_index)
                if value_index < len(tokens) and tokens[value_index].quoted:
                    value = tokens[value_index]
                    _require_output_basename(value.value, lines[row.line_index])
                    spans.add((row.line_index, value.start, value.end))
    return spans


def _classify_token(
    tokens: list[OrcaLineToken],
    token_index: int,
    *,
    neb_keyword_indices: set[int],
    reference_value_indices: set[int],
) -> tuple[str, str | None]:
    """``(keyword, reference kind)`` of one unquoted token, for both scanner passes.

    ``keyword`` is the lowercased token, with ``% name`` read as ``%name``. The
    kind is that of the file named by the token's value, or ``None`` when the
    token names no file: a leading ``%moinp``/``%pointcharges``, a block file
    key, or an official ``%neb`` file key that is not itself a reference value.
    """

    word = tokens[token_index].value.lower()
    spaced = token_index == 1 and tokens[0].value == "%"
    keyword = f"%{word}" if spaced else word
    if keyword in _SIMPLE_FILE_REFERENCE_KEYS and (token_index == 0 or spaced):
        return keyword, "auxiliary"
    if word in _BLOCK_FILE_REFERENCE_KEYS:
        return keyword, "auxiliary"
    if token_index in neb_keyword_indices and token_index not in reference_value_indices:
        return keyword, "neb_geometry"
    return keyword, None


def scan_orca_file_references(lines: list[str]) -> list[OrcaFileReference]:
    """Every external file reference of an ORCA input, or a fail-closed error.

    Execution binding materializes each returned reference in the generation
    and rewrites it to the private copy; claim-time verification rescans the
    bound input and compares. The ``* xyzfile`` geometry counts as a reference.

    Each line is read in two passes that classify tokens with one rule
    (:func:`_classify_token`). The first marks the value token of every file
    directive, so the second never reads a file name as a directive, and it
    appends each reference and rejects the rest. Raises ``ValueError`` for
    unsupported auxiliary/external-program directives, quoted file-path values
    of keywords it does not bind, ``%plots``/``%md`` output names that are not
    plain basenames, malformed references, and more than
    ``MAX_ORCA_INPUT_REFERENCES`` references.
    """
    moinp_references = orca_moinp_references(lines)
    output_name_spans = _output_file_name_spans(lines)
    moinp_by_line: dict[int, list[OrcaFileReference]] = {}
    for reference in moinp_references:
        moinp_by_line.setdefault(reference.line_index, []).append(reference)
    references: list[OrcaFileReference] = []
    in_neb_block = False
    for line_index, line in enumerate(lines):
        tokens = orca_line_tokens(line)
        semantic_moinp_value_indices = {
            token_index
            for token_index, token in enumerate(tokens)
            for reference in moinp_by_line.get(line_index, [])
            if (token.start, token.end) == (reference.start, reference.end)
        }
        references.extend(moinp_by_line.get(line_index, []))
        neb_keyword_indices, in_neb_block = neb_file_reference_context(
            tokens,
            in_neb_block=in_neb_block,
        )
        compact_active = "".join(token.value.lower() for token in tokens if not token.quoted)
        if "gcp(file)" in compact_active:
            raise ValueError("Unsupported ORCA auxiliary or external program directive: GCP(FILE)")
        reference_value_indices: set[int] = set()
        reference_value_indices.update(semantic_moinp_value_indices)
        geometry_token = xyzfile_reference_token(tokens)
        if geometry_token is not None:
            # Marked so the second pass never misreads a geometry file named
            # like a directive (``progress.xyz``).
            reference_value_indices.add(4)
            references.append(
                OrcaFileReference(
                    line_index=line_index,
                    value=geometry_token.value,
                    start=geometry_token.start,
                    end=geometry_token.end,
                    kind="geometry",
                )
            )
        for token_index, token in enumerate(tokens):
            if token.quoted:
                continue
            _keyword, kind = _classify_token(
                tokens,
                token_index,
                neb_keyword_indices=neb_keyword_indices,
                reference_value_indices=reference_value_indices,
            )
            value_index = value_token_index(tokens, token_index)
            if kind is not None and value_index < len(tokens):
                reference_value_indices.add(value_index)
        for token_index, token in enumerate(tokens):
            if token.quoted:
                if (
                    token_index not in reference_value_indices
                    and (line_index, token.start, token.end) not in output_name_spans
                    and _FILE_PATH_VALUE_RE.search(token.value.strip())
                ):
                    raise ValueError(f"Unsupported ORCA file reference: {line.strip()}")
                continue
            keyword, kind = _classify_token(
                tokens,
                token_index,
                neb_keyword_indices=neb_keyword_indices,
                reference_value_indices=reference_value_indices,
            )
            normalized_keyword = keyword.lstrip("%!")
            value_index = value_token_index(tokens, token_index)
            if (
                normalized_keyword == "gcpmethod"
                and value_index < len(tokens)
                and tokens[value_index].value.strip().lower() == "file"
            ):
                raise ValueError(
                    "Unsupported ORCA auxiliary or external program directive: GCPMETHOD file"
                )
            if token_index not in reference_value_indices and (
                normalized_keyword in _UNSUPPORTED_EXTERNAL_HOOK_KEYS
                or normalized_keyword.startswith("prog")
            ):
                raise ValueError(
                    f"Unsupported ORCA auxiliary or external program directive: {keyword}"
                )
            if keyword in _UNSUPPORTED_FILE_REFERENCE_KEYS:
                raise ValueError(f"Unsupported ORCA auxiliary file directive: {keyword}")
            if kind is None or value_index in semantic_moinp_value_indices:
                continue
            references.append(_value_reference(line_index, line, tokens, value_index, kind))
    if len(references) > MAX_ORCA_INPUT_REFERENCES:
        raise ValueError(
            f"ORCA input has more than {MAX_ORCA_INPUT_REFERENCES} external file references"
        )
    return references


__all__ = [
    "MAX_ORCA_INPUT_REFERENCES",
    "MOINP_RE",
    "OrcaFileReference",
    "checkpoint_file_looks_intact",
    "nonempty_file",
    "orca_input_requests_moread",
    "orca_moinp_references",
    "scan_orca_file_references",
    "set_moinp",
]
