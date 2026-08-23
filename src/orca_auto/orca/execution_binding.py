from __future__ import annotations

import hashlib
import io
import math
import os
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.artifacts import RUN_REPORT_JSON_FILE, RUN_STATE_FILE
from orca_auto.core.engine_process import (
    atomic_write_confined_bytes,
    require_confined_regular_file,
)
from orca_auto.core.geometry_limits import MAX_ADMISSION_ATOMS, MAX_HESSIAN_ADMISSION_ATOMS
from orca_auto.core.queue.engine.input_snapshot import (
    MAX_INPUT_SNAPSHOT_BYTES,
    cleanup_unowned_direct_generation_directory,
    read_stable_regular_file,
)
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_TOKEN_KEY,
    bind_snapshot_intent_generation_identities,
    create_snapshot_intent,
    discard_snapshot_intent,
    discard_snapshot_intent_if_generations_absent,
)
from orca_auto.core.queue.generation import (
    is_visible_generation_name,
    new_visible_generation_name,
)
from orca_auto.core.utils.persistence import durable_mkdir, fsync_directory

from .completion_rules import IRC_ROUTE_RE, OPT_ROUTE_RE, TS_ROUTE_RE
from .input_blocks import (
    GEOM_HEADER_RE,
    OrcaFileReference,
    ensure_route_keywords,
    orca_input_requests_moread,
    orca_moinp_references,
    orca_route_line,
    quote_orca_path,
    scan_orca_file_references,
    set_moinp,
    unquoted_orca_path,
    validate_supported_xyz_geometry_syntax,
    validate_unambiguous_orca_directives,
)
from .job_type import FREQ_RE
from .resource_directives import resource_request_from_lines
from .retry_policy import (
    effective_max_retries,
    resume_checkpoint_input_path,
    retry_input_path,
)

ORCA_EXECUTION_SNAPSHOT_VERSION = 2
MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES = 4 * MAX_INPUT_SNAPSHOT_BYTES
_GENERATION_RUNTIME_FILE_NAMES = frozenset(
    {
        RUN_REPORT_JSON_FILE,
        RUN_STATE_FILE,
    }
)
_XYZ_GEOMETRY_REFERENCE_KINDS = frozenset({"geometry", "neb_geometry"})
_NEB_ROUTE_RE = re.compile(r"\b(?:ZOOM-)?NEB(?:-(?:TS|CI))?\b", re.IGNORECASE)
_XYZ_ATOM_LABEL_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_:+().{}\[\]-]*\Z")
_XYZ_COORDINATE_RE = re.compile(r"\A[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?\Z")


def _render_bound_reference(reference: OrcaFileReference, relative_path: str) -> str:
    if reference.kind != "geometry":
        return quote_orca_path(relative_path)
    return unquoted_orca_path(relative_path)


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


def _route_writes_same_stem_xyz(lines: list[str]) -> bool:
    route_text = " ".join(route for line in lines if (route := orca_route_line(line)) is not None)
    return any(
        pattern.search(route_text) is not None
        for pattern in (OPT_ROUTE_RE, TS_ROUTE_RE, IRC_ROUTE_RE, _NEB_ROUTE_RE)
    )


def _validated_xyz_atom_count(path: Path, payload: bytes, *, max_atoms: int) -> int:
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


def _strict_xyz_atom_row(path: Path, line: str) -> str:
    tokens = line.split()
    if len(tokens) != 4 or _XYZ_ATOM_LABEL_RE.fullmatch(tokens[0]) is None:
        raise ValueError(f"ORCA XYZ geometry has an invalid atom row: {path}")
    if any(_XYZ_COORDINATE_RE.fullmatch(value) is None for value in tokens[1:]):
        raise ValueError(f"ORCA XYZ geometry has invalid coordinates: {path}")
    coordinates = tuple(float(value.replace("d", "e").replace("D", "E")) for value in tokens[1:])
    if not all(math.isfinite(value) for value in coordinates):
        raise ValueError(f"ORCA XYZ geometry has non-finite coordinates: {path}")
    return tokens[0].casefold()


def _xyz_atom_lines(path: Path, payload: bytes, *, max_atoms: int) -> list[str]:
    atom_count = _validated_xyz_atom_count(path, payload, max_atoms=max_atoms)
    try:
        lines = payload.decode("utf-8", errors="strict").splitlines()
    except UnicodeError as exc:
        raise ValueError(f"ORCA XYZ geometry must be UTF-8 text: {path}") from exc
    header_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        -1,
    )
    atom_start = header_index + 2
    atom_lines = lines[atom_start : atom_start + atom_count]
    if header_index < 0 or len(atom_lines) != atom_count:
        raise ValueError(f"ORCA XYZ geometry has fewer atoms than declared: {path}")
    for line in atom_lines:
        _strict_xyz_atom_row(path, line)
    if any(line.strip() for line in lines[atom_start + atom_count :]):
        raise ValueError(f"ORCA XYZ geometry has trailing rows after its declared atoms: {path}")
    return atom_lines


def _inline_geometry_atom_signature(path: Path, payload: bytes) -> tuple[str, ...]:
    try:
        lines = payload.decode("utf-8", errors="strict").splitlines()
    except UnicodeError as exc:
        raise ValueError(f"ORCA inline geometry must be UTF-8 text: {path}") from exc
    for index, line in enumerate(lines):
        match = GEOM_HEADER_RE.match(line.strip())
        if match is None:
            continue
        if match.group(1).lower() != "xyz":
            raise ValueError(f"ORCA recovery bound input has no inline geometry: {path}")
        signature: list[str] = []
        for atom_line in lines[index + 1 :]:
            if atom_line.strip() == "*":
                if not signature:
                    raise ValueError(f"ORCA recovery bound input has an empty geometry: {path}")
                return tuple(signature)
            signature.append(_strict_xyz_atom_row(path, atom_line))
        break
    raise ValueError(f"ORCA recovery bound input has no complete inline geometry: {path}")


def _reference_source(job_dir: Path, selected_inp: Path, reference: str) -> Path:
    raw_path = Path(reference).expanduser()
    candidate = raw_path if raw_path.is_absolute() else selected_inp.parent / raw_path
    return require_confined_regular_file(
        job_dir,
        candidate,
        label="ORCA referenced input",
    )


def _confined_reference_path(job_dir: Path, selected_inp: Path, reference: str) -> Path:
    """Resolve a reference without requiring its final file to still exist."""

    raw_path = Path(reference).expanduser()
    candidate = raw_path if raw_path.is_absolute() else selected_inp.parent / raw_path
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(job_dir):
        raise ValueError(f"ORCA referenced input must stay inside its root: {candidate}")
    current = candidate
    while current != job_dir and current != current.parent:
        if current.is_symlink():
            raise ValueError(f"ORCA referenced input must not contain symlinks: {current}")
        current = current.parent
    return resolved


def _execution_directory(job_dir: Path, generation_name: str) -> Path:
    if not is_visible_generation_name(generation_name):
        raise ValueError("ORCA generation name is invalid")
    execution_dir = job_dir / generation_name
    if execution_dir.is_symlink():
        raise ValueError(f"ORCA generation must not be a symlink: {execution_dir}")
    execution_dir.mkdir(mode=0o700, exist_ok=False)
    fsync_directory(job_dir)
    return execution_dir.resolve()


