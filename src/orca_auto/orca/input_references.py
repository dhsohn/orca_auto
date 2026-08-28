"""Canonical scanning policy for external ORCA input file references."""

from __future__ import annotations

from . import input_blocks as _input_blocks

MAX_ORCA_INPUT_REFERENCES = 128

_NEB_FILE_REFERENCE_KEYS = frozenset({"product", "ts"})
_SIMPLE_FILE_REFERENCE_KEYS = frozenset({"%moinp", "%pointcharges"})
_BLOCK_FILE_REFERENCE_KEYS = frozenset(
    {
        "hessfile",
        "hess_filename",
        "inhessname",
        "ircinithess",
        "moinp",
        "neb_end_xyzfile",
        "neb_restart_xyzfile",
        "neb_ts_xyzfile",
        "restart_allxyzfile",
    }
)
_UNSUPPORTED_FILE_REFERENCE_KEYS = frozenset(
    {
        "%cclib",
        "%ljcoefficients",
        "neb_end_pdbfile",
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


def neb_file_reference_context(
    tokens: list[_input_blocks.OrcaLineToken],
    *,
    in_neb_block: bool,
) -> tuple[set[int], bool]:
    """Return official ``%neb`` file-key indices and the next block state."""

    body_start = 0
    block_name = ""
    if (
        tokens
        and not tokens[0].quoted
        and tokens[0].value.startswith("%")
        and tokens[0].value != "%"
    ):
        block_name = tokens[0].value[1:].lower()
        body_start = 1
    elif (
        len(tokens) >= 2
        and not tokens[0].quoted
        and tokens[0].value == "%"
        and not tokens[1].quoted
    ):
        block_name = tokens[1].value.lower()
        body_start = 2
    if block_name:
        if block_name != "neb":
            return set(), False
    elif not in_neb_block:
        return set(), False

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


def scan_orca_file_references(
    lines: list[str],
    *,
    include_geometry: bool = True,
) -> list[_input_blocks.OrcaFileReference]:
    """Every external file reference of an ORCA input, or a fail-closed error.

    This is the single scanner shared by execution binding (which binds every
    reference into the generation) and restart rematerialization (which copies
    and rewrites the auxiliary references; it passes ``include_geometry=False``
    because the ``* xyzfile`` geometry line is rewritten separately). Both
    consumers must see the same reference set, or an input accepted at
    execution time silently loses references on restart. The geometry
    reference always counts toward ``MAX_ORCA_INPUT_REFERENCES`` — filtering
    it out of the returned set must not loosen the cap.

    Raises ``ValueError`` for unsupported auxiliary/external-program
    directives, malformed references, and more than
    ``MAX_ORCA_INPUT_REFERENCES`` references.
    """
    moinp_references = _input_blocks.orca_moinp_references(lines)
    moinp_by_line: dict[int, list[_input_blocks.OrcaFileReference]] = {}
    for reference in moinp_references:
        moinp_by_line.setdefault(reference.line_index, []).append(reference)
    references: list[_input_blocks.OrcaFileReference] = []
    in_neb_block = False
    for line_index, line in enumerate(lines):
        tokens = _input_blocks.orca_line_tokens(line)
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
        if len(tokens) >= 5 and tokens[0].value == "*" and tokens[1].value.lower() == "xyzfile":
            value_token = tokens[4]
            # Always mark the filename so the second pass never misreads it as
            # a directive (e.g. a geometry file named ``progress.xyz``), and
            # always collect the reference so the cap below counts it even for
            # callers that filter geometry out of the returned set.
            reference_value_indices.add(4)
            references.append(
                _input_blocks.OrcaFileReference(
                    line_index=line_index,
                    value=value_token.value,
                    start=value_token.start,
                    end=value_token.end,
                    kind="geometry",
                )
            )
        for token_index, token in enumerate(tokens):
            if token.quoted:
                continue
            keyword = token.value.lower()
            spaced_percent_keyword = (
                f"%{keyword}" if token_index == 1 and tokens[0].value == "%" else ""
            )
            effective_keyword = spaced_percent_keyword or keyword
            is_simple = effective_keyword in _SIMPLE_FILE_REFERENCE_KEYS and (
                token_index == 0 or bool(spaced_percent_keyword)
            )
            is_neb_file_directive = (
                token_index in neb_keyword_indices and token_index not in reference_value_indices
            )
            is_value_directive = (
                is_simple or keyword in _BLOCK_FILE_REFERENCE_KEYS or is_neb_file_directive
            )
            is_value_directive = is_value_directive or effective_keyword == "%base"
            if not is_value_directive:
                continue
            value_index = token_index + 1
            if value_index < len(tokens) and tokens[value_index].value == "=":
                value_index += 1
            if value_index < len(tokens):
                reference_value_indices.add(value_index)
        for token_index, token in enumerate(tokens):
            if token.quoted:
                continue
            keyword = token.value.lower()
            spaced_percent_keyword = (
                f"%{keyword}" if token_index == 1 and tokens[0].value == "%" else ""
            )
            effective_keyword = spaced_percent_keyword or keyword
            normalized_keyword = effective_keyword.lstrip("%!")
            if normalized_keyword == "gcpmethod":
                value_index = token_index + 1
                if value_index < len(tokens) and tokens[value_index].value == "=":
                    value_index += 1
                if (
                    value_index < len(tokens)
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
                    f"Unsupported ORCA auxiliary or external program directive: {effective_keyword}"
                )
            if effective_keyword in _UNSUPPORTED_FILE_REFERENCE_KEYS:
                raise ValueError(f"Unsupported ORCA auxiliary file directive: {effective_keyword}")
            is_simple = effective_keyword in _SIMPLE_FILE_REFERENCE_KEYS and (
                token_index == 0 or bool(spaced_percent_keyword)
            )
            is_neb_file_directive = (
                token_index in neb_keyword_indices and token_index not in reference_value_indices
            )
            if (
                not is_simple
                and keyword not in _BLOCK_FILE_REFERENCE_KEYS
                and not is_neb_file_directive
            ):
                continue
            value_index = token_index + 1
            if value_index < len(tokens) and tokens[value_index].value == "=":
                value_index += 1
            if value_index in semantic_moinp_value_indices:
                continue
            if value_index >= len(tokens):
                raise ValueError(f"Invalid ORCA auxiliary file reference: {line.strip()}")
            value_token = tokens[value_index]
            value = value_token.value.strip()
            if not value or (not value_token.quoted and value.lower() == "end"):
                raise ValueError(f"Invalid ORCA auxiliary file reference: {line.strip()}")
            references.append(
                _input_blocks.OrcaFileReference(
                    line_index=line_index,
                    value=value,
                    start=value_token.start,
                    end=value_token.end,
                    kind="neb_geometry" if is_neb_file_directive else "auxiliary",
                )
            )
    if len(references) > MAX_ORCA_INPUT_REFERENCES:
        raise ValueError(
            f"ORCA input has more than {MAX_ORCA_INPUT_REFERENCES} external file references"
        )
    if not include_geometry:
        return [reference for reference in references if reference.kind != "geometry"]
    return references


__all__ = [
    "MAX_ORCA_INPUT_REFERENCES",
    "scan_orca_file_references",
]
