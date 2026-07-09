from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def mapping_metadata(entry: Mapping[str, Any] | None) -> dict[str, Any]:
    metadata = (entry or {}).get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def mapping_metadata_value(entry: Mapping[str, Any] | None, key: str) -> Any:
    return mapping_metadata(entry).get(key)


def entry_metadata_value(entry: Any, key: str, default: Any = "") -> Any:
    metadata = getattr(entry, "metadata", {})
    getter = getattr(metadata, "get", None)
    if getter is None:
        return default
    return getter(key, default)


def entry_metadata_text(entry: Any, key: str, default: Any = "") -> str:
    return str(entry_metadata_value(entry, key, default)).strip()


def _resolved_absolute_path(value: Any, *, label: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{label} must be an absolute path string.")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label} is required.")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path: {text!r}")
    try:
        return candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} cannot be resolved: {text!r}") from exc


def entry_metadata_resolved_path(entry: Any, key: str, default: Any = "") -> Path:
    return _resolved_absolute_path(
        entry_metadata_value(entry, key, default),
        label=f"Queue metadata {key!r}",
    )


def require_path_within_root(
    path: str | Path,
    root: str | Path,
    *,
    label: str = "Path",
) -> Path:
    resolved_path = _resolved_absolute_path(path, label=label)
    resolved_root = _resolved_absolute_path(root, label="Allowed root")
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"{label} must be under allowed root: {resolved_root}. got={resolved_path}"
        ) from exc
    return resolved_path


def require_path_within_roots(
    path: str | Path,
    roots: Iterable[str | Path],
    *,
    label: str = "Path",
) -> Path:
    resolved_path = _resolved_absolute_path(path, label=label)
    resolved_roots = tuple(_resolved_absolute_path(root, label="Allowed root") for root in roots)
    for resolved_root in resolved_roots:
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError:
            continue
        return resolved_path
    roots_text = ", ".join(str(root) for root in resolved_roots) or "<none>"
    raise ValueError(f"{label} must be under an allowed root: {roots_text}. got={resolved_path}")


def entry_metadata_dict(entry: Any, key: str) -> dict[str, Any]:
    payload = entry_metadata_value(entry, key, {})
    return dict(payload) if isinstance(payload, dict) else {}


def object_attribute_fields(source: Any, *field_names: str) -> dict[str, Any]:
    return {field_name: getattr(source, field_name) for field_name in field_names}


__all__ = [
    "entry_metadata_dict",
    "entry_metadata_resolved_path",
    "entry_metadata_text",
    "entry_metadata_value",
    "mapping_metadata",
    "mapping_metadata_value",
    "object_attribute_fields",
    "require_path_within_root",
    "require_path_within_roots",
]