def _payload_with_budget(
    source: Path,
    payload: bytes,
    *,
    role: str,
    consumed_bytes: int,
) -> tuple[dict[str, Any], bytes, int]:
    if consumed_bytes + len(payload) > MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES:
        raise ValueError("ORCA submission inputs exceed the aggregate snapshot size limit")
    descriptor = {
        "role": role,
        "source_path": str(source.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    return descriptor, payload, consumed_bytes + len(payload)


def _source_with_budget(
    source: Path,
    *,
    role: str,
    consumed_bytes: int,
) -> tuple[dict[str, Any], bytes, int]:
    payload = read_stable_regular_file(source, max_bytes=MAX_INPUT_SNAPSHOT_BYTES)
    return _payload_with_budget(
        source,
        payload,
        role=role,
        consumed_bytes=consumed_bytes,
    )


def _private_input_path(
    execution_dir: Path,
    *,
    source: Path,
) -> Path:
    name = source.name
    if not name or any(character in name for character in ("\x00", "\r", "\n", "\\")):
        raise ValueError(f"ORCA referenced input filename cannot be preserved safely: {source}")
    return execution_dir / name


def _validate_unique_dependency_basenames(
    dependencies: list[Path],
    *,
    job_dir: Path,
) -> None:
    source_by_basename: dict[str, Path] = {}
    for source in dependencies:
        previous = source_by_basename.setdefault(source.name, source)
        if previous == source:
            continue
        previous_display = previous.relative_to(job_dir).as_posix()
        source_display = source.relative_to(job_dir).as_posix()
        raise ValueError(
            "ORCA referenced inputs from different source paths use the same basename, "
            "which is not supported by a flat generation: "
            f"{source.name} ({previous_display}, {source_display})"
        )


def _validate_dependency_basename(
    source: Path,
    selected_inp: Path,
    *,
    effective_retry_count: int,
    hessian_requested: bool,
    same_stem_xyz_is_output: bool,
    inline_same_stem_xyz: bool,
) -> None:
    name = source.name
    retry_inputs = [
        retry_input_path(selected_inp, retry_number)
        for retry_number in range(1, effective_retry_count + 1)
    ]
    attempt_inputs = [selected_inp, *retry_inputs]
    resume_inputs = [
        resume_checkpoint_input_path(attempt_input) for attempt_input in attempt_inputs
    ]
    runtime_input_variants = [*attempt_inputs, *resume_inputs]
    runtime_owned_names = set(_GENERATION_RUNTIME_FILE_NAMES)
    runtime_owned_names.update(path.name for path in [*retry_inputs, *resume_inputs])
    for runtime_input in runtime_input_variants:
        runtime_owned_names.add(runtime_input.with_suffix(".out").name)
        runtime_owned_names.add(runtime_input.with_suffix(".gbw").name)
        if hessian_requested:
            runtime_owned_names.add(runtime_input.with_suffix(".hess").name)
        if same_stem_xyz_is_output and not inline_same_stem_xyz:
            runtime_owned_names.add(runtime_input.with_suffix(".xyz").name)
    if name in runtime_owned_names:
        raise ValueError(
            "ORCA referenced input basename conflicts with a generation runtime/output file: "
            f"{name}"
        )


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
    references: list[OrcaFileReference],
    private_paths: Mapping[Path, Path],
    *,
    job_dir: Path,
    selected_inp: Path,
    execution_dir: Path,
    inline_geometry_atoms: Mapping[Path, list[str]],
) -> bytes:
    replacements: dict[int, list[tuple[int, int, str]]] = {}
    inline_replacements: dict[int, str] = {}
    for reference in references:
        source = _confined_reference_path(job_dir, selected_inp, reference.value)
        if reference.kind == "geometry" and source in inline_geometry_atoms:
            match = GEOM_HEADER_RE.match(lines[reference.line_index].strip())
            if match is None or match.group(1).lower() != "xyzfile":
                raise ValueError("ORCA mutable geometry reference cannot be safely inlined")
            if reference.line_index in inline_replacements:
                raise ValueError("ORCA selected input has duplicate mutable geometry references")
            inline_replacements[reference.line_index] = "\n".join(
                [
                    f"* xyz {match.group(2)} {match.group(3)}",
                    *inline_geometry_atoms[source],
                    "*",
                ]
            )
            continue
        private_path = private_paths[source]
        relative = private_path.relative_to(execution_dir).as_posix()
        replacements.setdefault(reference.line_index, []).append(
            (reference.start, reference.end, _render_bound_reference(reference, relative))
        )
    rewritten = list(lines)
    for line_index, replacement in inline_replacements.items():
        if line_index in replacements:
            raise ValueError("ORCA geometry line has conflicting snapshot rewrites")
        rewritten[line_index] = replacement
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


def orca_execution_provenance(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the durable execution identity attached to ORCA run artifacts."""

    execution_dir_identity = snapshot.get("execution_dir_identity")
    if not isinstance(execution_dir_identity, Mapping):
        raise ValueError("ORCA execution snapshot has no generation directory identity")
    return {
        "execution_dir": str(snapshot.get("execution_dir") or ""),
        "execution_dir_identity": {
            "device": int(execution_dir_identity.get("device", -1)),
            "inode": int(execution_dir_identity.get("inode", -1)),
        },
        "generation_owner_token": str(snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY) or ""),
        "source_selected_inp": str(snapshot.get("source_selected_inp") or ""),
        "bound_selected_identity": dict(snapshot.get("bound_selected_identity") or {}),
        "materialized_inputs": dict(snapshot.get("materialized_inputs") or {}),
        "runtime_mutable_input_roles": list(snapshot.get("runtime_mutable_input_roles") or []),
        "executable_identity": dict(
            (snapshot.get("executable_identities") or {}).get("orca") or {}
        ),
    }


def verify_orca_snapshot_executable(
    snapshot: Mapping[str, Any],
    *,
    expected_executable: str | Path | None = None,
) -> str:
    """Verify and return the executable identity pinned by one snapshot."""

    executable_identities = snapshot.get("executable_identities")
    if not isinstance(executable_identities, Mapping):
        raise ValueError("Queued ORCA execution snapshot has no executable identities")
    identity = executable_identities.get("orca")
    if not isinstance(identity, dict):
        raise ValueError("Queued ORCA execution snapshot has no ORCA executable identity")
    if expected_executable is None:
        return _engine_runner.verify_executable_identity(identity)
    current = _engine_runner.executable_identity(expected_executable)
    if current != identity:
        raise ValueError("ORCA crash recovery executable does not match the submitted identity")
    return str(current["path"])


def _reserve_execution_generation(
    job_dir: Path,
    *,
    queue_root: Path,
    intent_token: str,
) -> tuple[str, Path, tuple[int, int]]:
    job_status = job_dir.stat()
    job_identity = (int(job_status.st_dev), int(job_status.st_ino))
    for _attempt in range(32):
        generation_name = new_visible_generation_name()
        execution_dir = job_dir / generation_name
        try:
            create_snapshot_intent(
                queue_root,
                token=intent_token,
                kind="orca_visible_generation",
                generation_paths=[execution_dir],
            )
        except FileExistsError:
            if execution_dir.exists() or execution_dir.is_symlink():
                continue
            raise
        try:
            reserved = _execution_directory(job_dir, generation_name)
        except FileExistsError:
            discard_snapshot_intent(queue_root, intent_token)
            continue
        except BaseException:
            discard_snapshot_intent(queue_root, intent_token)
            raise
        details = reserved.stat()
        generation_identity = (int(details.st_dev), int(details.st_ino))
        try:
            bind_snapshot_intent_generation_identities(queue_root, intent_token)
        except BaseException:
            try:
                cleanup_unowned_direct_generation_directory(
                    job_dir,
                    namespace=generation_name,
                    label="ORCA execution snapshot",
                    expected_job_identity=job_identity,
                    expected_generation_identity=generation_identity,
                    expected_owner_token=intent_token,
                )
            finally:
                discard_snapshot_intent_if_generations_absent(queue_root, intent_token)
            raise
        return generation_name, reserved, generation_identity
    raise FileExistsError("Could not reserve a unique visible ORCA generation directory")


def _recovery_seed_plan(
    job_dir: Path,
    recovery_from: Mapping[str, Any],
) -> tuple[Path, str, set[str], tuple[str, ...] | None, dict[str, dict[str, Any]]]:
    """Validate a crashed snapshot and plan seeding from its frozen generation."""

    seed_dir = orca_execution_snapshot_generation_dir(job_dir, recovery_from)
    raw_sources = recovery_from.get("source_inputs")
    raw_selected = raw_sources.get("selected_source") if isinstance(raw_sources, Mapping) else None
    if not isinstance(raw_selected, Mapping):
        raise ValueError("ORCA recovery snapshot has no selected source descriptor")
    selected_sha256 = str(raw_selected.get("sha256") or "").strip().lower()
    if len(selected_sha256) != 64:
        raise ValueError("ORCA recovery snapshot has no selected source digest")
    mutable_roles = recovery_from.get("runtime_mutable_input_roles")
    materialized = recovery_from.get("materialized_inputs")
    seed_basenames: set[str] = set()
    if isinstance(mutable_roles, Sequence) and isinstance(materialized, Mapping):
        for role in mutable_roles:
            identity = materialized.get(role)
            if not isinstance(identity, Mapping):
                continue
            name = Path(str(identity.get("path") or "")).name
            if name:
                seed_basenames.add(name)
    seed_atom_signature: tuple[str, ...] | None = None
    if seed_basenames:
        _bound_selected, bound_selected_payload = _verified_identity_payload(
            recovery_from.get("bound_selected_identity"),
            root=seed_dir,
            label="recovery bound selected input",
        )
        seed_atom_signature = _inline_geometry_atom_signature(
            _bound_selected,
            bound_selected_payload,
        )
    submitted_identities = _recovery_submitted_dependency_identities(
        job_dir,
        recovery_from,
    )
    return seed_dir, selected_sha256, seed_basenames, seed_atom_signature, submitted_identities


def _validated_submitted_dependency_identity(
    job_dir: Path,
    source_text: str,
    identity: Any,
) -> dict[str, Any]:
    try:
        source = Path(source_text)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("ORCA recovery dependency identity has an invalid source path") from exc
    if (
        not source_text
        or not source.is_absolute()
        or source_text != str(source)
        or ".." in source.parts
        or "\x00" in source_text
        or not source.is_relative_to(job_dir)
    ):
        raise ValueError("ORCA recovery dependency identity has an invalid source path")
    if not isinstance(identity, Mapping):
        raise ValueError(f"ORCA recovery dependency identity is invalid: {source_text}")
    digest = str(identity.get("sha256") or "").strip().lower()
    size = identity.get("size_bytes")
    if (
        len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        raise ValueError(f"ORCA recovery dependency identity is invalid: {source_text}")
    return {"sha256": digest, "size_bytes": size}


def _recovery_submitted_dependency_identities(
    job_dir: Path,
    recovery_from: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    recovery = recovery_from.get("recovery")
    propagated = (
        recovery.get("submitted_dependency_identities") if isinstance(recovery, Mapping) else None
    )
    if propagated is not None:
        if not isinstance(propagated, Mapping):
            raise ValueError("ORCA recovery submitted dependency identities are invalid")
        return {
            str(source): _validated_submitted_dependency_identity(
                job_dir,
                str(source),
                identity,
            )
            for source, identity in propagated.items()
        }

    dependency_paths = recovery_from.get("dependency_paths")
    source_inputs = recovery_from.get("source_inputs")
    if not isinstance(dependency_paths, list) or not isinstance(source_inputs, Mapping):
        raise ValueError("ORCA recovery snapshot has invalid dependency identities")
    submitted: dict[str, dict[str, Any]] = {}
    for index, raw_source in enumerate(dependency_paths):
        if not isinstance(raw_source, str):
            raise ValueError("ORCA recovery snapshot has invalid dependency identities")
        role = f"dependency_{index:06d}"
        descriptor = source_inputs.get(role)
        if (
            not isinstance(descriptor, Mapping)
            or str(descriptor.get("role") or "") != role
            or descriptor.get("source_path") != raw_source
        ):
            raise ValueError("ORCA recovery snapshot has invalid dependency identities")
        submitted[raw_source] = _validated_submitted_dependency_identity(
            job_dir,
            raw_source,
            descriptor,
        )
    return submitted


def _validated_recovery_source_payload(
    job_dir: Path,
    source: Path,
    submitted_identities: Mapping[str, Mapping[str, Any]],
) -> bytes:
    expected = submitted_identities.get(str(source))
    if expected is None:
        raise ValueError(
            "ORCA recovery has no submitted identity for dependency: "
            f"{source.relative_to(job_dir).as_posix()}"
        )
    payload = read_stable_regular_file(source, max_bytes=MAX_INPUT_SNAPSHOT_BYTES)
    if len(payload) != expected.get("size_bytes") or hashlib.sha256(
        payload
    ).hexdigest() != expected.get("sha256"):
        raise ValueError(
            "ORCA recovery dependency changed since the crashed submission: "
            f"{source.relative_to(job_dir).as_posix()}"
        )
    return payload


def _validated_recovery_seed(
    *,
    job_dir: Path,
    seed_dir: Path,
    basename: str,
    expected_atom_signature: tuple[str, ...],
    max_atoms: int,
) -> tuple[Path, bytes] | None:
    """Return the exact validated runtime seed bytes, or None to use the source.

    A crash can leave the runtime geometry absent, empty, or truncated
    mid-write; those fall back to the pristine submission geometry. A seed
    that parses but changes the submitted atom order or labels is evidence of
    substitution, not of a crash, and fails closed.
    """

    candidate = seed_dir / basename
    if not candidate.exists() and not candidate.is_symlink():
        return None
    seed = require_confined_regular_file(
        job_dir,
        candidate,
        label="ORCA recovery seed geometry",
    )
    payload = read_stable_regular_file(seed, max_bytes=MAX_INPUT_SNAPSHOT_BYTES)
    if not payload.strip():
        return None
    try:
        seed_atoms = _xyz_atom_lines(seed, payload, max_atoms=max_atoms)
    except ValueError:
        return None
    seed_atom_signature = tuple(_strict_xyz_atom_row(seed, line) for line in seed_atoms)
    if seed_atom_signature != expected_atom_signature:
        raise ValueError(
            "ORCA recovery seed geometry does not preserve the submitted atom count, order, "
            "and labels: "
            f"{basename}"
        )
    return seed, payload


def recovery_checkpoint_private_name(source_selected: Path) -> str:
    return f"{source_selected.stem}.moinp.gbw"


def recovery_checkpoint_source_names(
    source_selected: Path,
    *,
    effective_retry_count: int,
) -> list[str]:
    """Attempt checkpoint basenames, oldest attempt first.

    A crash can land while ORCA is running a generated retry or resume input,
    whose orbitals live under that attempt's stem (for example
    ``<stem>.retry01.gbw``), not the submission stem.
    """

    attempts = [source_selected]
    for retry_number in range(1, effective_retry_count + 1):
        attempts.append(retry_input_path(source_selected, retry_number))
    attempts.extend(resume_checkpoint_input_path(attempt) for attempt in list(attempts))
    names: list[str] = []
    for attempt in attempts:
        name = attempt.with_suffix(".gbw").name
        if name not in names:
            names.append(name)
    return names


def _is_recovery_checkpoint_source_name(name: str, *, source_selected: Path) -> bool:
    """Whether *name* is an attempt checkpoint basename for this submission.

    Mirrors the shapes produced by ``recovery_checkpoint_source_names``
    (base, ``retryNN``, and ``resume`` stems) without re-deriving the retry
    budget from any file, so claim-time verification stays a function of the
    pinned snapshot alone.
    """

    stem = re.escape(source_selected.stem)
    return re.fullmatch(rf"{stem}(?:\.retry\d{{2}})?(?:\.resume)?\.gbw", name) is not None


def _validated_recovery_checkpoint(
    *,
    job_dir: Path,
    seed_dir: Path,
    source_selected: Path,
    scanned_dependencies: Sequence[Path],
    estimated_source_bytes: int,
    effective_retry_count: int,
) -> Path | None:
    """Return the frozen runtime checkpoint to seed from, or None to skip.

    Considers every attempt stem (base, retries, resumes) and picks the
    newest-written intact candidate. Skipping (absent, empty, over the
    snapshot byte budgets, or a basename collision with a scanned dependency)
    degrades to geometry-only recovery; it never fails the rebind. A
    symlinked or otherwise non-regular candidate still fails closed via the
    confinement check.
    """

    best: tuple[int, int, Path] | None = None
    source_names = recovery_checkpoint_source_names(
        source_selected,
        effective_retry_count=effective_retry_count,
    )
    for rank, name in enumerate(source_names):
        candidate = seed_dir / name
        if not candidate.exists() and not candidate.is_symlink():
            continue
        checkpoint = require_confined_regular_file(
            job_dir,
            candidate,
            label="ORCA recovery checkpoint",
        )
        size = checkpoint.stat().st_size
        if size == 0 or size > MAX_INPUT_SNAPSHOT_BYTES:
            continue
        if estimated_source_bytes + size > MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES:
            continue
        key = (checkpoint.stat().st_mtime_ns, rank)
        if best is None or key > (best[0], best[1]):
            best = (key[0], key[1], checkpoint)
    if best is None:
        return None
    private_name = recovery_checkpoint_private_name(source_selected)
    if any(dependency.name == private_name for dependency in scanned_dependencies):
        return None
    return best[2]


def orca_execution_started_evidence(job_dir: str | Path, snapshot: Any) -> bool:
    """Report whether a queued generation shows evidence of started execution.

    Evidence is structural and content-independent: any directory entry beyond
    the materialized snapshot files, or a registered runtime-mutable input
    whose bytes moved. An unreadable mutable input counts as evidence so a
    crashed run never silently reuses its generation.
    """

    if not isinstance(snapshot, Mapping):
        raise ValueError("ORCA execution snapshot must be an object")
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    execution_dir = orca_execution_snapshot_generation_dir(resolved_job_dir, snapshot)
    expected_names = {Path(str(snapshot.get("selected_inp") or "")).name}
    materialized = snapshot.get("materialized_inputs")
    if isinstance(materialized, Mapping):
        for identity in materialized.values():
            if isinstance(identity, Mapping):
                name = Path(str(identity.get("path") or "")).name
                if name:
                    expected_names.add(name)
    expected_names.discard("")
    with os.scandir(execution_dir) as entries:
        for entry in entries:
            if entry.name not in expected_names:
                return True
    mutable_roles = snapshot.get("runtime_mutable_input_roles")
    if isinstance(mutable_roles, Sequence) and isinstance(materialized, Mapping):
        for role in mutable_roles:
            identity = materialized.get(role)
            if not isinstance(identity, Mapping):
                continue
            path = Path(str(identity.get("path") or ""))
            try:
                current = _file_identity(path)
            except (OSError, ValueError):
                return True
            if current != dict(identity):
                return True
    return False


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
    normalized_selected_payload: bytes | None = None,
    source_selected_sha256: str | None = None,
    bound_selected_validator: Callable[[Path, bytes], None] | None = None,
    recovery_from: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one visible, immutable ORCA execution generation.

    With ``recovery_from`` (the crashed submission's snapshot), runtime-mutable
    geometry inputs are seeded from that frozen generation so the replacement
    generation resumes from the last written geometry while every integrity
    check stays byte-exact.
    """

    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("ORCA execution snapshot retry budget must be a nonnegative integer")
    if set(resource_request) != {"max_cores", "max_memory_gb"} or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in resource_request.values()
    ):
        raise ValueError("ORCA execution snapshot resources must be positive integers")
    if (normalized_selected_payload is None) != (source_selected_sha256 is None):
        raise ValueError("ORCA normalized selected input requires its bound source digest")
    raw_job_dir = Path(job_dir).expanduser()
    resolved_job_dir = raw_job_dir.resolve()
    if raw_job_dir.is_symlink() or not resolved_job_dir.is_dir():
        raise ValueError(f"ORCA job directory must be a real directory: {job_dir}")
    job_dir_stat = resolved_job_dir.stat()
    source_selected = require_confined_regular_file(
        resolved_job_dir,
        Path(selected_inp),
        label="ORCA selected input",
    )
    if source_selected.suffix.lower() != ".inp":
        raise ValueError(f"ORCA selected input must be an .inp file: {source_selected}")

    recovery_executable: str | None = None
    if recovery_from is not None:
        recovery_executable = verify_orca_snapshot_executable(
            recovery_from,
            expected_executable=orca_executable,
        )

    recovery_seed_dir: Path | None = None
    recovery_selected_sha256 = ""
    recovery_seed_basenames: set[str] = set()
    recovery_seed_atom_signature: tuple[str, ...] | None = None
    recovery_submitted_dependency_identities: dict[str, dict[str, Any]] = {}
    if recovery_from is not None:
        (
            recovery_seed_dir,
            recovery_selected_sha256,
            recovery_seed_basenames,
            recovery_seed_atom_signature,
            recovery_submitted_dependency_identities,
        ) = _recovery_seed_plan(resolved_job_dir, recovery_from)
    seeded_roles: dict[str, dict[str, Any]] = {}

    resolved_queue_root = Path(queue_root or resolved_job_dir).expanduser().resolve()
    resolved_intent_token = snapshot_intent_token or f"snapshot-{secrets.token_hex(16)}"
    generation_name, execution_dir, generation_identity = _reserve_execution_generation(
        resolved_job_dir,
        queue_root=resolved_queue_root,
        intent_token=resolved_intent_token,
    )
    try:
        source_inputs: dict[str, dict[str, Any]] = {}
        selected_descriptor, selected_payload, consumed_bytes = _source_with_budget(
            source_selected,
            role="selected_source",
            consumed_bytes=0,
        )
        source_inputs["selected_source"] = selected_descriptor
        if (
            source_selected_sha256 is not None
            and str(selected_descriptor.get("sha256") or "") != source_selected_sha256
        ):
            raise ValueError("ORCA selected input changed while submission resources were prepared")
        if recovery_from is not None and (
            str(selected_descriptor.get("sha256") or "").strip().lower() != recovery_selected_sha256
        ):
            raise ValueError("ORCA recovery source input changed since the crashed submission")
        try:
            selected_text = (
                normalized_selected_payload
                if normalized_selected_payload is not None
                else selected_payload
            ).decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValueError("ORCA selected input must be UTF-8 text") from exc
        inline_atom_count = _inline_geometry_atom_count(selected_text)
        lines = selected_text.splitlines()
        if normalized_selected_payload is not None and resource_request_from_lines(lines) != dict(
            resource_request
        ):
            raise ValueError("ORCA normalized selected input does not match its resource request")
        validate_supported_xyz_geometry_syntax(lines, label="ORCA selected input")
        moinp_references = orca_moinp_references(lines)
        if orca_input_requests_moread(lines) and not moinp_references:
            raise ValueError(
                "ORCA MORead requires an explicit MOInp file so the checkpoint can be "
                "bound into the execution snapshot"
            )
        hessian_requested = _route_requests_hessian(lines)
        same_stem_xyz_is_output = _route_writes_same_stem_xyz(lines)
        effective_retry_count = effective_max_retries(
            source_selected,
            configured_max_retries=max_retries,
        )
        if (
            hessian_requested
            and inline_atom_count is not None
            and inline_atom_count > MAX_HESSIAN_ADMISSION_ATOMS
        ):
            raise ValueError(
                "ORCA frequency calculation exceeds the server Hessian atom-count "
                f"limit of {MAX_HESSIAN_ADMISSION_ATOMS}"
            )
        references = scan_orca_file_references(lines)
        dependency_sources: set[Path] = set()
        geometry_dependencies: set[Path] = set()
        dependency_reference_kinds: dict[Path, set[str]] = {}
        for reference in references:
            prospective_dependency = _confined_reference_path(
                resolved_job_dir, source_selected, reference.value
            )
            may_use_recovery_geometry_seed = (
                recovery_seed_dir is not None
                and same_stem_xyz_is_output
                and reference.kind == "geometry"
                and prospective_dependency.name == source_selected.with_suffix(".xyz").name
                and str(prospective_dependency) in recovery_submitted_dependency_identities
            )
            dependency = (
                prospective_dependency
                if may_use_recovery_geometry_seed
                else _reference_source(
                    resolved_job_dir,
                    source_selected,
                    reference.value,
                )
            )
            if dependency == source_selected:
                raise ValueError("ORCA selected input must not reference itself as an input file")
            if reference.kind in _XYZ_GEOMETRY_REFERENCE_KINDS:
                geometry_dependencies.add(dependency)
            dependency_reference_kinds.setdefault(dependency, set()).add(reference.kind)
            dependency_sources.add(dependency)
        dependencies = sorted(
            dependency_sources,
            key=lambda path: path.relative_to(resolved_job_dir).as_posix(),
        )
        _validate_unique_dependency_basenames(
            dependencies,
            job_dir=resolved_job_dir,
        )
        recovery_checkpoint_source: Path | None = None
        if recovery_seed_dir is not None and not orca_input_requests_moread(lines):
            # Count dependencies twice: once for the materialized copies and
            # once more as a conservative bound on inline-geometry expansion
            # of the bound input, so a checkpoint near the aggregate cap is
            # skipped gracefully instead of failing the rebind after the
            # generation is already materialized.
            estimated_source_bytes = (
                consumed_bytes
                + 2
                * sum(
                    int(recovery_submitted_dependency_identities[str(dependency)]["size_bytes"])
                    if str(dependency) in recovery_submitted_dependency_identities
                    else dependency.stat().st_size
                    for dependency in dependencies
                )
                + 2 * len(selected_payload)
            )
            recovery_checkpoint_source = _validated_recovery_checkpoint(
                job_dir=resolved_job_dir,
                seed_dir=recovery_seed_dir,
                source_selected=source_selected,
                scanned_dependencies=dependencies,
                estimated_source_bytes=estimated_source_bytes,
                effective_retry_count=effective_retry_count,
            )

        bound_dependencies: list[tuple[Path, Path, bool, str | None, bytes | None]] = []
        for dependency in dependencies:
            inline_same_stem_xyz = (
                same_stem_xyz_is_output
                and dependency.name == source_selected.with_suffix(".xyz").name
                and dependency_reference_kinds.get(dependency) == {"geometry"}
            )
            _validate_dependency_basename(
                dependency,
                source_selected,
                effective_retry_count=effective_retry_count,
                hessian_requested=hessian_requested,
                same_stem_xyz_is_output=same_stem_xyz_is_output,
                inline_same_stem_xyz=inline_same_stem_xyz,
            )
            seed_source: Path | None = None
            dependency_payload: bytes | None = None
            if (
                recovery_seed_dir is not None
                and inline_same_stem_xyz
                and dependency.name in recovery_seed_basenames
            ):
                if recovery_seed_atom_signature is None:
                    raise ValueError("ORCA recovery mutable geometry has no bound atom signature")
                validated_seed = _validated_recovery_seed(
                    job_dir=resolved_job_dir,
                    seed_dir=recovery_seed_dir,
                    basename=dependency.name,
                    expected_atom_signature=recovery_seed_atom_signature,
                    max_atoms=(
                        MAX_HESSIAN_ADMISSION_ATOMS if hessian_requested else MAX_ADMISSION_ATOMS
                    ),
                )
                if validated_seed is not None:
                    seed_source, dependency_payload = validated_seed
            if seed_source is None and recovery_from is not None:
                dependency_payload = _validated_recovery_source_payload(
                    resolved_job_dir,
                    dependency,
                    recovery_submitted_dependency_identities,
                )
            bound_dependencies.append(
                (
                    dependency,
                    seed_source if seed_source is not None else dependency,
                    inline_same_stem_xyz,
                    None,
                    dependency_payload,
                )
            )
        if recovery_checkpoint_source is not None:
            bound_dependencies.append(
                (
                    recovery_checkpoint_source,
                    recovery_checkpoint_source,
                    False,
                    recovery_checkpoint_private_name(source_selected),
                    None,
                )
            )
        # Roles must follow the canonical order of the *stored* source paths:
        # the claim-time verifier re-sorts dependency_paths, and a recovery
        # seed living inside the previous generation sorts differently than
        # the submission source it replaces.
        bound_dependencies.sort(
            key=lambda item: item[1].relative_to(resolved_job_dir).as_posix(),
        )

        private_paths: dict[Path, Path] = {}
        materialized_inputs: dict[str, dict[str, Any]] = {}
        runtime_mutable_input_roles: list[str] = []
        inline_geometry_atoms: dict[Path, list[str]] = {}
        dependency_paths: list[str] = []
        recovery_checkpoint_role = ""
        recovery_checkpoint_private: Path | None = None
        for index, (
            dependency,
            effective_source,
            inline_same_stem_xyz,
            private_override,
            source_payload,
        ) in enumerate(bound_dependencies):
            role = f"dependency_{index:06d}"
            if source_payload is None:
                descriptor, dependency_payload, consumed_bytes = _source_with_budget(
                    effective_source,
                    role=role,
                    consumed_bytes=consumed_bytes,
                )
            else:
                descriptor, dependency_payload, consumed_bytes = _payload_with_budget(
                    effective_source,
                    source_payload,
                    role=role,
                    consumed_bytes=consumed_bytes,
                )
            source_inputs[role] = descriptor
            if effective_source is not dependency:
                seeded_roles[role] = {
                    "path": str(descriptor.get("source_path") or ""),
                    "sha256": str(descriptor.get("sha256") or ""),
                    "size_bytes": int(descriptor.get("size_bytes") or 0),
                }
            if dependency in geometry_dependencies:
                geometry_limit = (
                    MAX_HESSIAN_ADMISSION_ATOMS if hessian_requested else MAX_ADMISSION_ATOMS
                )
                if inline_same_stem_xyz:
                    inline_geometry_atoms[dependency] = _xyz_atom_lines(
                        dependency,
                        dependency_payload,
                        max_atoms=geometry_limit,
                    )
                else:
                    _validated_xyz_atom_count(
                        dependency,
                        dependency_payload,
                        max_atoms=geometry_limit,
                    )
            target = (
                execution_dir / private_override
                if private_override is not None
                else _private_input_path(
                    execution_dir,
                    source=dependency,
                )
            )
            if target.name == source_selected.name:
                raise ValueError(
                    "ORCA referenced input basename conflicts with the selected input in the "
                    f"flat generation: {target.name}"
                )
            _write_private_input(
                execution_dir,
                target,
                dependency_payload,
                label="ORCA dependency snapshot",
            )
            if inline_same_stem_xyz:
                target.chmod(0o600)
            private_paths[dependency] = target.resolve()
            materialized_inputs[role] = _file_identity(target)
            if inline_same_stem_xyz:
                runtime_mutable_input_roles.append(role)
            if private_override is not None:
                recovery_checkpoint_role = role
                recovery_checkpoint_private = target.resolve()
            dependency_paths.append(str(effective_source))

        bound_payload = _rewrite_bound_input(
            lines,
            references,
            private_paths,
            job_dir=resolved_job_dir,
            selected_inp=source_selected,
            execution_dir=execution_dir,
            inline_geometry_atoms=inline_geometry_atoms,
        )
        if recovery_checkpoint_private is not None:
            bound_lines = bound_payload.decode("utf-8").splitlines()
            ensure_route_keywords(bound_lines, ["MORead"])
            set_moinp(bound_lines, recovery_checkpoint_private, execution_dir)
            validate_unambiguous_orca_directives(
                bound_lines,
                label="ORCA recovery bound input",
            )
            bound_payload = ("\n".join(bound_lines).rstrip() + "\n").encode("utf-8")
        if consumed_bytes + len(bound_payload) > MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES:
            raise ValueError("ORCA submission inputs exceed the aggregate snapshot size limit")
        bound_selected = execution_dir / source_selected.name
        if bound_selected_validator is not None:
            bound_selected_validator(bound_selected, bound_payload)
        _write_private_input(
            execution_dir,
            bound_selected,
            bound_payload,
            label="ORCA bound selected input",
        )
        bound_identity = _file_identity(bound_selected)
        executable = _engine_runner.executable_identity(
            recovery_executable if recovery_executable is not None else orca_executable
        )
        if recovery_from is not None:
            executable_identities = recovery_from.get("executable_identities")
            previous_executable = (
                executable_identities.get("orca")
                if isinstance(executable_identities, Mapping)
                else None
            )
            if executable != previous_executable:
                raise ValueError(
                    "ORCA crash recovery executable changed while the replacement snapshot "
                    "was built"
                )
        snapshot: dict[str, Any] = {
            "version": ORCA_EXECUTION_SNAPSHOT_VERSION,
            "job_dir_identity": {
                "device": int(job_dir_stat.st_dev),
                "inode": int(job_dir_stat.st_ino),
            },
            "generation_name": generation_name,
            "execution_dir_identity": {
                "device": generation_identity[0],
                "inode": generation_identity[1],
            },
            "execution_dir": str(execution_dir),
            SNAPSHOT_INTENT_TOKEN_KEY: resolved_intent_token,
            SNAPSHOT_INTENT_QUEUE_ROOT_KEY: str(resolved_queue_root),
            "source_selected_inp": str(source_selected),
            "selected_inp": str(bound_selected.resolve()),
            "selected_input_xyz": str(selected_input_xyz or ""),
            "dependency_paths": dependency_paths,
            "source_inputs": source_inputs,
            "materialized_inputs": materialized_inputs,
            "runtime_mutable_input_roles": runtime_mutable_input_roles,
            "bound_selected_identity": bound_identity,
            "resource_request": dict(resource_request),
            "max_retries": int(max_retries),
            "executable_identities": {"orca": executable},
        }
        if recovery_from is not None:
            snapshot["recovery"] = {
                "previous_generation_name": str(recovery_from.get("generation_name") or ""),
                "previous_execution_dir": str(recovery_seed_dir),
                "seeded_roles": seeded_roles,
                "checkpoint_role": recovery_checkpoint_role,
                "submitted_dependency_identities": (recovery_submitted_dependency_identities),
            }
        return snapshot
    except BaseException:
        try:
            cleanup_unowned_direct_generation_directory(
                resolved_job_dir,
                namespace=generation_name,
                label="ORCA execution snapshot",
                expected_job_identity=(job_dir_stat.st_dev, job_dir_stat.st_ino),
                expected_generation_identity=generation_identity,
                expected_owner_token=resolved_intent_token,
            )
        finally:
            discard_snapshot_intent_if_generations_absent(
                resolved_queue_root,
                resolved_intent_token,
            )
        raise


