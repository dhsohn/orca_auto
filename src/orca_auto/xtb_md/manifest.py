from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.config.files import (
    MAX_JOB_MANIFEST_BYTES,
    UniqueKeySafeLoader,
    _validate_yaml_events,
    _validate_yaml_object_graph,
)
from orca_auto.core.queue.engine.input_snapshot import (
    MAX_INPUT_SNAPSHOT_BYTES,
    read_stable_regular_file,
)

MANIFEST_FILE_NAME = "xtb_md_job.yaml"
SCHEMA_VERSION = 1

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "input_xyz",
        "gfn",
        "charge",
        "uhf",
        "ensemble",
        "temperature_k",
        "time_ps",
        "walltime_seconds",
        "step_fs",
        "dump_fs",
        "hydrogen_mass_amu",
        "shake",
        "scc_accuracy",
        "solvent_model",
        "solvent",
        "resources",
    }
)
_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "input_xyz",
        "gfn",
        "ensemble",
        "temperature_k",
        "time_ps",
        "walltime_seconds",
        "step_fs",
        "dump_fs",
    }
)
_RESOURCE_FIELDS = frozenset({"max_cores", "max_memory_gb"})
_ELEMENT_SYMBOLS = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn "
    "Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La "
    "Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po "
    "At Rn"
).split()
_ATOMIC_NUMBERS = {symbol.casefold(): index for index, symbol in enumerate(_ELEMENT_SYMBOLS, 1)}
_ELEMENT_RE = re.compile(r"[A-Za-z]{1,2}")
_FINITE_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?")
_TRAJECTORY_FRAME_OVERHEAD_BYTES = 1024
_TRAJECTORY_ATOM_ROW_BYTES = 128


