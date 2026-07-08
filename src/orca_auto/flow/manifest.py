from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from orca_auto.core.utils import normalize_bool, normalize_text

FLOW_MANIFEST_FILENAMES = ("flow.yaml",)


def manifest_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if normalize_text(key)}


def load_flow_manifest(
    directory: Path,
    *,
    filenames: tuple[str, ...] = FLOW_MANIFEST_FILENAMES,
    description: str = "Workflow manifest",
) -> dict[str, Any]:
    for name in filenames:
        candidate = directory / name
        if not candidate.is_file():
            continue
        try:
            parsed = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(_invalid_manifest_yaml_message(candidate, description, exc)) from exc
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise ValueError(f"{description} must contain a mapping: {candidate}")
        return dict(parsed)
    return {}


def _invalid_manifest_yaml_message(path: Path, description: str, exc: yaml.YAMLError) -> str:
    message = f"Invalid {description}: {path}"
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        message = f"{message} (line {mark.line + 1}, column {mark.column + 1})"
    problem = normalize_text(getattr(exc, "problem", None))
    if problem:
        message = f"{message}: {problem}"
    return message


def _reject_windows_manifest_path_syntax(text: str, *, field_name: str) -> None:
    windows = PureWindowsPath(text)
    if "\\" in text or bool(windows.drive):
        raise ValueError(
            f"{field_name} must use Linux/POSIX path syntax, not Windows path syntax: {text!r}"
        )


def manifest_allows_external_inputs(manifest: dict[str, Any]) -> bool:
    return normalize_bool(manifest.get("allow_external_inputs"), default=False)


def _require_manifest_path_inside_base(
    *,
    base_dir: Path,
    target: Path,
    raw_text: str,
    field_name: str,
) -> None:
    base_resolved = base_dir.expanduser().resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must resolve inside workflow_dir unless "
            f"allow_external_inputs: true is set: {raw_text!r}"
        ) from exc


def resolve_manifest_file_value(
    base_dir: Path,
    value: Any,
    *,
    allow_external_inputs: bool = False,
    field_name: str = "manifest file path",
) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    _reject_windows_manifest_path_syntax(text, field_name=field_name)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    resolved = candidate.resolve()
    if not allow_external_inputs:
        _require_manifest_path_inside_base(
            base_dir=base_dir,
            target=resolved,
            raw_text=text,
            field_name=field_name,
        )
    return str(resolved)


def resolve_engine_manifest(base_dir: Path, manifest: dict[str, Any], key: str) -> dict[str, Any]:
    section = manifest_mapping(manifest.get(key))
    if not section:
        return {}
    resolved = dict(section)
    if "xcontrol_file" in resolved:
        resolved["xcontrol_file"] = resolve_manifest_file_value(
            base_dir,
            resolved.get("xcontrol_file"),
            allow_external_inputs=manifest_allows_external_inputs(manifest),
            field_name=f"{key}.xcontrol_file",
        )
    return resolved


def resolve_engine_manifest_with_presence(
    base_dir: Path,
    manifest: dict[str, Any],
    key: str,
) -> tuple[bool, dict[str, Any]]:
    if not isinstance(manifest.get(key), dict):
        return False, {}
    return True, resolve_engine_manifest(base_dir, manifest, key)


def resolve_endpoint_pairing_manifest(
    manifest: dict[str, Any],
    xtb_manifest: dict[str, Any],
) -> dict[str, Any]:
    xtb_section = manifest_mapping(xtb_manifest.pop("endpoint_pairing", None))
    top_level = manifest_mapping(manifest.get("endpoint_pairing"))
    resolved = dict(xtb_section)
    resolved.update(top_level)
    return resolved


__all__ = [
    "FLOW_MANIFEST_FILENAMES",
    "load_flow_manifest",
    "manifest_allows_external_inputs",
    "manifest_mapping",
    "resolve_endpoint_pairing_manifest",
    "resolve_engine_manifest",
    "resolve_engine_manifest_with_presence",
    "resolve_manifest_file_value",
]