def orca_execution_snapshot_generation_dir(
    job_dir: str | Path,
    snapshot: Any,
) -> Path:
    """Return the exact visible generation owned by one ORCA submission."""

    if not isinstance(snapshot, Mapping):
        raise ValueError("ORCA execution snapshot must be an object")
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    execution_text = str(snapshot.get("execution_dir") or "").strip()
    raw_execution_dir = Path(execution_text).expanduser()
    if not execution_text or not raw_execution_dir.is_absolute():
        raise ValueError("Queued ORCA execution directory is invalid")
    execution_dir = raw_execution_dir.resolve()
    if (
        raw_execution_dir.is_symlink()
        or not execution_dir.is_dir()
        or execution_dir.parent != resolved_job_dir
        or not is_visible_generation_name(execution_dir.name)
        or str(snapshot.get("generation_name") or "") != execution_dir.name
    ):
        raise ValueError("Queued ORCA execution directory escapes its job directory")
    raw_identity = snapshot.get("execution_dir_identity")
    if not isinstance(raw_identity, Mapping):
        raise ValueError("Queued ORCA generation has no directory identity")
    details = execution_dir.stat()
    if (int(details.st_dev), int(details.st_ino)) != (
        int(raw_identity.get("device", -1)),
        int(raw_identity.get("inode", -1)),
    ):
        raise ValueError("Queued ORCA generation directory identity changed")
    return execution_dir


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


