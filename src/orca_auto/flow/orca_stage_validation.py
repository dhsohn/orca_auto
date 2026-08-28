"""Canonical validation for durable ORCA workflow stage inputs."""

from __future__ import annotations

import math
import re
from pathlib import Path

from orca_auto.core.geometry_limits import MAX_ADMISSION_ATOMS
from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.core.utils import normalize_text
from orca_auto.orca.completion_rules import IRC_ROUTE_RE, OPT_ROUTE_RE, TS_ROUTE_RE
from orca_auto.orca.input_blocks import (
    GEOM_HEADER_RE,
    active_orca_line_text,
    geometry_range,
    orca_line_tokens,
    orca_route_line,
    orca_route_tokens,
    validate_supported_xyz_geometry_syntax,
)
from orca_auto.orca.job_type import FREQ_RE
from orca_auto.orca.scants import validate_scan_coordinate_lines

from .contracts.workflow import SUPPORTED_WORKFLOW_ORCA_TASK_KINDS
from .xyz_utils import validated_xyz_atom_count

_ORCA_ROUTE_MARKER_PREFIXES = ("!", "%", "*", "$")
_SP_NON_ENERGY_ROUTE_TOKEN_RE = re.compile(r"\A(?:ENGRAD|NUMGRAD|MD)\Z", re.IGNORECASE)
_NEB_ROUTE_TOKEN_RE = re.compile(
    r"\A(?:ZOOM-)?NEB(?:-[A-Z0-9_-]+)?\Z",
    re.IGNORECASE,
)
_GOAT_ROUTE_TOKEN_RE = re.compile(r"\AGOAT[A-Z0-9_-]*\Z", re.IGNORECASE)
_INLINE_XYZ_ATOM_LABEL_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_:+().{}\[\]-]*\Z")
_INLINE_XYZ_COORDINATE_RE = re.compile(r"\A[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?\Z")


def validate_workflow_orca_task_kind(task_kind: str) -> str:
    """Return one supported durable ORCA workflow task kind, or fail closed."""

    normalized_kind = normalize_text(task_kind).lower()
    if normalized_kind not in SUPPORTED_WORKFLOW_ORCA_TASK_KINDS:
        raise ValueError(
            "unsupported workflow ORCA task_kind: "
            f"{normalized_kind or normalize_text(task_kind)!r}; "
            f"expected one of {sorted(SUPPORTED_WORKFLOW_ORCA_TASK_KINDS)!r}"
        )
    return normalized_kind


def validate_workflow_orca_route(*, task_kind: str, route_line: str) -> str:
    """Require the ORCA run type promised by a durable workflow task role."""

    normalized_kind = validate_workflow_orca_task_kind(task_kind)
    rendered_route = ensure_route_line(route_line)
    normalized_route = rendered_route

    route_tokens = tuple(
        token.value
        for line in normalized_route.splitlines()
        for token in orca_route_tokens(line)
        if not token.quoted
    )
    has_ts = any(TS_ROUTE_RE.fullmatch(token) is not None for token in route_tokens)
    has_opt = any(OPT_ROUTE_RE.fullmatch(token) is not None for token in route_tokens)
    has_freq = any(FREQ_RE.fullmatch(token) is not None for token in route_tokens)
    if normalized_kind == "optts_freq":
        folded_tokens = {token.casefold() for token in route_tokens}
        valid = (
            "optts" in folded_tokens
            and has_freq
            and "scants" not in folded_tokens
            and "neb-ts" not in folded_tokens
        )
        requirement = (
            "exact OptTS and a supported frequency-analysis route token without ScanTS or NEB-TS"
        )
    elif normalized_kind == "sp":
        forbidden_tokens = [
            token
            for token in route_tokens
            if OPT_ROUTE_RE.fullmatch(token) is not None
            or TS_ROUTE_RE.fullmatch(token) is not None
            or FREQ_RE.fullmatch(token) is not None
            or IRC_ROUTE_RE.fullmatch(token) is not None
            or _SP_NON_ENERGY_ROUTE_TOKEN_RE.fullmatch(token) is not None
            or _NEB_ROUTE_TOKEN_RE.fullmatch(token) is not None
            or _GOAT_ROUTE_TOKEN_RE.fullmatch(token) is not None
        ]
        valid = not forbidden_tokens
        requirement = (
            "a pure single-point route without non-energy run types; "
            f"forbidden_tokens={forbidden_tokens!r}"
        )
    else:
        valid = has_opt and not has_ts
        requirement = "a non-TS geometry optimization"
    if not valid:
        raise ValueError(
            "workflow ORCA route-role mismatch: "
            f"task_kind={normalized_kind!r} requires {requirement}; "
            f"route_line={normalized_route!r}"
        )
    return normalized_route


