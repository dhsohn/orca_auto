"""Remote-ingress admission policy for uploaded run directories.

Every check here runs against an untrusted upload before anything is queued:
server-owned resource caps, atom-count ceilings, the CREST cost policy, and
the ORCA file-reference confinement walker. The walker is deliberately a
security-policy validator rather than a discovery scanner: it layers
shell-metacharacter rejection, remote-disabled feature bans, and traversal
rejection over the shared tokenizers from ``orca_auto.orca.input_blocks``.
Tests pin the directive-key tables here against the scanner's key sets so a
new scanner-known file-reference key cannot silently open a remote gap.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from orca_auto.core.engine_process import atomic_write_confined_bytes
from orca_auto.core.geometry_limits import MAX_REMOTE_ADMISSION_ATOMS

from ..manifest import INTERACTION_ENERGY_MAX_FRAGMENTS_CAP
from ..xyz_utils import validated_xyz_atom_count

_REMOTE_WORKFLOW_COUNT_LIMITS = {
    "max_orca_stages": 20,
    "max_crest_candidates": 8,
    "max_xtb_stages": 8,
    "max_candidates": 20,
    "max_scan_extensions": 4,
    "max_xtb_handoff_retries": 4,
    # Caps the interaction-energy fragment fan-out an uploaded manifest may
    # declare; the materializer enforces the same ceiling on real stage counts.
    "max_fragments": INTERACTION_ENERGY_MAX_FRAGMENTS_CAP,
}
# sp_route_line carries a real ORCA route into a fragment single point, so it
# MUST be scanned for forbidden identifiers / '%' / core caps like every other
# route-line key. Omitting it would be a remote arbitrary-ORCA-feature bypass.
_REMOTE_ROUTE_LINE_KEYS = frozenset(
    {"route_line", "orca_route_line", "orca_optts_route_line", "sp_route_line"}
)
_REMOTE_DISABLED_CREST_COST_KEYS = frozenset(
    {
        "mdlen",
        "len",
        "tstep",
        "allow_high_tstep",
        "mddump",
        "max_md_steps",
        "allow_high_cost_md",
        "max_dump_frames",
        "allow_high_volume_md",
    }
)
_REMOTE_DISABLED_XTB_COST_KEYS = frozenset({"max_ranking_evaluations", "allow_high_cost_ranking"})
_REMOTE_SCAN_POINTS_LIMIT = 200
_REMOTE_CREST_MDLEN_PS = 5.0
_REMOTE_CREST_MAX_ATOM_MD_WORK_UNITS = 50_000_000
_REMOTE_CREST_MAX_TRAJECTORIES = 140
_REMOTE_SCAN_COORDINATE_RE = re.compile(
    r"\A\s*(?P<kind>[BADbad])\s+(?P<atoms>\d+(?:\s+\d+){1,3})\s*=\s*"
    r"(?P<start>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s*,\s*"
    r"(?P<end>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s*,\s*"
    r"(?P<points>\d+)\s*\Z"
)
REMOTE_ORCA_SIMPLE_INPUT_REFERENCE_KEYS = frozenset({"%moinp"})
REMOTE_ORCA_BLOCK_INPUT_REFERENCE_KEYS = frozenset(
    {
        "hessfile",
        "hess_filename",
        "inhessname",
        "ircinithess",
        "moinp",
    }
)
# NOTE: "neb_restart_gbwname" is shadowed at runtime — the forbidden-identifier
# sweep bans every "neb"-prefixed keyword before this table's contained-output
# validation branch can run, so its entry here never executes. It is kept for
# symmetry with the execution scanner's key set; relaxing the "neb" prefix ban
# would activate (and then require re-verifying) this validation path.
REMOTE_ORCA_BLOCK_CONTAINED_OUTPUT_KEYS = frozenset({"neb_restart_gbwname", "restart_gbw_basename"})
REMOTE_ORCA_FORBIDDEN_IDENTIFIERS = frozenset(
    {
        "%compound",
        "%compound_file",
        "%cclib",
        "%ljcoefficients",
        "%pointcharges",
        "$new_job",
        "$newjob",
        "compound",
        "compound_file",
        "cclib",
        "ext_params",
        "ext_args",
        "extargs",
        "extparams",
        "extopt",
        "frag1_methodfile",
        "frag1methodfile",
        "frag2_methodfile",
        "frag2methodfile",
        "gtoauxcname",
        "gtoauxjname",
        "gtoauxjkname",
        "gtoauxname",
        "gtoname",
        "ljcoefficients",
        "neb_end_pdbfile",
        "orcafffilename",
        "openfile",
        "progext",
        "progplot",
        "pointcharges",
        "product_pdbfile",
        "qm2customfile",
        "qm2_customfile",
        "readfragaux",
        "readfragauxc",
        "readfragauxj",
        "readfragauxjk",
        "readfragbasis",
        "readfragecp",
        "restart_allxyzfile",
        "sys_cmd",
        "write2file",
        "ts_pdbfile",
        "xtbinputstring",
        "xtbinputstring2",
        "xtbparamfile",
    }
)
_REMOTE_ORCA_IDENTIFIER_RE = re.compile(r"\A[%$A-Za-z_][%$A-Za-z0-9_]*")
_REMOTE_ORCA_SAFE_PATH_COMPONENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}\Z")
_REMOTE_ORCA_SHELL_META = frozenset("$`;|&<>")


def remote_orca_identifier_is_forbidden(identifier: str) -> bool:
    normalized = identifier.strip().lower()
    return (
        normalized in REMOTE_ORCA_FORBIDDEN_IDENTIFIERS
        or normalized.startswith("%compound")
        or normalized.startswith("compound_file")
        or normalized.startswith("neb")
        or (normalized.startswith("prog") and len(normalized) > len("prog"))
    )


def uploaded_flow_manifest(job_dir: Path) -> dict[str, Any]:
    from orca_auto.flow.manifest import load_flow_manifest

    try:
        return load_flow_manifest(job_dir)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize parser/filesystem failures
        raise ValueError("uploaded flow.yaml could not be safely parsed") from exc


def trusted_upload_resource_limits(orca_config: str | None) -> tuple[int, int]:
    """Load server-owned per-task limits used for all remote submissions."""

    from orca_auto.core.config.schema import CommonResourceConfig
    from orca_auto.orca.config import load_config

    config_path = str(orca_config or "").strip()
    resources = load_config(config_path).resources if config_path else CommonResourceConfig()
    return (
        max(1, int(resources.max_cores_per_task)),
        max(1, int(resources.max_memory_gb_per_task)),
    )


def validate_remote_xyz_atom_limits(job_dir: Path) -> int:
    resolved_root = job_dir.expanduser().resolve()
    largest_atom_count = 0
    for candidate in sorted(job_dir.rglob("*")):
        if candidate.suffix.lower() != ".xyz":
            continue
        if candidate.is_symlink():
            raise ValueError(f"uploaded XYZ input must not be a symlink: {candidate.name}")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
            raise ValueError(f"uploaded XYZ input escapes the run directory: {candidate.name}")
        largest_atom_count = max(
            largest_atom_count,
            validated_xyz_atom_count(
                resolved,
                max_atoms=MAX_REMOTE_ADMISSION_ATOMS,
            ),
        )
    return largest_atom_count


def apply_remote_workflow_crest_policy(job_dir: Path, *, atom_count: int) -> None:
    manifest = uploaded_flow_manifest(job_dir)
    workflow_type = str(manifest.get("workflow_type") or "").strip().lower()
    if workflow_type == "scan_ts_search":
        return
    raw_crest = manifest.get("crest")
    if raw_crest is None:
        crest: dict[str, Any] = {}
    elif isinstance(raw_crest, dict):
        crest = dict(raw_crest)
    else:
        raise ValueError("flow.yaml crest must be a mapping")
    from orca_auto.flow.engines.crest.runner import default_timestep_fs

    timestep_fs = default_timestep_fs(crest.get("gfn"))
    estimated_steps = math.ceil(
        ((_REMOTE_CREST_MDLEN_PS * 1000.0) / timestep_fs) * _REMOTE_CREST_MAX_TRAJECTORIES
    )
    estimated_work_units = atom_count * estimated_steps
    if estimated_work_units > _REMOTE_CREST_MAX_ATOM_MD_WORK_UNITS:
        raise ValueError(
            "uploaded workflow molecule size and CREST MD policy exceed the remote "
            f"work-unit ceiling of {_REMOTE_CREST_MAX_ATOM_MD_WORK_UNITS} atom-steps"
        )
    crest["mdlen"] = _REMOTE_CREST_MDLEN_PS
    manifest["crest"] = crest
    try:
        serialized_manifest = json.dumps(
            manifest,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "uploaded flow.yaml must contain only JSON-compatible scalar values"
        ) from exc
    atomic_write_confined_bytes(
        job_dir,
        job_dir / "flow.yaml",
        (serialized_manifest + "\n").encode("utf-8"),
        label="remote workflow policy manifest",
    )


def validate_remote_orca_inline_atom_limits(inp_path: Path, lines: list[str]) -> None:
    from orca_auto.orca.input_blocks import (
        GEOM_HEADER_RE,
        validate_supported_xyz_geometry_syntax,
    )

    validate_supported_xyz_geometry_syntax(lines, label=inp_path.name)

    cursor = 0
    while cursor < len(lines):
        match = GEOM_HEADER_RE.match(lines[cursor].strip())
        cursor += 1
        if match is None or match.group(1).lower() == "xyzfile":
            continue
        atom_count = 0
        while cursor < len(lines) and lines[cursor].strip() != "*":
            if lines[cursor].strip():
                atom_count += 1
                if atom_count > MAX_REMOTE_ADMISSION_ATOMS:
                    raise ValueError(
                        f"{inp_path.name} exceeds the remote atom-count limit of "
                        f"{MAX_REMOTE_ADMISSION_ATOMS}"
                    )
            cursor += 1


def validate_orca_resource_limits(
    job_dir: Path,
    *,
    max_cores: int,
    max_memory_gb: int,
) -> None:
    """Reject standalone inputs whose explicit directives exceed server caps."""

    from orca_auto.orca.input_blocks import active_orca_line_text, orca_route_line
    from orca_auto.orca.resource_directives import MAXCORE_RE, NPROCS_RE, PAL_ROUTE_RE

    for inp_path in sorted(job_dir.glob("*.inp")):
        lines = inp_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        validate_remote_orca_inline_atom_limits(inp_path, lines)
        validate_orca_file_references(job_dir, inp_path, lines)
        core_requests: list[int] = []
        maxcore_requests: list[int] = []
        for line in lines:
            active_line = active_orca_line_text(line)
            normalized_line = re.sub(
                r"\A(?P<indent>\s*)%\s+(?=[A-Za-z])",
                r"\g<indent>%",
                active_line,
                count=1,
            )
            core_requests.extend(
                int(match.group(1)) for match in NPROCS_RE.finditer(normalized_line)
            )
            route = orca_route_line(line)
            if route is not None:
                core_requests.extend(int(match.group(1)) for match in PAL_ROUTE_RE.finditer(route))
            maxcore_match = MAXCORE_RE.match(normalized_line)
            if maxcore_match:
                maxcore_requests.append(int(maxcore_match.group(1)))

        requested_cores = max(core_requests, default=0) or None
        if requested_cores is not None and requested_cores > max_cores:
            raise ValueError(
                f"{inp_path.name} requests {requested_cores} cores; server limit is {max_cores}"
            )
        maxcore_mb = max(maxcore_requests, default=0)
        if maxcore_mb <= 0:
            continue
        effective_cores = requested_cores or max_cores
        requested_memory_gb = max(
            1,
            (effective_cores * maxcore_mb + 1023) // 1024,
        )
        if requested_memory_gb > max_memory_gb:
            raise ValueError(
                f"{inp_path.name} requests about {requested_memory_gb} GiB; "
                f"server limit is {max_memory_gb} GiB"
            )


def validate_orca_file_references(
    job_dir: Path,
    inp_path: Path,
    lines: list[str],
) -> None:
    """Confine remote ORCA input/output paths to the uploaded run directory."""

    from orca_auto.orca.input_blocks import (
        OrcaLineToken,
        orca_line_tokens,
        orca_route_tokens,
    )

    resolved_root = job_dir.resolve()

    def normalized_path_text(value: str, *, raw_token: str) -> str:
        text = value.strip()
        raw = raw_token.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
            raw = raw[1:-1]
        if not text or any(ord(character) < 32 for character in text):
            raise ValueError(f"{inp_path.name} contains an invalid file reference")
        # Backslashes are ambiguous because ORCA's quoting rules and the
        # POSIX filesystem do not agree on whether they escape or separate
        # path components. Remote ingress accepts one canonical form only.
        if "\\" in raw:
            raise ValueError(f"{inp_path.name} file references must use forward slashes")
        normalized = text.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if (
            normalized.startswith(("~", "$"))
            or pure.is_absolute()
            or (len(normalized) >= 2 and normalized[1] == ":")
            or ".." in pure.parts
        ):
            raise ValueError(
                f"{inp_path.name} file reference escapes the uploaded run directory: {text!r}"
            )
        if any(
            _REMOTE_ORCA_SAFE_PATH_COMPONENT_RE.fullmatch(component) is None
            for component in pure.parts
        ):
            raise ValueError(f"{inp_path.name} contains an unsafe ORCA filename: {text!r}")
        return normalized

    def validate_reference(
        value: str,
        *,
        raw_token: str,
        directive: str,
        must_exist: bool,
        single_component: bool = False,
    ) -> Path:
        normalized = normalized_path_text(value, raw_token=raw_token)
        pure = PurePosixPath(normalized)
        if not pure.parts or pure == PurePosixPath("."):
            raise ValueError(f"{inp_path.name} has an invalid {directive} file reference")
        if single_component and len(pure.parts) != 1:
            raise ValueError(
                f"{inp_path.name} {directive} must be one shell-safe filename component"
            )
        candidate = (resolved_root / Path(*pure.parts)).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(
                f"{inp_path.name} {directive} escapes the uploaded run directory"
            ) from exc
        if must_exist:
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError(f"{inp_path.name} {directive} file is missing: {normalized!r}")
            return candidate
        if candidate.is_symlink() or not candidate.parent.is_dir():
            raise ValueError(
                f"{inp_path.name} {directive} output path is unavailable: {normalized!r}"
            )
        return candidate

    def value_token_after(
        tokens: Sequence[OrcaLineToken],
        index: int,
        directive: str,
    ) -> OrcaLineToken:
        value_index = index + 1
        if value_index < len(tokens) and tokens[value_index].value == "=":
            value_index += 1
        if value_index >= len(tokens):
            raise ValueError(f"{inp_path.name} has an invalid {directive} file reference")
        value_token = tokens[value_index]
        if not value_token.value.strip() or (
            not value_token.quoted and value_token.value.lower() == "end"
        ):
            raise ValueError(f"{inp_path.name} has an invalid {directive} file reference")
        return value_token

    for line in lines:
        if "\x00" in line:
            raise ValueError(f"{inp_path.name} contains a NUL byte")
        tokens = orca_line_tokens(line)
        if not tokens:
            continue
        if any(
            character in _REMOTE_ORCA_SHELL_META for token in tokens for character in token.value
        ):
            raise ValueError(f"{inp_path.name} contains shell metacharacters not accepted remotely")
        spaced_percent_keyword = ""
        if len(tokens) >= 2 and tokens[0].value == "%" and not tokens[1].quoted:
            spaced_percent_keyword = f"%{tokens[1].value.lower()}"
        for route_token in orca_route_tokens(line):
            if route_token.quoted:
                continue
            route_identifier_match = _REMOTE_ORCA_IDENTIFIER_RE.match(route_token.value)
            route_identifier = (
                route_identifier_match.group(0).lower()
                if route_identifier_match is not None
                else ""
            )
            if route_token.value.lower() == "md" or (
                route_identifier and remote_orca_identifier_is_forbidden(route_identifier)
            ):
                raise ValueError(
                    f"{inp_path.name} uses a remote-disabled ORCA feature: "
                    f"{route_identifier or route_token.value.lower()}"
                )
        if tokens[0].value.lower() == "%md" or spaced_percent_keyword == "%md":
            raise ValueError(
                f"{inp_path.name} uses the remote-disabled ORCA molecular dynamics block"
            )
        compact_tokens = "".join(token.value for token in tokens if not token.quoted).lower()
        if "gcp(file)" in compact_tokens:
            raise ValueError(
                f"{inp_path.name} uses a remote-disabled external GCP parameter source"
            )

        geometry_type = ""
        geometry_path_index = -1
        if len(tokens) >= 2 and tokens[0].value == "*":
            geometry_type = tokens[1].value.lower()
            geometry_path_index = 4
        elif tokens[0].value.startswith("*"):
            geometry_type = tokens[0].value[1:].lower()
            geometry_path_index = 3

        reference_value_indices: set[int] = set()
        if geometry_type.endswith("file") and geometry_path_index < len(tokens):
            reference_value_indices.add(geometry_path_index)
        for index, token in enumerate(tokens):
            if token.quoted:
                continue
            keyword = token.value.lower()
            simple_input = (index == 0 and keyword in REMOTE_ORCA_SIMPLE_INPUT_REFERENCE_KEYS) or (
                index == 1 and spaced_percent_keyword in REMOTE_ORCA_SIMPLE_INPUT_REFERENCE_KEYS
            )
            block_input = keyword in REMOTE_ORCA_BLOCK_INPUT_REFERENCE_KEYS
            contained_output = keyword in REMOTE_ORCA_BLOCK_CONTAINED_OUTPUT_KEYS
            output_base = (index == 0 and keyword == "%base") or (
                index == 1 and spaced_percent_keyword == "%base"
            )
            if simple_input or block_input or contained_output or output_base:
                value_token = value_token_after(tokens, index, token.value)
                reference_value_indices.add(tokens.index(value_token))
            if keyword == "gcpmethod":
                value_token = value_token_after(tokens, index, token.value)
                if value_token.value.strip().lower() == "file":
                    raise ValueError(
                        f"{inp_path.name} uses a remote-disabled external GCP parameter source"
                    )

        # Reject unmistakable traversal syntax even for less-common ORCA
        # file directives. Known directives below additionally require the
        # referenced input file to exist in the extracted snapshot.
        for index, token in enumerate(tokens):
            if not token.quoted:
                identifier_match = _REMOTE_ORCA_IDENTIFIER_RE.match(token.value)
                identifier = (
                    identifier_match.group(0).lower() if identifier_match is not None else ""
                )
                if index not in reference_value_indices and remote_orca_identifier_is_forbidden(
                    identifier
                ):
                    raise ValueError(
                        f"{inp_path.name} uses a remote-disabled ORCA feature: {identifier}"
                    )
            raw_token = line[token.start : token.end]
            raw_value = raw_token.strip()
            if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {'"', "'"}:
                raw_value = raw_value[1:-1]
            normalized_raw = raw_value.replace("\\", "/")
            raw_path = PurePosixPath(normalized_raw)
            if (
                normalized_raw.startswith(("/", "~", "$"))
                or (len(normalized_raw) >= 2 and normalized_raw[1] == ":")
                or ".." in raw_path.parts
            ):
                raise ValueError(
                    f"{inp_path.name} contains an external file reference: {token.value!r}"
                )

        if geometry_type.endswith("file"):
            if len(tokens) <= geometry_path_index:
                raise ValueError(f"{inp_path.name} has an invalid {geometry_type} reference")
            if geometry_type != "xyzfile":
                raise ValueError(
                    f"{inp_path.name} uses a remote-disabled geometry format: {geometry_type}"
                )
            value_token = tokens[geometry_path_index]
            geometry_path = validate_reference(
                value_token.value,
                raw_token=line[value_token.start : value_token.end],
                directive=geometry_type,
                must_exist=True,
            )
            validated_xyz_atom_count(
                geometry_path,
                max_atoms=MAX_REMOTE_ADMISSION_ATOMS,
            )

        for index, token in enumerate(tokens):
            if token.quoted:
                continue
            keyword = token.value.lower()
            simple_input = (index == 0 and keyword in REMOTE_ORCA_SIMPLE_INPUT_REFERENCE_KEYS) or (
                index == 1 and spaced_percent_keyword in REMOTE_ORCA_SIMPLE_INPUT_REFERENCE_KEYS
            )
            block_input = keyword in REMOTE_ORCA_BLOCK_INPUT_REFERENCE_KEYS
            contained_output = keyword in REMOTE_ORCA_BLOCK_CONTAINED_OUTPUT_KEYS
            output_base = (index == 0 and keyword == "%base") or (
                index == 1 and spaced_percent_keyword == "%base"
            )
            if not (simple_input or block_input or contained_output or output_base):
                continue
            value_token = value_token_after(tokens, index, token.value)
            validate_reference(
                value_token.value,
                raw_token=line[value_token.start : value_token.end],
                directive=token.value,
                must_exist=not (contained_output or output_base),
                single_component=contained_output or output_base,
            )


def validate_workflow_resource_limits(
    job_dir: Path,
    *,
    max_cores: int,
    max_memory_gb: int,
) -> None:
    """Reject resource overrides above caps anywhere in an uploaded manifest."""

    manifest = uploaded_flow_manifest(job_dir)
    from ..manifest import normalize_interaction_energy_block

    normalize_interaction_energy_block(manifest.get("interaction_energy"))
    limits = {
        "max_cores": max_cores,
        "max_cores_per_task": max_cores,
        "max_memory_gb": max_memory_gb,
        "max_memory_gb_per_task": max_memory_gb,
    }

    # This check is path-sensitive, so perform it directly on the canonical
    # top-level section. A global identity-based traversal can otherwise
    # inspect a YAML-aliased mapping first at a benign path and skip the same
    # object when it later appears under ``crest``.
    crest_manifest = manifest.get("crest")
    if isinstance(crest_manifest, dict):
        for raw_key, item in crest_manifest.items():
            key = str(raw_key).strip()
            if (
                key in _REMOTE_DISABLED_CREST_COST_KEYS
                and item is not None
                and (not isinstance(item, str) or item.strip())
            ):
                raise ValueError(
                    f"flow.yaml crest.{key} is disabled for uploaded workflows; "
                    "CREST runtime and trajectory-volume controls are server-owned"
                )

    for section_name in ("xtb", "xtb_job_manifest"):
        xtb_manifest = manifest.get(section_name)
        if not isinstance(xtb_manifest, dict):
            continue
        for raw_key, item in xtb_manifest.items():
            key = str(raw_key).strip()
            if (
                key in _REMOTE_DISABLED_XTB_COST_KEYS
                and item is not None
                and (not isinstance(item, str) or item.strip())
            ):
                raise ValueError(
                    f"flow.yaml {section_name}.{key} is disabled for uploaded workflows; "
                    "xTB evaluation budgets are server-owned"
                )

    interaction_manifest = manifest.get("interaction_energy")
    if isinstance(interaction_manifest, dict):
        remote_priority = interaction_manifest.get("priority")
        if remote_priority is not None and (
            not isinstance(remote_priority, str) or remote_priority.strip()
        ):
            raise ValueError(
                "flow.yaml interaction_energy.priority is disabled for uploaded workflows; "
                "queue priority is server-owned"
            )

    def validate_route_line(value: object, *, path: str) -> None:
        from orca_auto.orca.resource_directives import PAL_ROUTE_RE

        if not isinstance(value, str):
            raise ValueError(f"flow.yaml {path} must be a single route-line string")
        route = value.strip()
        if (
            not route.startswith("!")
            or len(route) > 500
            or any(character in route for character in ("\r", "\n", "\x00", "%", "#"))
        ):
            raise ValueError(f"flow.yaml {path} is not a safe single ORCA route line")
        unsafe_identifiers = {
            match.group(0).lower()
            for token in route[1:].split()
            if (match := _REMOTE_ORCA_IDENTIFIER_RE.match(token)) is not None
            and (token.lower() == "md" or remote_orca_identifier_is_forbidden(match.group(0)))
        }
        if unsafe_identifiers:
            shown = ", ".join(sorted(unsafe_identifiers))
            raise ValueError(f"flow.yaml {path} uses remote-disabled ORCA features: {shown}")
        if "gcp(file)" in "".join(route.lower().split()):
            raise ValueError(
                f"flow.yaml {path} uses a remote-disabled external GCP parameter source"
            )
        requested = max(
            (int(match.group(1)) for match in PAL_ROUTE_RE.finditer(route)),
            default=0,
        )
        if requested > max_cores:
            raise ValueError(
                f"flow.yaml {path} requests {requested} cores; server limit is {max_cores}"
            )

    def validate_scan_coordinate(value: object, *, path: str) -> None:
        if not isinstance(value, str):
            raise ValueError(f"flow.yaml {path} must be a scan-coordinate string")
        match = _REMOTE_SCAN_COORDINATE_RE.fullmatch(value)
        if match is None:
            raise ValueError(f"flow.yaml {path} is not a safe ORCA scan coordinate")
        kind = match.group("kind").upper()
        atom_count = len(match.group("atoms").split())
        if atom_count != {"B": 2, "A": 3, "D": 4}[kind]:
            raise ValueError(f"flow.yaml {path} has the wrong atom count for {kind}")
        start = float(match.group("start"))
        end = float(match.group("end"))
        points = int(match.group("points"))
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError(f"flow.yaml {path} scan range must be finite")
        if points < 2 or points > _REMOTE_SCAN_POINTS_LIMIT:
            raise ValueError(
                f"flow.yaml {path} requests {points} points; "
                f"server limit is {_REMOTE_SCAN_POINTS_LIMIT}"
            )

    stack: list[tuple[Any, str]] = [(manifest, "")]
    seen_containers: set[int] = set()
    while stack:
        value, path = stack.pop()
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            for raw_key, item in value.items():
                key = str(raw_key).strip()
                item_path = f"{path}.{key}" if path else key
                limit = limits.get(key)
                if limit is not None and item is not None:
                    try:
                        requested = int(item)
                    except (OverflowError, TypeError, ValueError):
                        # The workflow parser owns ordinary type validation.
                        requested = 0
                    if requested > limit:
                        raise ValueError(
                            f"flow.yaml {item_path} requests {requested}; server limit is {limit}"
                        )
                count_limit = _REMOTE_WORKFLOW_COUNT_LIMITS.get(key)
                if count_limit is not None and item is not None:
                    try:
                        requested_count = int(item)
                    except (OverflowError, TypeError, ValueError):
                        requested_count = 0
                    if requested_count > count_limit:
                        raise ValueError(
                            f"flow.yaml {item_path} requests {requested_count}; "
                            f"server limit is {count_limit}"
                        )
                if key in _REMOTE_ROUTE_LINE_KEYS and item is not None:
                    validate_route_line(item, path=item_path)
                if key == "scan_coordinate" and item is not None:
                    validate_scan_coordinate(item, path=item_path)
                stack.append((item, item_path))
        elif isinstance(value, list):
            identity = id(value)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            for index, item in enumerate(value):
                stack.append((item, f"{path}[{index}]"))