def _verified_identity_payload(
    identity: Any,
    *,
    root: Path,
    label: str,
) -> tuple[Path, bytes]:
    """Read identity-bound input bytes once and verify those exact bytes."""

    if not isinstance(identity, Mapping):
        raise ValueError(f"Queued ORCA execution snapshot has no {label} identity")
    path = require_confined_regular_file(
        root,
        Path(str(identity.get("path") or "")).expanduser(),
        label=f"Queued ORCA {label} snapshot",
    )
    payload = read_stable_regular_file(
        path,
        # The bound input may be larger than either source file after a
        # same-stem XYZ dependency is inlined. Construction already caps this
        # rewritten artifact against the aggregate snapshot budget.
        max_bytes=MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES,
        require_single_link=True,
    )
    current = {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    if current != dict(identity):
        raise ValueError(f"Queued ORCA {label} snapshot is corrupt")
    return path, payload


def _verify_source_descriptor(
    descriptor: Any,
    *,
    role: str,
    expected_source: str,
    job_dir: Path,
) -> tuple[Mapping[str, Any], Path]:
    if not isinstance(descriptor, Mapping) or str(descriptor.get("role") or "") != role:
        raise ValueError(f"Queued ORCA source descriptor {role!r} is invalid")
    raw_source = descriptor.get("source_path")
    if not isinstance(raw_source, str) or not raw_source:
        raise ValueError(f"Queued ORCA source descriptor {role!r} has an invalid path")
    source_text = raw_source
    if source_text != expected_source:
        raise ValueError(f"Queued ORCA source descriptor {role!r} has a mismatched path")
    try:
        source_path = Path(source_text)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"Queued ORCA source descriptor {role!r} has an invalid path") from exc
    if (
        not source_path.is_absolute()
        or source_text != str(source_path)
        or ".." in source_path.parts
        or "\x00" in source_text
    ):
        raise ValueError(
            f"Queued ORCA source descriptor {role!r} does not store a canonical absolute path"
        )
    if not source_path.is_relative_to(job_dir):
        raise ValueError(f"Queued ORCA source descriptor {role!r} escapes its job directory")
    digest = str(descriptor.get("sha256") or "").strip().lower()
    size = descriptor.get("size_bytes")
    if (
        len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        raise ValueError(f"Queued ORCA source descriptor {role!r} has invalid content identity")
    return descriptor, source_path


def verify_orca_execution_snapshot(
    job_dir: str | Path,
    snapshot: Any,
    *,
    expected_selected_inp: str | Path,
    expected_source_selected_inp: str | Path,
    expected_selected_input_xyz: str,
    expected_resource_request: Mapping[str, int],
    expected_max_retries: int,
    allow_runtime_outputs: bool = False,
) -> tuple[Path, str]:
    """Verify a queued private ORCA input tree and its bound executable identity."""

    resolved_job_dir = Path(job_dir).expanduser().resolve()
    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("version") != ORCA_EXECUTION_SNAPSHOT_VERSION
    ):
        raise ValueError("Queue metadata 'execution_snapshot' has an unsupported version")
    execution_dir = orca_execution_snapshot_generation_dir(resolved_job_dir, snapshot)

    job_identity = snapshot.get("job_dir_identity")
    if not isinstance(job_identity, Mapping):
        raise ValueError("Queued ORCA execution snapshot has no job directory identity")
    job_details = resolved_job_dir.stat()
    if (int(job_details.st_dev), int(job_details.st_ino)) != (
        int(job_identity.get("device", -1)),
        int(job_identity.get("inode", -1)),
    ):
        raise ValueError("Queued ORCA job directory identity changed")

    raw_inputs = snapshot.get("source_inputs")
    materialized_inputs = snapshot.get("materialized_inputs")
    dependency_paths = snapshot.get("dependency_paths")
    runtime_mutable_input_roles = snapshot.get("runtime_mutable_input_roles")
    if (
        not isinstance(raw_inputs, Mapping)
        or not isinstance(materialized_inputs, Mapping)
        or not isinstance(dependency_paths, list)
        or not isinstance(runtime_mutable_input_roles, list)
        or any(not isinstance(path, str) or not path.strip() for path in dependency_paths)
        or any(not isinstance(role, str) or not role for role in runtime_mutable_input_roles)
        or len(runtime_mutable_input_roles) != len(set(runtime_mutable_input_roles))
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
    mutable_roles = set(runtime_mutable_input_roles)
    if not mutable_roles.issubset(expected_dependency_roles):
        raise ValueError("Queued ORCA execution snapshot has invalid mutable input roles")
    recovery_block = snapshot.get("recovery")
    recovery_checkpoint_role = (
        str(recovery_block.get("checkpoint_role") or "")
        if isinstance(recovery_block, Mapping)
        else ""
    )
    if recovery_checkpoint_role and (
        recovery_checkpoint_role not in expected_dependency_roles
        or recovery_checkpoint_role in mutable_roles
    ):
        raise ValueError("Queued ORCA recovery checkpoint role is invalid")

    source_selected = str(snapshot.get("source_selected_inp") or "").strip()
    if source_selected != str(expected_source_selected_inp):
        raise ValueError("Queued ORCA source selected input does not match its queue metadata")
    _selected_descriptor, source_selected_path = _verify_source_descriptor(
        raw_inputs["selected_source"],
        role="selected_source",
        expected_source=source_selected,
        job_dir=resolved_job_dir,
    )
    verified_dependencies: list[tuple[str, Mapping[str, Any], Path]] = []
    for index, expected_source in enumerate(dependency_paths):
        role = f"dependency_{index:06d}"
        descriptor, source_path = _verify_source_descriptor(
            raw_inputs[role],
            role=role,
            expected_source=expected_source,
            job_dir=resolved_job_dir,
        )
        verified_dependencies.append((role, descriptor, source_path))
    verified_source_paths = [
        source_path for _role, _descriptor, source_path in verified_dependencies
    ]
    if len(verified_source_paths) != len(set(verified_source_paths)):
        raise ValueError("Queued ORCA execution snapshot has duplicate dependency source paths")
    if source_selected_path in verified_source_paths:
        raise ValueError("Queued ORCA execution snapshot lists its selected input as a dependency")
    _validate_unique_dependency_basenames(
        verified_source_paths,
        job_dir=resolved_job_dir,
    )
    if any(source_path.name == source_selected_path.name for source_path in verified_source_paths):
        raise ValueError(
            "Queued ORCA dependency basename conflicts with the selected input in the flat "
            f"generation: {source_selected_path.name}"
        )
    verified_private_paths: dict[str, Path] = {}
    for role, descriptor, source_path in verified_dependencies:
        materialized_identity = materialized_inputs[role]
        if not isinstance(materialized_identity, Mapping):
            raise ValueError(f"Queued ORCA dependency {role!r} has no materialized identity")
        if materialized_identity.get("sha256") != descriptor.get(
            "sha256"
        ) or materialized_identity.get("size_bytes") != descriptor.get("size_bytes"):
            raise ValueError(f"Queued ORCA dependency {role!r} mismatches its source snapshot")
        if role in mutable_roles and allow_runtime_outputs:
            private_path = require_confined_regular_file(
                execution_dir,
                Path(str(materialized_identity.get("path") or "")),
                label=f"Queued ORCA mutable runtime output {role!r}",
            )
        else:
            private_path = _verify_identity(
                materialized_identity,
                root=execution_dir,
                label=f"private dependency {role!r}",
            )
        if role == recovery_checkpoint_role:
            # The seeded checkpoint is the one sanctioned rename: a crashed
            # generation's runtime attempt checkpoint (base, retry, or resume
            # stem) materialized under the `%moinp`-safe `<stem>.moinp.gbw`
            # name. The source name is provenance validated purely by shape —
            # content is hash-pinned above — so verification never depends on
            # re-reading a mutable file to rebuild the attempt ladder.
            selected_source_name = Path(source_selected)
            if (
                private_path.parent != execution_dir
                or not _is_recovery_checkpoint_source_name(
                    source_path.name,
                    source_selected=selected_source_name,
                )
                or private_path.name != recovery_checkpoint_private_name(selected_source_name)
            ):
                raise ValueError(
                    f"Queued ORCA recovery checkpoint {role!r} violates its rename contract"
                )
        elif private_path.parent != execution_dir or private_path.name != source_path.name:
            raise ValueError(
                f"Queued ORCA dependency {role!r} does not preserve its source basename"
            )
        if (
            role in mutable_roles
            and private_path.name != Path(source_selected).with_suffix(".xyz").name
        ):
            raise ValueError(f"Queued ORCA mutable dependency {role!r} is not same-stem XYZ")
        verified_private_paths[role] = private_path

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
        or selected.name != Path(source_selected).name
    ):
        raise ValueError("Queued ORCA selected input does not match its private snapshot")
    try:
        selected_text = read_stable_regular_file(
            selected,
            require_single_link=True,
        ).decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("Queued ORCA bound selected input must be UTF-8 text") from exc
    selected_lines = selected_text.splitlines()
    bound_references = scan_orca_file_references(selected_lines)
    hessian_requested = _route_requests_hessian(selected_lines)
    same_stem_xyz_is_output = _route_writes_same_stem_xyz(selected_lines)
    effective_retry_count = effective_max_retries(
        selected,
        configured_max_retries=expected_max_retries,
    )
    for role, _descriptor, source_path in verified_dependencies:
        if role == recovery_checkpoint_role:
            # The checkpoint's source deliberately carries the runtime-owned
            # `<stem>.gbw` name inside the frozen crashed generation; its
            # naming is enforced by the rename contract above instead.
            continue
        _validate_dependency_basename(
            source_path,
            source_selected_path,
            effective_retry_count=effective_retry_count,
            hessian_requested=hessian_requested,
            same_stem_xyz_is_output=same_stem_xyz_is_output,
            inline_same_stem_xyz=role in mutable_roles,
        )
    bound_reference_paths = {
        _reference_source(execution_dir, selected, reference.value)
        for reference in bound_references
    }
    expected_bound_reference_paths = {
        private_path
        for role, private_path in verified_private_paths.items()
        if role not in mutable_roles
    }
    if bound_reference_paths != expected_bound_reference_paths:
        raise ValueError(
            "Queued ORCA bound input references do not match its materialized dependencies"
        )
    if verified_source_paths != sorted(
        verified_source_paths,
        key=lambda path: path.relative_to(resolved_job_dir).as_posix(),
    ):
        raise ValueError("Queued ORCA dependency roles are not in canonical source path order")
    if mutable_roles:
        if _inline_geometry_atom_count(selected_text) is None or any(
            reference.kind == "geometry" for reference in bound_references
        ):
            raise ValueError("Queued ORCA mutable same-stem XYZ is not bound as inline geometry")
    if str(snapshot.get("selected_input_xyz") or "") != str(expected_selected_input_xyz or ""):
        raise ValueError("Queued ORCA selected geometry does not match its execution snapshot")
    if snapshot.get("resource_request") != dict(expected_resource_request):
        raise ValueError("Queued ORCA resource request does not match its execution snapshot")
    if snapshot.get("max_retries") != int(expected_max_retries):
        raise ValueError("Queued ORCA retry budget does not match its execution snapshot")

    executable = verify_orca_snapshot_executable(snapshot)
    return selected, executable