def _validate_workflow_orca_input_lines(
    *,
    task_kind: str,
    normalized_kind: str,
    inp_path: Path,
    input_lines: list[str],
) -> str:
    raw_route_lines = [line for line in input_lines if orca_route_line(line) is not None]
    try:
        for line in raw_route_lines:
            _validate_workflow_route_field_line(line)
    except ValueError as exc:
        raise ValueError(
            "workflow ORCA route-role mismatch: "
            f"task_kind={normalized_kind!r} has an invalid active route line: {exc}; "
            f"inp_path={str(inp_path)!r}"
        ) from exc
    route_lines = [orca_route_line(line) for line in raw_route_lines]
    if not raw_route_lines or any(route is None for route in route_lines):
        raise ValueError(
            "workflow ORCA route-role mismatch: "
            f"task_kind={normalized_kind!r} requires a readable selected input "
            f"with an active route line; inp_path={str(inp_path)!r}"
        )
    route_line = "\n".join(route for route in route_lines if route is not None)
    validated_route = validate_workflow_orca_route(task_kind=task_kind, route_line=route_line)
    if normalized_kind == "relaxed_scan":
        try:
            atom_count = _workflow_orca_input_atom_count(inp_path, input_lines)
            validate_scan_coordinate_lines(input_lines, atom_count=atom_count)
        except ValueError as exc:
            raise ValueError(
                "workflow ORCA route-role mismatch: "
                "task_kind='relaxed_scan' requires a %geom Scan block with a valid "
                f"coordinate: {exc}; inp_path={str(inp_path)!r}"
            ) from exc
    return validated_route


def validate_workflow_orca_input_bytes(
    *,
    task_kind: str,
    inp_path: Path,
    input_bytes: bytes,
) -> str:
    """Validate the exact selected-input bytes bound into a workflow snapshot."""

    normalized_kind = validate_workflow_orca_task_kind(task_kind)
    try:
        input_lines = input_bytes.decode("utf-8", errors="strict").splitlines()
    except UnicodeError:
        input_lines = []
    return _validate_workflow_orca_input_lines(
        task_kind=task_kind,
        normalized_kind=normalized_kind,
        inp_path=inp_path,
        input_lines=input_lines,
    )


def validate_workflow_orca_input(*, task_kind: str, inp_path: Path | None) -> str:
    """Validate every active route line in a materialized workflow input."""

    normalized_kind = validate_workflow_orca_task_kind(task_kind)
    if inp_path is None:
        raise ValueError(
            "workflow ORCA route-role mismatch: "
            f"task_kind={normalized_kind!r} requires a readable selected input "
            "with an active route line; selected input path is missing"
        )
    try:
        input_bytes = read_stable_regular_file(inp_path)
    except (OSError, ValueError):
        input_bytes = b""
    return validate_workflow_orca_input_bytes(
        task_kind=task_kind,
        inp_path=inp_path,
        input_bytes=input_bytes,
    )