@dataclass(frozen=True)
class XtbMdManifestLimits:
    """Server-owned admission limits applied before a job can be queued."""

    max_atoms: int
    max_steps: int
    max_atom_steps: int
    max_frames: int
    max_walltime_seconds: int
    max_cores: int
    max_memory_gb: int
    max_trajectory_bytes: int = 1_073_741_824
    max_abs_charge: int = 100
    max_uhf: int = 100
    max_abs_coordinate: float = 1_000_000.0
    min_temperature_k: float = 1.0
    max_temperature_k: float = 5_000.0
    min_step_fs: float = 0.01
    max_step_fs: float = 5.0
    min_scc_accuracy: float = 0.01
    max_scc_accuracy: float = 5.0
    max_hydrogen_mass_amu: int = 4

    def __post_init__(self) -> None:
        for field_name, value in (
            ("max_atoms", self.max_atoms),
            ("max_steps", self.max_steps),
            ("max_atom_steps", self.max_atom_steps),
            ("max_frames", self.max_frames),
            ("max_walltime_seconds", self.max_walltime_seconds),
            ("max_cores", self.max_cores),
            ("max_memory_gb", self.max_memory_gb),
            ("max_trajectory_bytes", self.max_trajectory_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"xTB-MD limit {field_name!r} must be a positive integer")
        for minimum_name, maximum_name in (
            ("min_temperature_k", "max_temperature_k"),
            ("min_step_fs", "max_step_fs"),
            ("min_scc_accuracy", "max_scc_accuracy"),
        ):
            minimum = getattr(self, minimum_name)
            maximum = getattr(self, maximum_name)
            if (
                isinstance(minimum, bool)
                or isinstance(maximum, bool)
                or not isinstance(minimum, (int, float))
                or not isinstance(maximum, (int, float))
                or not math.isfinite(float(minimum))
                or not math.isfinite(float(maximum))
                or float(minimum) <= 0
                or float(maximum) < float(minimum)
            ):
                raise ValueError(
                    f"xTB-MD limits {minimum_name!r}/{maximum_name!r} must form a "
                    "positive finite range"
                )
        if (
            isinstance(self.max_abs_coordinate, bool)
            or not isinstance(self.max_abs_coordinate, (int, float))
            or not math.isfinite(float(self.max_abs_coordinate))
            or float(self.max_abs_coordinate) <= 0
        ):
            raise ValueError("xTB-MD limit 'max_abs_coordinate' must be positive and finite")
        if (
            isinstance(self.max_hydrogen_mass_amu, bool)
            or not isinstance(self.max_hydrogen_mass_amu, int)
            or self.max_hydrogen_mass_amu < 0
        ):
            raise ValueError("xTB-MD limit 'max_hydrogen_mass_amu' must be non-negative")
        for field_name, value in (
            ("max_abs_charge", self.max_abs_charge),
            ("max_uhf", self.max_uhf),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"xTB-MD limit {field_name!r} must be non-negative")


@dataclass(frozen=True)
class XtbMdResourceOverrides:
    max_cores: int | None = None
    max_memory_gb: int | None = None


@dataclass(frozen=True)
class XtbMdManifest:
    schema_version: int
    input_xyz: Path
    atom_symbols: tuple[str, ...]
    atom_count: int
    gfn: int
    charge: int
    uhf: int
    ensemble: str
    temperature_k: float
    time_ps: float
    walltime_seconds: int
    step_fs: float
    dump_fs: float
    hydrogen_mass_amu: int
    shake: int
    scc_accuracy: float
    solvent_model: str
    solvent: str
    resources: XtbMdResourceOverrides
    expected_steps: int
    dump_interval_steps: int
    expected_frames: int
    atom_steps: int
    estimated_trajectory_bytes: int
    max_abs_coordinate: float

    def public_dict(self) -> dict[str, Any]:
        """Return the canonical user-controlled portion for snapshots and reports."""

        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "input_xyz": self.input_xyz.name,
            "gfn": self.gfn,
            "charge": self.charge,
            "uhf": self.uhf,
            "ensemble": self.ensemble,
            "temperature_k": self.temperature_k,
            "time_ps": self.time_ps,
            "walltime_seconds": self.walltime_seconds,
            "step_fs": self.step_fs,
            "dump_fs": self.dump_fs,
            "hydrogen_mass_amu": self.hydrogen_mass_amu,
            "shake": self.shake,
            "scc_accuracy": self.scc_accuracy,
        }
        if self.solvent:
            payload["solvent_model"] = self.solvent_model
            payload["solvent"] = self.solvent
        resources = {
            key: value
            for key, value in (
                ("max_cores", self.resources.max_cores),
                ("max_memory_gb", self.resources.max_memory_gb),
            )
            if value is not None
        }
        if resources:
            payload["resources"] = resources
        return payload


def _strict_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"xTB-MD manifest field {field!r} must be an integer")
    if not -(2**31) <= value < 2**31:
        raise ValueError(f"xTB-MD manifest field {field!r} is outside the supported integer range")
    return value


def _decimal_number(value: Any, *, field: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"xTB-MD manifest field {field!r} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"xTB-MD manifest field {field!r} must be finite")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"xTB-MD manifest field {field!r} must be a finite number") from exc
    if not parsed.is_finite():
        raise ValueError(f"xTB-MD manifest field {field!r} must be finite")
    if positive and parsed <= 0:
        raise ValueError(f"xTB-MD manifest field {field!r} must be greater than zero")
    try:
        converted = float(parsed)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"xTB-MD manifest field {field!r} is outside the supported range") from exc
    if not math.isfinite(converted):
        raise ValueError(f"xTB-MD manifest field {field!r} is outside the supported range")
    return parsed


def _exact_positive_ratio(numerator: Decimal, denominator: Decimal, *, field: str) -> int:
    ratio = numerator / denominator
    integral = ratio.to_integral_value()
    if ratio != integral or integral < 1:
        raise ValueError(f"xTB-MD {field} must be a positive integer multiple of step_fs")
    return int(integral)