def cleanup_unowned_orca_execution_snapshot(job_dir: str | Path, snapshot: Any) -> None:
    """Remove a visible generation that never acquired a durable queue owner."""

    if not isinstance(snapshot, Mapping):
        return
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    job_dir_identity = snapshot.get("job_dir_identity")
    if not isinstance(job_dir_identity, Mapping):
        raise ValueError("Refusing to clean an ORCA snapshot without job identity")
    job_dir_stat = resolved_job_dir.stat()
    if (int(job_dir_stat.st_dev), int(job_dir_stat.st_ino)) != (
        int(job_dir_identity.get("device", -1)),
        int(job_dir_identity.get("inode", -1)),
    ):
        raise ValueError("Refusing to clean an ORCA snapshot from a changed job directory")
    expected_job_identity = (int(job_dir_stat.st_dev), int(job_dir_stat.st_ino))
    namespace = str(snapshot.get("generation_name") or "").strip()
    raw_execution_dir = Path(str(snapshot.get("execution_dir") or "")).expanduser()
    execution_dir = resolved_job_dir / namespace
    if not is_visible_generation_name(namespace) or raw_execution_dir != execution_dir:
        raise ValueError("Refusing to clean a mismatched ORCA execution generation")
    if (
        execution_dir.is_symlink()
        or raw_execution_dir.is_symlink()
        or execution_dir.parent != resolved_job_dir
    ):
        raise ValueError("Refusing to clean an unconfined ORCA execution snapshot")
    raw_generation_identity = snapshot.get("execution_dir_identity")
    if not isinstance(raw_generation_identity, Mapping):
        raise ValueError("Refusing to clean an ORCA snapshot without generation identity")
    expected_generation_identity = (
        int(raw_generation_identity.get("device", -1)),
        int(raw_generation_identity.get("inode", -1)),
    )
    intent_token = str(snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY) or "").strip()
    intent_root = str(snapshot.get(SNAPSHOT_INTENT_QUEUE_ROOT_KEY) or "").strip()
    if not intent_token or not intent_root:
        raise ValueError("Refusing to clean an ORCA snapshot without owner identity")
    cleanup_succeeded = False
    try:
        cleanup_unowned_direct_generation_directory(
            resolved_job_dir,
            namespace=namespace,
            label="ORCA execution snapshot",
            expected_job_identity=expected_job_identity,
            expected_generation_identity=expected_generation_identity,
            expected_owner_token=intent_token,
        )
        cleanup_succeeded = True
    finally:
        if cleanup_succeeded and intent_token and intent_root:
            discard_snapshot_intent_if_generations_absent(intent_root, intent_token)


__all__ = [
    "MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES",
    "ORCA_EXECUTION_SNAPSHOT_VERSION",
    "build_orca_execution_snapshot",
    "cleanup_unowned_orca_execution_snapshot",
    "orca_execution_provenance",
    "orca_execution_snapshot_generation_dir",
    "orca_execution_started_evidence",
    "verify_orca_execution_snapshot",
]