def _workflow_orca_input_atom_count(inp_path: Path, lines: list[str]) -> int:
    """Atom count bound to the selected input's one supported XYZ geometry."""

    validate_supported_xyz_geometry_syntax(lines, label="workflow ORCA selected input")
    header_entry = next(
        (
            (line, match)
            for line in lines
            if (match := GEOM_HEADER_RE.match(line.strip())) is not None
        ),
        None,
    )
    if header_entry is None:
        raise ValueError("selected input must define exactly one supported XYZ geometry")
    header_line, header = header_entry
    if header.group(1).lower() == "xyzfile":
        tokens = orca_line_tokens(header_line)
        reference = Path(tokens[4].value).expanduser()
        if not reference.is_absolute():
            reference = inp_path.parent / reference
        return validated_xyz_atom_count(reference)

    geometry = geometry_range(lines)
    if geometry is None:
        raise ValueError("selected input must define exactly one supported XYZ geometry")
    start, end, _charge, _multiplicity = geometry
    atom_rows = [line for line in lines[start + 1 : end - 1] if line.strip()]
    for atom_row in atom_rows:
        atom_tokens = atom_row.split()
        if len(atom_tokens) != 4 or _INLINE_XYZ_ATOM_LABEL_RE.fullmatch(atom_tokens[0]) is None:
            raise ValueError("inline XYZ geometry contains an invalid atom row")
        if any(_INLINE_XYZ_COORDINATE_RE.fullmatch(value) is None for value in atom_tokens[1:]):
            raise ValueError("inline XYZ geometry contains invalid coordinates")
        coordinates = tuple(
            float(value.replace("d", "e").replace("D", "E")) for value in atom_tokens[1:]
        )
        if not all(math.isfinite(value) for value in coordinates):
            raise ValueError("inline XYZ geometry contains non-finite coordinates")
    atom_count = len(atom_rows)
    if atom_count < 1:
        raise ValueError("inline XYZ geometry must contain at least one atom")
    if atom_count > MAX_ADMISSION_ATOMS:
        raise ValueError(
            f"ORCA molecule exceeds the server atom-count limit of {MAX_ADMISSION_ATOMS}"
        )
    return atom_count


def ensure_route_line(route_line: str) -> str:
    """Return only canonical active route lines from a workflow route field.

    A one-line route may omit its leading ``!`` for backwards compatibility.
    Multiline values must use an explicit ``!`` on every active line. Blank and
    comment-only lines are discarded, while any other active ORCA input line is
    rejected so validation and rendering can never observe different programs.
    """

    if not isinstance(route_line, str):
        raise ValueError(
            f"workflow ORCA route-role mismatch: route_line must be a string; got={route_line!r}"
        )

    raw_value = route_line.strip()
    active_lines = [
        raw_line for raw_line in raw_value.splitlines() if active_orca_line_text(raw_line).strip()
    ]
    if len(active_lines) == 1 and orca_route_line(active_lines[0]) is None:
        active_text = active_orca_line_text(active_lines[0]).strip()
        if active_text.startswith(("%", "*", "$")):
            raise ValueError(
                "workflow ORCA route-role mismatch: route_line may contain only active "
                f"'!' route lines; got={active_text!r}"
            )
        _validate_workflow_route_field_line(active_lines[0])
        active_lines = [f"! {active_text}"]

    canonical: list[str] = []
    for raw_line in active_lines:
        active_route = orca_route_line(raw_line)
        if active_route is None:
            raise ValueError(
                "workflow ORCA route-role mismatch: route_line may contain only active "
                f"'!' route lines; got={active_orca_line_text(raw_line).strip()!r}"
            )
        _validate_workflow_route_field_line(raw_line)
        canonical.append(active_route)
    if not canonical:
        raise ValueError("workflow ORCA route-role mismatch: route_line has no active route")
    return "\n".join(canonical)


def _validate_workflow_route_field_line(raw_line: str) -> None:
    tokens = orca_line_tokens(raw_line)
    if any(token.quoted for token in tokens):
        raise ValueError("workflow ORCA route fields do not support quoted tokens")
    if not tokens:
        return
    payload_tokens = orca_route_tokens(raw_line) if tokens[0].value.startswith("!") else tokens
    invalid = next(
        (
            token.value
            for token in payload_tokens
            if token.value.startswith(_ORCA_ROUTE_MARKER_PREFIXES)
        ),
        "",
    )
    if invalid:
        raise ValueError(
            f"workflow ORCA route field contains a marker-prefixed payload token: {invalid!r}"
        )


__all__ = [
    "ensure_route_line",
    "validate_workflow_orca_input",
    "validate_workflow_orca_input_bytes",
    "validate_workflow_orca_route",
    "validate_workflow_orca_task_kind",
]