def _require_xtb_binary64_ratio(
    numerator: Decimal,
    denominator: Decimal,
    *,
    expected: int,
    field: str,
    numerator_scale: float = 1.0,
) -> None:
    # xTB reads these values as binary64 and assigns the ratio to an integer,
    # which truncates toward zero.  Decimal-only arithmetic can otherwise admit
    # values such as 0.0003 ps / 0.1 fs as three steps while xTB executes two.
    try:
        binary64_ratio = float(numerator) * numerator_scale / float(denominator)
        if not math.isfinite(binary64_ratio):
            raise ValueError
        computed = int(binary64_ratio)
    except (OverflowError, ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"xTB-MD {field} is outside xTB's supported binary64 range") from exc
    if computed != expected:
        raise ValueError(f"xTB-MD {field} is not represented as the requested integer ratio by xTB")


def _require_decimal_range(
    value: Decimal,
    *,
    minimum: float,
    maximum: float,
    field: str,
) -> None:
    if value < Decimal(str(minimum)) or value > Decimal(str(maximum)):
        raise ValueError(
            f"xTB-MD manifest field {field!r} must be between {minimum:g} and {maximum:g}"
        )


def _reject_unknown_fields(
    mapping: Mapping[Any, Any], *, allowed: frozenset[str], field: str
) -> None:
    if any(not isinstance(key, str) for key in mapping):
        raise ValueError(f"xTB-MD manifest {field} keys must be strings")
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ValueError(f"Unknown xTB-MD manifest {field} fields: {unknown}")


