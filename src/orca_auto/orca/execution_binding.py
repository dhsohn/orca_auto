from __future__ import annotations

import io
import secrets
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.engine_process import (
    atomic_write_confined_bytes,
    require_confined_regular_file,
)
from orca_auto.core.geometry_limits import MAX_ADMISSION_ATOMS, MAX_HESSIAN_ADMISSION_ATOMS
from orca_auto.core.queue.engine.input_snapshot import (
    MAX_INPUT_SNAPSHOT_BYTES,
    cleanup_unowned_input_snapshot_namespace,
    input_snapshot_namespace_dir,
    read_stable_regular_file,
    reserve_input_snapshot_namespace,
    snapshot_input_file,
    verify_input_snapshot,
)
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_TOKEN_KEY,
    create_snapshot_intent,
    discard_snapshot_intent_if_generations_absent,
)
from orca_auto.core.utils.persistence import durable_mkdir, fsync_directory

from .input_blocks import (
    GEOM_HEADER_RE,
    orca_line_tokens,
    orca_route_line,
    quote_orca_path,
    validate_supported_xyz_geometry_syntax,
)
from .job_type import FREQ_RE

ORCA_EXECUTION_ROOT_NAME = ".orca_auto_orca_executions"
MAX_ORCA_INPUT_REFERENCES = 128
MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES = 4 * MAX_INPUT_SNAPSHOT_BYTES
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
        "sys_cmd",
        "write2file",
        "xtbinputstring",
        "xtbinputstring2",
        "xtbparamfile",
    }
)


@dataclass(frozen=True)
class _FileReference:
    line_index: int
    value: str
    start: int
    end: int
    kind: str