def _required_text(raw: Mapping[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"xTB-MD manifest field {field!r} must be a non-empty string")
    if value != value.strip() or "\x00" in value:
        raise ValueError(f"xTB-MD manifest field {field!r} must use canonical text")
    return value


def load_xyz_geometry(
    path: str | Path,
    *,
    max_atoms: int,
    max_abs_coordinate: float = 1_000_000.0,
) -> tuple[str, ...]:
    """Validate one immutable-snapshot-compatible XYZ and return canonical elements."""

    if isinstance(max_atoms, bool) or not isinstance(max_atoms, int) or max_atoms < 1:
        raise ValueError("xTB-MD max_atoms must be a positive integer")
    if (
        isinstance(max_abs_coordinate, bool)
        or not isinstance(max_abs_coordinate, (int, float))
        or not math.isfinite(float(max_abs_coordinate))
        or float(max_abs_coordinate) <= 0
    ):
        raise ValueError("xTB-MD max_abs_coordinate must be positive and finite")
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError("xTB-MD input_xyz must not be a symlink")
    payload = read_stable_regular_file(
        candidate,
        max_bytes=MAX_INPUT_SNAPSHOT_BYTES,
        require_single_link=True,
    )
    try:
        lines = payload.decode("utf-8", errors="strict").splitlines()
    except UnicodeError as exc:
        raise ValueError("xTB-MD input_xyz must be UTF-8 text") from exc
    if len(lines) < 2:
        raise ValueError("xTB-MD input_xyz must contain one complete XYZ frame")
    try:
        atom_count = int(lines[0].strip())
    except ValueError as exc:
        raise ValueError("xTB-MD input_xyz has an invalid atom count") from exc
    if atom_count < 1:
        raise ValueError("xTB-MD input_xyz atom count must be positive")
    if atom_count > max_atoms:
        raise ValueError(f"xTB-MD input_xyz exceeds the server atom limit of {max_atoms}")
    if len(lines) != atom_count + 2:
        raise ValueError("xTB-MD input_xyz must contain exactly one complete XYZ frame")
    symbols: list[str] = []
    for index, line in enumerate(lines[2:], 1):
        tokens = line.split()
        if len(tokens) != 4 or not _ELEMENT_RE.fullmatch(tokens[0]):
            raise ValueError(f"xTB-MD input_xyz atom line {index} is invalid")
        symbol_key = tokens[0].casefold()
        if symbol_key not in _ATOMIC_NUMBERS:
            raise ValueError(f"xTB-MD input_xyz contains unsupported element {tokens[0]!r}")
        coordinate_tokens = tokens[1:4]
        if any(
            token.casefold().lstrip("+-") in {"nan", "inf", "infinity"}
            for token in coordinate_tokens
        ):
            raise ValueError(f"xTB-MD input_xyz atom line {index} has non-finite coordinates")
        if not all(_FINITE_NUMBER_RE.fullmatch(token) for token in coordinate_tokens):
            raise ValueError(f"xTB-MD input_xyz atom line {index} has an invalid coordinate")
        try:
            coordinates = tuple(
                float(token.replace("D", "E").replace("d", "e")) for token in coordinate_tokens
            )
        except ValueError as exc:
            raise ValueError(f"xTB-MD input_xyz atom line {index} is invalid") from exc
        if not all(math.isfinite(coordinate) for coordinate in coordinates):
            raise ValueError(f"xTB-MD input_xyz atom line {index} has non-finite coordinates")
        if any(abs(coordinate) > float(max_abs_coordinate) for coordinate in coordinates):
            raise ValueError(
                f"xTB-MD input_xyz atom line {index} exceeds the coordinate magnitude limit"
            )
        symbols.append(_ELEMENT_SYMBOLS[_ATOMIC_NUMBERS[symbol_key] - 1])
    return tuple(symbols)


def _resolve_input_xyz(
    job_dir: Path,
    raw_name: str,
    *,
    max_atoms: int,
    max_abs_coordinate: float,
) -> tuple[Path, tuple[str, ...]]:
    relative = Path(raw_name)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name != raw_name:
        raise ValueError("xTB-MD input_xyz must be one file name inside the job directory")
    if relative.suffix.lower() != ".xyz":
        raise ValueError("xTB-MD input_xyz must name an .xyz file")
    candidate = job_dir / relative
    symbols = load_xyz_geometry(
        candidate,
        max_atoms=max_atoms,
        max_abs_coordinate=max_abs_coordinate,
    )
    return candidate.resolve(), symbols


def _validate_electronic_state(symbols: tuple[str, ...], *, charge: int, uhf: int) -> None:
    nuclear_charge = sum(_ATOMIC_NUMBERS[symbol.casefold()] for symbol in symbols)
    electron_count = nuclear_charge - charge
    if electron_count < 0:
        raise ValueError("xTB-MD charge leaves a negative electron count")
    if uhf < 0 or uhf > electron_count:
        raise ValueError("xTB-MD uhf must be between zero and the electron count")
    if (electron_count - uhf) % 2:
        raise ValueError("xTB-MD electron count and uhf parity are inconsistent")


def _parse_resources(raw: Mapping[str, Any], limits: XtbMdManifestLimits) -> XtbMdResourceOverrides:
    resources_raw = raw.get("resources")
    if resources_raw is None:
        return XtbMdResourceOverrides()
    if not isinstance(resources_raw, Mapping):
        raise ValueError("xTB-MD manifest field 'resources' must be a mapping")
    _reject_unknown_fields(resources_raw, allowed=_RESOURCE_FIELDS, field="resources")
    parsed: dict[str, int | None] = {"max_cores": None, "max_memory_gb": None}
    for key, limit in (
        ("max_cores", limits.max_cores),
        ("max_memory_gb", limits.max_memory_gb),
    ):
        if key not in resources_raw:
            continue
        value = _strict_int(resources_raw[key], field=f"resources.{key}")
        if value < 1 or value > limit:
            raise ValueError(
                f"xTB-MD manifest resources.{key} must be between 1 and the server limit {limit}"
            )
        parsed[key] = value
    return XtbMdResourceOverrides(**parsed)


def _parse_solvent(raw: Mapping[str, Any]) -> tuple[str, str]:
    solvent_model_raw = raw.get("solvent_model", "")
    solvent_raw = raw.get("solvent", "")
    if not isinstance(solvent_model_raw, str) or not isinstance(solvent_raw, str):
        raise ValueError("xTB-MD solvent_model and solvent must be strings")
    if solvent_model_raw != solvent_model_raw.strip() or solvent_raw != solvent_raw.strip():
        raise ValueError("xTB-MD solvent fields must use canonical text")
    solvent_model = solvent_model_raw.lower()
    solvent = solvent_raw.lower()
    probe: list[str] = []
    _engine_runner.append_solvent_option(
        probe,
        {"solvent_model": solvent_model, "solvent": solvent},
    )
    return solvent_model, solvent


def parse_manifest(
    raw: Mapping[str, Any],
    *,
    job_dir: str | Path,
    limits: XtbMdManifestLimits,
) -> XtbMdManifest:
    if not isinstance(raw, Mapping):
        raise ValueError("xTB-MD manifest top level must be a mapping")
    _reject_unknown_fields(raw, allowed=_TOP_LEVEL_FIELDS, field="top-level")
    missing = sorted(_REQUIRED_FIELDS - set(raw))
    if missing:
        raise ValueError(f"Missing required xTB-MD manifest fields: {missing}")

    schema_version = _strict_int(raw["schema_version"], field="schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported xTB-MD manifest schema_version: {schema_version}")
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    if not resolved_job_dir.is_dir():
        raise ValueError(f"xTB-MD job directory does not exist: {resolved_job_dir}")
    input_xyz, atom_symbols = _resolve_input_xyz(
        resolved_job_dir,
        _required_text(raw, "input_xyz"),
        max_atoms=limits.max_atoms,
        max_abs_coordinate=limits.max_abs_coordinate,
    )

    gfn = _strict_int(raw["gfn"], field="gfn")
    if gfn not in {1, 2}:
        raise ValueError("xTB-MD gfn must be exactly 1 or 2")
    charge = _strict_int(raw.get("charge", 0), field="charge")
    uhf = _strict_int(raw.get("uhf", 0), field="uhf")
    if abs(charge) > limits.max_abs_charge:
        raise ValueError(
            f"xTB-MD charge exceeds the server magnitude limit {limits.max_abs_charge}"
        )
    if uhf > limits.max_uhf:
        raise ValueError(f"xTB-MD uhf exceeds the server limit {limits.max_uhf}")
    _validate_electronic_state(atom_symbols, charge=charge, uhf=uhf)

    ensemble = _required_text(raw, "ensemble")
    if ensemble not in {"nvt", "nve"}:
        raise ValueError("xTB-MD ensemble must be exactly 'nvt' or 'nve'")
    temperature = _decimal_number(raw["temperature_k"], field="temperature_k", positive=True)
    time_ps = _decimal_number(raw["time_ps"], field="time_ps", positive=True)
    step_fs = _decimal_number(raw["step_fs"], field="step_fs", positive=True)
    dump_fs = _decimal_number(raw["dump_fs"], field="dump_fs", positive=True)
    _require_decimal_range(
        temperature,
        minimum=limits.min_temperature_k,
        maximum=limits.max_temperature_k,
        field="temperature_k",
    )
    _require_decimal_range(
        step_fs,
        minimum=limits.min_step_fs,
        maximum=limits.max_step_fs,
        field="step_fs",
    )
    if dump_fs > time_ps * 1000:
        raise ValueError("xTB-MD dump_fs must not exceed the total simulation time")
    expected_steps = _exact_positive_ratio(time_ps * 1000, step_fs, field="time_ps")
    dump_interval_steps = _exact_positive_ratio(dump_fs, step_fs, field="dump_fs")
    if expected_steps > limits.max_steps:
        raise ValueError(f"xTB-MD job exceeds the server step limit of {limits.max_steps}")
    _require_xtb_binary64_ratio(
        time_ps,
        step_fs,
        expected=expected_steps,
        field="time_ps/step_fs",
        numerator_scale=1000.0,
    )
    _require_xtb_binary64_ratio(
        dump_fs,
        step_fs,
        expected=dump_interval_steps,
        field="dump_fs/step_fs",
    )
    expected_frames = (expected_steps + dump_interval_steps - 1) // dump_interval_steps
    atom_steps = len(atom_symbols) * expected_steps
    estimated_trajectory_bytes = expected_frames * (
        _TRAJECTORY_FRAME_OVERHEAD_BYTES + len(atom_symbols) * _TRAJECTORY_ATOM_ROW_BYTES
    )
    if expected_frames > limits.max_frames:
        raise ValueError(f"xTB-MD job exceeds the server frame limit of {limits.max_frames}")
    if atom_steps > limits.max_atom_steps:
        raise ValueError(
            f"xTB-MD job exceeds the server atom-step limit of {limits.max_atom_steps}"
        )
    if estimated_trajectory_bytes > limits.max_trajectory_bytes:
        raise ValueError(
            "xTB-MD estimated trajectory exceeds the server byte limit "
            f"{limits.max_trajectory_bytes}"
        )
    walltime_seconds = _strict_int(raw["walltime_seconds"], field="walltime_seconds")
    if walltime_seconds < 1 or walltime_seconds > limits.max_walltime_seconds:
        raise ValueError(
            "xTB-MD walltime_seconds must be between 1 and the server limit "
            f"{limits.max_walltime_seconds}"
        )

    hydrogen_mass = _strict_int(raw.get("hydrogen_mass_amu", 4), field="hydrogen_mass_amu")
    if not 0 <= hydrogen_mass <= limits.max_hydrogen_mass_amu:
        raise ValueError(
            "xTB-MD hydrogen_mass_amu must be between 0 and the server limit "
            f"{limits.max_hydrogen_mass_amu}"
        )
    shake = _strict_int(raw.get("shake", 2), field="shake")
    if shake not in {0, 1, 2}:
        raise ValueError("xTB-MD shake must be exactly 0, 1, or 2")
    scc_accuracy = _decimal_number(
        raw.get("scc_accuracy", 2.0), field="scc_accuracy", positive=True
    )
    _require_decimal_range(
        scc_accuracy,
        minimum=limits.min_scc_accuracy,
        maximum=limits.max_scc_accuracy,
        field="scc_accuracy",
    )
    solvent_model, solvent = _parse_solvent(raw)
    resources = _parse_resources(raw, limits)

    return XtbMdManifest(
        schema_version=schema_version,
        input_xyz=input_xyz,
        atom_symbols=atom_symbols,
        atom_count=len(atom_symbols),
        gfn=gfn,
        charge=charge,
        uhf=uhf,
        ensemble=ensemble,
        temperature_k=float(temperature),
        time_ps=float(time_ps),
        walltime_seconds=walltime_seconds,
        step_fs=float(step_fs),
        dump_fs=float(dump_fs),
        hydrogen_mass_amu=hydrogen_mass,
        shake=shake,
        scc_accuracy=float(scc_accuracy),
        solvent_model=solvent_model,
        solvent=solvent,
        resources=resources,
        expected_steps=expected_steps,
        dump_interval_steps=dump_interval_steps,
        expected_frames=expected_frames,
        atom_steps=atom_steps,
        estimated_trajectory_bytes=estimated_trajectory_bytes,
        max_abs_coordinate=float(limits.max_abs_coordinate),
    )


def load_manifest(
    job_dir: str | Path,
    *,
    limits: XtbMdManifestLimits,
) -> XtbMdManifest:
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    manifest_path = resolved_job_dir / MANIFEST_FILE_NAME
    if not manifest_path.exists():
        raise ValueError(f"Missing xTB-MD job manifest: {manifest_path}")
    payload = read_stable_regular_file(
        manifest_path,
        max_bytes=MAX_JOB_MANIFEST_BYTES,
        require_single_link=True,
    )
    try:
        text = payload.decode("utf-8", errors="strict")
        _validate_yaml_events(text)
        parsed = yaml.load(text, Loader=UniqueKeySafeLoader)
        _validate_yaml_object_graph(parsed)
    except UnicodeError as exc:
        raise ValueError("xTB-MD manifest must be UTF-8 text") from exc
    except yaml.YAMLError as exc:
        raise ValueError("Invalid xTB-MD YAML manifest") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError("xTB-MD manifest top level must be a mapping")
    return parse_manifest(parsed, job_dir=resolved_job_dir, limits=limits)


__all__ = [
    "MANIFEST_FILE_NAME",
    "SCHEMA_VERSION",
    "XtbMdManifest",
    "XtbMdManifestLimits",
    "XtbMdResourceOverrides",
    "load_xyz_geometry",
    "load_manifest",
    "parse_manifest",
]