def _file_references(lines: list[str]) -> list[_FileReference]:
    references: list[_FileReference] = []
    for line_index, line in enumerate(lines):
        tokens = orca_line_tokens(line)
        compact_active = "".join(token.value.lower() for token in tokens if not token.quoted)
        if "gcp(file)" in compact_active:
            raise ValueError("Unsupported ORCA auxiliary or external program directive: GCP(FILE)")
        reference_value_indices: set[int] = set()
        if len(tokens) >= 5 and tokens[0].value == "*" and tokens[1].value.lower() == "xyzfile":
            value_token = tokens[4]
            reference_value_indices.add(4)
            references.append(
                _FileReference(
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
            is_value_directive = is_simple or keyword in _BLOCK_FILE_REFERENCE_KEYS
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
            if not is_simple and keyword not in _BLOCK_FILE_REFERENCE_KEYS:
                continue
            value_index = token_index + 1
            if value_index < len(tokens) and tokens[value_index].value == "=":
                value_index += 1
            if value_index >= len(tokens):
                raise ValueError(f"Invalid ORCA auxiliary file reference: {line.strip()}")
            value_token = tokens[value_index]
            value = value_token.value.strip()
            if not value or (not value_token.quoted and value.lower() == "end"):
                raise ValueError(f"Invalid ORCA auxiliary file reference: {line.strip()}")
            references.append(
                _FileReference(
                    line_index=line_index,
                    value=value,
                    start=value_token.start,
                    end=value_token.end,
                    kind="auxiliary",
                )
            )
    if len(references) > MAX_ORCA_INPUT_REFERENCES:
        raise ValueError(
            f"ORCA input has more than {MAX_ORCA_INPUT_REFERENCES} external file references"
        )
    return references


def _inline_geometry_atom_count(selected_text: str) -> int | None:
    lines = io.StringIO(selected_text)
    for line in lines:
        match = GEOM_HEADER_RE.match(line.strip())
        if match is None:
            continue
        if match.group(1).lower() == "xyzfile":
            return None
        atom_count = 0
        for atom_line in lines:
            stripped = atom_line.strip()
            if stripped == "*":
                break
            if not stripped:
                continue
            atom_count += 1
            if atom_count > MAX_ADMISSION_ATOMS:
                raise ValueError(
                    f"ORCA molecule exceeds the server atom-count limit of {MAX_ADMISSION_ATOMS}"
                )
        return atom_count
    return None


def _route_requests_hessian(lines: list[str]) -> bool:
    route_text = " ".join(route for line in lines if (route := orca_route_line(line)) is not None)
    return bool(FREQ_RE.search(route_text))


def _validated_xyz_atom_count(path: Path, *, max_atoms: int) -> int:
    payload = read_stable_regular_file(path)
    try:
        lines = io.StringIO(payload.decode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ValueError(f"ORCA XYZ geometry must be UTF-8 text: {path}") from exc
    header = ""
    for line in lines:
        header = line.strip()
        if header:
            break
    try:
        atom_count = int(header)
    except ValueError as exc:
        raise ValueError(f"ORCA XYZ geometry has an invalid atom count: {path}") from exc
    if atom_count <= 0:
        raise ValueError(f"ORCA XYZ geometry must contain at least one atom: {path}")
    if atom_count > max_atoms:
        raise ValueError(f"ORCA molecule exceeds the server atom-count limit of {max_atoms}")
    return atom_count


def _reference_source(job_dir: Path, selected_inp: Path, reference: str) -> Path:
    raw_path = Path(reference).expanduser()
    candidate = raw_path if raw_path.is_absolute() else selected_inp.parent / raw_path
    return require_confined_regular_file(
        job_dir,
        candidate,
        label="ORCA referenced input",
    )


def _execution_directory(job_dir: Path, generation_name: str) -> Path:
    execution_parent = job_dir / ORCA_EXECUTION_ROOT_NAME
    if execution_parent.is_symlink():
        raise ValueError(f"ORCA execution snapshot root must not be a symlink: {execution_parent}")
    durable_mkdir(execution_parent, mode=0o700, exist_ok=True)
    if not execution_parent.is_dir() or not execution_parent.resolve().is_relative_to(job_dir):
        raise ValueError("ORCA execution snapshot root escapes the job directory")
    execution_dir = execution_parent / generation_name
    try:
        durable_mkdir(execution_dir, mode=0o700, exist_ok=False)
    except BaseException:
        if execution_dir.is_dir() and not execution_dir.is_symlink():
            shutil.rmtree(execution_dir, ignore_errors=True)
            fsync_directory(execution_parent)
        raise
    return execution_dir.resolve()


def _snapshot_with_budget(
    job_dir: Path,
    source: Path,
    *,
    role: str,
    consumed_bytes: int,
    namespace: str,
) -> tuple[dict[str, Any], int]:
    size = source.stat().st_size
    if size > MAX_INPUT_SNAPSHOT_BYTES:
        raise ValueError(
            f"ORCA input snapshot source exceeds {MAX_INPUT_SNAPSHOT_BYTES} bytes: {source}"
        )
    if consumed_bytes + size > MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES:
        raise ValueError("ORCA submission inputs exceed the aggregate snapshot size limit")
    descriptor = snapshot_input_file(job_dir, source, role=role, namespace=namespace)
    actual_size = int(descriptor.get("size_bytes") or 0)
    if consumed_bytes + actual_size > MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES:
        raise ValueError("ORCA submission inputs exceed the aggregate snapshot size limit")
    return descriptor, consumed_bytes + actual_size


def _private_input_path(
    execution_dir: Path,
    *,
    role: str,
    descriptor: Mapping[str, Any],
    source: Path,
) -> Path:
    suffix = source.suffix.lower()
    digest = str(descriptor.get("sha256") or "")
    return execution_dir / ".inputs" / f"{role}-{digest}{suffix}"


def _write_private_input(
    execution_dir: Path,
    target: Path,
    payload: bytes,
    *,
    label: str,
) -> None:
    durable_mkdir(target.parent, mode=0o700, parents=True, exist_ok=True)
    atomic_write_confined_bytes(
        execution_dir,
        target,
        payload,
        label=label,
        mode=0o400,
    )


def _rewrite_bound_input(
    lines: list[str],
    references: list[_FileReference],
    private_paths: Mapping[Path, Path],
    *,
    job_dir: Path,
    selected_inp: Path,
    execution_dir: Path,
) -> bytes:
    replacements: dict[int, list[tuple[int, int, str]]] = {}
    for reference in references:
        source = _reference_source(job_dir, selected_inp, reference.value)
        private_path = private_paths[source]
        relative = private_path.relative_to(execution_dir).as_posix()
        replacements.setdefault(reference.line_index, []).append(
            (reference.start, reference.end, quote_orca_path(relative))
        )
    rewritten = list(lines)
    for line_index, line_replacements in replacements.items():
        updated = rewritten[line_index]
        for start, end, replacement in sorted(line_replacements, reverse=True):
            updated = updated[:start] + replacement + updated[end:]
        rewritten[line_index] = updated
    return ("\n".join(rewritten).rstrip() + "\n").encode("utf-8")


def _file_identity(path: Path) -> dict[str, Any]:
    identity = _engine_runner.executable_identity(path)
    return {
        "path": identity["path"],
        "sha256": identity["sha256"],
        "size_bytes": identity["size_bytes"],
    }


def build_orca_execution_snapshot(
    job_dir: str | Path,
    selected_inp: str | Path,
    *,
    selected_input_xyz: str,
    resource_request: Mapping[str, int],
    max_retries: int,
    orca_executable: str | Path,
    queue_root: str | Path | None = None,
    snapshot_intent_token: str | None = None,
) -> dict[str, Any]:
    """Create an isolated, immutable input tree for one ORCA queue generation."""

    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("ORCA execution snapshot retry budget must be a nonnegative integer")
    if set(resource_request) != {"max_cores", "max_memory_gb"} or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in resource_request.values()
    ):
        raise ValueError("ORCA execution snapshot resources must be positive integers")
    raw_job_dir = Path(job_dir).expanduser()
    resolved_job_dir = raw_job_dir.resolve()
    if raw_job_dir.is_symlink() or not resolved_job_dir.is_dir():
        raise ValueError(f"ORCA job directory must be a real directory: {job_dir}")
    source_selected = require_confined_regular_file(
        resolved_job_dir,
        Path(selected_inp),
        label="ORCA selected input",
    )
    if source_selected.suffix.lower() != ".inp":
        raise ValueError(f"ORCA selected input must be an .inp file: {source_selected}")

    resolved_queue_root = Path(queue_root or resolved_job_dir).expanduser().resolve()
    resolved_intent_token = snapshot_intent_token or f"snapshot-{secrets.token_hex(16)}"
    generation_name = f"generation-{secrets.token_hex(16)}"
    input_snapshot_namespace = generation_name
    execution_dir = resolved_job_dir / ORCA_EXECUTION_ROOT_NAME / generation_name
    input_generation = resolved_job_dir / ".orca_auto_input_snapshots" / input_snapshot_namespace
    create_snapshot_intent(
        resolved_queue_root,
        token=resolved_intent_token,
        kind="orca_execution_pair",
        generation_paths=[execution_dir, input_generation],
    )
    try:
        execution_dir = _execution_directory(resolved_job_dir, generation_name)
        reserve_input_snapshot_namespace(resolved_job_dir, input_snapshot_namespace)
        input_snapshots: dict[str, dict[str, Any]] = {}
        selected_descriptor, consumed_bytes = _snapshot_with_budget(
            resolved_job_dir,
            source_selected,
            role="selected_source",
            consumed_bytes=0,
            namespace=input_snapshot_namespace,
        )
        input_snapshots["selected_source"] = selected_descriptor
        selected_snapshot_path = verify_input_snapshot(
            resolved_job_dir,
            selected_descriptor,
            role="selected_source",
        )
        try:
            selected_text = read_stable_regular_file(
                selected_snapshot_path,
                require_single_link=True,
            ).decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValueError("ORCA selected input must be UTF-8 text") from exc
        inline_atom_count = _inline_geometry_atom_count(selected_text)
        lines = selected_text.splitlines()
        validate_supported_xyz_geometry_syntax(lines, label="ORCA selected input")
        hessian_requested = _route_requests_hessian(lines)
        if (
            hessian_requested
            and inline_atom_count is not None
            and inline_atom_count > MAX_HESSIAN_ADMISSION_ATOMS
        ):
            raise ValueError(
                "ORCA frequency calculation exceeds the server Hessian atom-count "
                f"limit of {MAX_HESSIAN_ADMISSION_ATOMS}"
            )
        references = _file_references(lines)
        dependency_sources: set[Path] = set()
        for reference in references:
            dependency = _reference_source(
                resolved_job_dir,
                source_selected,
                reference.value,
            )
            if dependency == source_selected:
                raise ValueError("ORCA selected input must not reference itself as an input file")
            if reference.kind == "geometry":
                _validated_xyz_atom_count(
                    dependency,
                    max_atoms=(
                        MAX_HESSIAN_ADMISSION_ATOMS if hessian_requested else MAX_ADMISSION_ATOMS
                    ),
                )
            dependency_sources.add(dependency)
        dependencies = sorted(
            dependency_sources,
            key=lambda path: path.relative_to(resolved_job_dir).as_posix(),
        )

        private_paths: dict[Path, Path] = {}
        materialized_inputs: dict[str, dict[str, Any]] = {}
        dependency_paths: list[str] = []
        for index, dependency in enumerate(dependencies):
            role = f"dependency_{index:06d}"
            descriptor, consumed_bytes = _snapshot_with_budget(
                resolved_job_dir,
                dependency,
                role=role,
                consumed_bytes=consumed_bytes,
                namespace=input_snapshot_namespace,
            )
            input_snapshots[role] = descriptor
            target = _private_input_path(
                execution_dir,
                role=role,
                descriptor=descriptor,
                source=dependency,
            )
            snapshot_path = verify_input_snapshot(
                resolved_job_dir,
                descriptor,
                role=role,
            )
            _write_private_input(
                execution_dir,
                target,
                read_stable_regular_file(snapshot_path, require_single_link=True),
                label="ORCA private dependency snapshot",
            )
            private_paths[dependency] = target.resolve()
            materialized_inputs[role] = _file_identity(target)
            dependency_paths.append(str(dependency))

        bound_payload = _rewrite_bound_input(
            lines,
            references,
            private_paths,
            job_dir=resolved_job_dir,
            selected_inp=source_selected,
            execution_dir=execution_dir,
        )
        if consumed_bytes + len(bound_payload) > MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES:
            raise ValueError("ORCA submission inputs exceed the aggregate snapshot size limit")
        bound_selected = execution_dir / source_selected.name
        _write_private_input(
            execution_dir,
            bound_selected,
            bound_payload,
            label="ORCA bound selected input",
        )
        bound_identity = _file_identity(bound_selected)
        executable = _engine_runner.executable_identity(orca_executable)
        return {
            "version": 1,
            "execution_dir": str(execution_dir),
            "input_snapshot_namespace": input_snapshot_namespace,
            SNAPSHOT_INTENT_TOKEN_KEY: resolved_intent_token,
            SNAPSHOT_INTENT_QUEUE_ROOT_KEY: str(resolved_queue_root),
            "source_selected_inp": str(source_selected),
            "selected_inp": str(bound_selected.resolve()),
            "selected_input_xyz": str(selected_input_xyz or ""),
            "dependency_paths": dependency_paths,
            "input_snapshots": input_snapshots,
            "materialized_inputs": materialized_inputs,
            "bound_selected_identity": bound_identity,
            "resource_request": dict(resource_request),
            "max_retries": int(max_retries),
            "executable_identities": {"orca": executable},
        }
    except BaseException:
        try:
            cleanup_unowned_input_snapshot_namespace(
                resolved_job_dir,
                input_snapshot_namespace,
            )
        finally:
            shutil.rmtree(execution_dir, ignore_errors=True)
            fsync_directory(execution_dir.parent)
            discard_snapshot_intent_if_generations_absent(
                resolved_queue_root,
                resolved_intent_token,
            )
        raise


def orca_execution_snapshot_generation_dirs(
    job_dir: str | Path,
    snapshot: Any,
) -> tuple[Path, Path]:
    """Return the exact private generations owned by one ORCA submission."""

    if not isinstance(snapshot, Mapping):
        raise ValueError("ORCA execution snapshot must be an object")
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    raw_execution_dir = Path(str(snapshot.get("execution_dir") or "")).expanduser()
    execution_dir = raw_execution_dir.resolve()
    raw_expected_parent = resolved_job_dir / ORCA_EXECUTION_ROOT_NAME
    expected_parent = raw_expected_parent.resolve()
    if (
        raw_expected_parent.is_symlink()
        or raw_execution_dir.is_symlink()
        or not execution_dir.is_dir()
        or execution_dir.parent != expected_parent
    ):
        raise ValueError("Queued ORCA execution directory escapes its job directory")
    namespace = str(snapshot.get("input_snapshot_namespace") or "").strip()
    if not namespace or namespace != execution_dir.name:
        raise ValueError("Queued ORCA input snapshot namespace does not match its generation")
    input_generation = input_snapshot_namespace_dir(resolved_job_dir, namespace)
    return execution_dir, input_generation


def _verify_identity(identity: Any, *, root: Path, label: str) -> Path:
    if not isinstance(identity, Mapping):
        raise ValueError(f"Queued ORCA execution snapshot has no {label} identity")
    path = require_confined_regular_file(
        root,
        Path(str(identity.get("path") or "")).expanduser(),
        label=f"Queued ORCA {label} snapshot",
    )
    current = _file_identity(path)
    if current != dict(identity):
        raise ValueError(f"Queued ORCA {label} snapshot is corrupt")
    return path


def verify_orca_execution_snapshot(
    job_dir: str | Path,
    snapshot: Any,
    *,
    expected_selected_inp: str | Path,
    expected_selected_input_xyz: str,
    expected_resource_request: Mapping[str, int],
    expected_max_retries: int,
) -> tuple[Path, str]:
    """Verify a queued private ORCA input tree and its bound executable identity."""

    resolved_job_dir = Path(job_dir).expanduser().resolve()
    if not isinstance(snapshot, Mapping) or snapshot.get("version") != 1:
        raise ValueError("Queue metadata 'execution_snapshot' has an unsupported version")
    execution_dir, input_generation = orca_execution_snapshot_generation_dirs(
        resolved_job_dir,
        snapshot,
    )

    raw_inputs = snapshot.get("input_snapshots")
    materialized_inputs = snapshot.get("materialized_inputs")
    dependency_paths = snapshot.get("dependency_paths")
    if (
        not isinstance(raw_inputs, Mapping)
        or not isinstance(materialized_inputs, Mapping)
        or not isinstance(dependency_paths, list)
        or any(not isinstance(path, str) or not path.strip() for path in dependency_paths)
    ):
        raise ValueError("Queue metadata 'execution_snapshot' has invalid ORCA inputs")
    expected_source_roles = {"selected_source"}
    expected_dependency_roles = {
        f"dependency_{index:06d}" for index in range(len(dependency_paths))
    }
    if set(raw_inputs) != expected_source_roles | expected_dependency_roles:
        raise ValueError("Queued ORCA execution snapshot has unexpected source roles")
    if set(materialized_inputs) != expected_dependency_roles:
        raise ValueError("Queued ORCA execution snapshot has unexpected private input roles")

    for role, descriptor in raw_inputs.items():
        if not isinstance(role, str) or not isinstance(descriptor, Mapping):
            raise ValueError("Queued ORCA execution snapshot has an invalid source descriptor")
        source_snapshot = verify_input_snapshot(resolved_job_dir, descriptor, role=role)
        if source_snapshot.parent != input_generation:
            raise ValueError(
                f"Queued ORCA source snapshot {role!r} escapes its submission generation"
            )
    for index, expected_source in enumerate(dependency_paths):
        role = f"dependency_{index:06d}"
        descriptor = raw_inputs[role]
        if str(descriptor.get("source_path") or "") != expected_source:
            raise ValueError(f"Queued ORCA dependency {role!r} has a mismatched source path")
        private_path = _verify_identity(
            materialized_inputs[role],
            root=execution_dir,
            label=f"private dependency {role!r}",
        )
        if not private_path.is_relative_to(execution_dir / ".inputs"):
            raise ValueError(f"Queued ORCA dependency {role!r} escapes its private input tree")
        if materialized_inputs[role].get("sha256") != descriptor.get(
            "sha256"
        ) or materialized_inputs[role].get("size_bytes") != descriptor.get("size_bytes"):
            raise ValueError(f"Queued ORCA dependency {role!r} mismatches its source snapshot")

    selected = _verify_identity(
        snapshot.get("bound_selected_identity"),
        root=execution_dir,
        label="bound selected input",
    )
    expected_selected = Path(expected_selected_inp).expanduser().resolve()
    if (
        selected != expected_selected
        or str(snapshot.get("selected_inp") or "") != str(expected_selected)
        or not selected.is_relative_to(execution_dir)
        or selected.parent != execution_dir
    ):
        raise ValueError("Queued ORCA selected input does not match its private snapshot")
    if str(snapshot.get("selected_input_xyz") or "") != str(expected_selected_input_xyz or ""):
        raise ValueError("Queued ORCA selected geometry does not match its execution snapshot")
    if snapshot.get("resource_request") != dict(expected_resource_request):
        raise ValueError("Queued ORCA resource request does not match its execution snapshot")
    if snapshot.get("max_retries") != int(expected_max_retries):
        raise ValueError("Queued ORCA retry budget does not match its execution snapshot")

    executable_identities = snapshot.get("executable_identities")
    if not isinstance(executable_identities, Mapping):
        raise ValueError("Queued ORCA execution snapshot has no executable identities")
    executable = _engine_runner.verify_executable_identity(executable_identities.get("orca"))
    return selected, executable


def cleanup_unowned_orca_execution_snapshot(job_dir: str | Path, snapshot: Any) -> None:
    """Remove a private generation that never acquired a durable queue owner."""

    if not isinstance(snapshot, Mapping):
        return
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    raw_execution_dir = Path(str(snapshot.get("execution_dir") or "")).expanduser()
    execution_dir = raw_execution_dir.resolve()
    expected_parent = resolved_job_dir / ORCA_EXECUTION_ROOT_NAME
    if (
        expected_parent.is_symlink()
        or raw_execution_dir.is_symlink()
        or not execution_dir.is_relative_to(expected_parent.resolve())
        or execution_dir.parent != expected_parent.resolve()
    ):
        raise ValueError("Refusing to clean an unconfined ORCA execution snapshot")
    namespace = str(snapshot.get("input_snapshot_namespace") or "").strip()
    if not namespace or namespace != execution_dir.name:
        raise ValueError("Refusing to clean a mismatched ORCA execution snapshot pair")
    try:
        try:
            cleanup_unowned_input_snapshot_namespace(resolved_job_dir, namespace)
        finally:
            try:
                shutil.rmtree(execution_dir)
            except FileNotFoundError:
                pass
            else:
                fsync_directory(execution_dir.parent)
    finally:
        intent_token = str(snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY) or "").strip()
        intent_root = str(snapshot.get(SNAPSHOT_INTENT_QUEUE_ROOT_KEY) or "").strip()
        if intent_token and intent_root:
            discard_snapshot_intent_if_generations_absent(intent_root, intent_token)


__all__ = [
    "MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES",
    "MAX_ORCA_INPUT_REFERENCES",
    "ORCA_EXECUTION_ROOT_NAME",
    "build_orca_execution_snapshot",
    "cleanup_unowned_orca_execution_snapshot",
    "orca_execution_snapshot_generation_dirs",
    "verify_orca_execution_snapshot",
]
