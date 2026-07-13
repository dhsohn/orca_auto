from __future__ import annotations

import os
import warnings
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent, NodeEvent

from orca_auto.core.paths import is_rejected_windows_path
from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.core.utils.coercion import normalize_text

ORCA_AUTO_CONFIG_ENV_VAR = "ORCA_AUTO_CONFIG"
DEFAULT_CONFIG_FILENAME = "orca_auto.yaml"
DEFAULT_SHARED_ADMISSION_DIRNAME = ".admission"
SECURE_CONFIG_FILE_MODE = 0o600
YAML_CONFIG_LOAD_EXCEPTIONS = (OSError, ValueError, yaml.YAMLError)
MAX_JOB_MANIFEST_BYTES = 1024 * 1024
MAX_JOB_MANIFEST_ALIASES = 32
MAX_JOB_MANIFEST_NODES = 10_000
MAX_JOB_MANIFEST_DEPTH = 64


def _validate_yaml_events(payload: str) -> None:
    aliases = 0
    nodes = 0
    depth = 0
    active_anchors: list[str | None] = []
    for event in yaml.parse(payload, Loader=yaml.SafeLoader):
        if isinstance(event, AliasEvent):
            aliases += 1
            if aliases > MAX_JOB_MANIFEST_ALIASES:
                raise ValueError(
                    f"YAML manifest exceeds the {MAX_JOB_MANIFEST_ALIASES}-alias limit"
                )
            if event.anchor in active_anchors:
                raise ValueError("YAML manifest contains a recursive alias cycle")
            continue
        if isinstance(event, NodeEvent):
            nodes += 1
            if nodes > MAX_JOB_MANIFEST_NODES:
                raise ValueError(f"YAML manifest exceeds the {MAX_JOB_MANIFEST_NODES}-node limit")
        if isinstance(event, CollectionStartEvent):
            depth += 1
            if depth > MAX_JOB_MANIFEST_DEPTH:
                raise ValueError(
                    f"YAML manifest exceeds the {MAX_JOB_MANIFEST_DEPTH}-level nesting limit"
                )
            active_anchors.append(event.anchor)
        elif isinstance(event, CollectionEndEvent):
            depth -= 1
            active_anchors.pop()


def _validate_yaml_object_graph(value: Any) -> None:
    expanded_nodes = 0
    active_containers: set[int] = set()

    def visit(current: Any, depth: int) -> None:
        nonlocal expanded_nodes
        expanded_nodes += 1
        if expanded_nodes > MAX_JOB_MANIFEST_NODES:
            raise ValueError(
                f"YAML manifest expands beyond the {MAX_JOB_MANIFEST_NODES}-node limit"
            )
        if depth > MAX_JOB_MANIFEST_DEPTH:
            raise ValueError(
                f"YAML manifest exceeds the {MAX_JOB_MANIFEST_DEPTH}-level nesting limit"
            )
        if not isinstance(current, (dict, list)):
            return
        identity = id(current)
        if identity in active_containers:
            raise ValueError("YAML manifest contains a recursive object graph")
        active_containers.add(identity)
        try:
            if isinstance(current, dict):
                for key, item in current.items():
                    visit(key, depth + 1)
                    visit(item, depth + 1)
            else:
                for item in current:
                    visit(item, depth + 1)
        finally:
            active_containers.remove(identity)

    visit(value, 0)


def load_bounded_yaml_data(
    path: str | Path,
    *,
    max_bytes: int = MAX_JOB_MANIFEST_BYTES,
) -> Any:
    """Load one bounded regular YAML file and reject pathological object graphs."""

    manifest_path = Path(path).expanduser()
    payload = read_stable_regular_file(
        manifest_path,
        max_bytes=max_bytes,
        require_single_link=True,
    )
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError(f"YAML manifest must be UTF-8 text: {manifest_path}") from exc
    _validate_yaml_events(text)
    parsed = yaml.safe_load(text)
    _validate_yaml_object_graph(parsed)
    return parsed


def config_env_value(env_var: str = ORCA_AUTO_CONFIG_ENV_VAR) -> str:
    return os.getenv(env_var, "").strip()


def secure_config_file_permissions(
    config_path: str | Path,
    *,
    mode: int = SECURE_CONFIG_FILE_MODE,
) -> None:
    Path(config_path).chmod(mode)


def default_config_path_from_repo_root(
    repo_root: Path,
    *,
    env_var: str = ORCA_AUTO_CONFIG_ENV_VAR,
) -> str:
    env_path = config_env_value(env_var)
    if env_path:
        return env_path

    repo_default = repo_root / "config" / DEFAULT_CONFIG_FILENAME
    if repo_default.exists():
        return str(repo_default)

    home_default = Path.home() / "orca_auto" / "config" / DEFAULT_CONFIG_FILENAME
    if home_default.exists():
        return str(home_default)

    return str(repo_default)


def discover_shared_config_path(
    explicit: str | Path | None,
    repo_root: Path,
    *,
    env_var: str = ORCA_AUTO_CONFIG_ENV_VAR,
) -> str | None:
    explicit_text = str(explicit or "").strip()
    if explicit_text:
        return str(Path(explicit_text).expanduser().resolve())

    discovered = default_config_path_from_repo_root(repo_root, env_var=env_var)
    if config_env_value(env_var):
        return str(Path(discovered).expanduser().resolve())

    path = Path(discovered).expanduser().resolve()
    return str(path) if path.exists() else None


def load_yaml_mapping(
    config_path: str | Path,
    *,
    invalid_message: str = "YAML top-level is not a mapping: {path}",
) -> tuple[Path, dict[str, Any]]:
    path = Path(config_path).expanduser().resolve()
    try:
        with path.open("r", encoding="utf-8") as handle:
            parsed = yaml.safe_load(handle) or {}
    except yaml.YAMLError:
        # PyYAML's exception text includes source snippets. Config files contain
        # bot tokens, so never propagate the raw parser message.
        raise ValueError(f"Invalid YAML syntax: {path}") from None
    if not isinstance(parsed, dict):
        raise ValueError(invalid_message.format(path=path))
    return path, parsed


def load_required_yaml_mapping(
    config_path: str | Path,
    *,
    missing_error: Callable[[Path], Exception] | None = None,
    invalid_message: str = "YAML top-level is not a mapping: {path}",
) -> tuple[Path, dict[str, Any]]:
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        if missing_error is not None:
            raise missing_error(path)
        raise FileNotFoundError(path)
    return load_yaml_mapping(path, invalid_message=invalid_message)


def mapping_section(raw: dict[str, Any] | None, key: str) -> dict[str, Any]:
    section = raw.get(key) if isinstance(raw, dict) else None
    return section if isinstance(section, dict) else {}


def _configured_mapping_section(
    raw: Mapping[str, Any],
    key: str,
    *,
    field_name: str | None = None,
) -> dict[str, Any]:
    if key not in raw:
        return {}
    section = raw.get(key)
    if not isinstance(section, Mapping):
        field = field_name or key
        raise ValueError(f"{field} section must be a mapping when configured.")
    return dict(section)


def validate_shared_config_sections(raw: Mapping[str, Any]) -> None:
    """Validate the mapping shape of shared execution-control sections."""

    _configured_mapping_section(raw, "scheduler")
    _configured_mapping_section(raw, "resources")
    workflow = _configured_mapping_section(raw, "workflow")
    _configured_mapping_section(workflow, "paths", field_name="workflow.paths")


def messenger_mapping_from_root(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the canonical ``messenger`` mapping with legacy Telegram support.

    ``messenger.telegram`` is authoritative whenever that key is present.  The
    legacy top-level ``telegram`` value is copied only when the nested key is
    absent; the two mappings are deliberately never merged field-by-field.  In
    particular, an explicitly empty nested mapping must not be repopulated with
    credentials from the legacy location.
    """
    root = raw if isinstance(raw, Mapping) else {}
    messenger_raw = root.get("messenger")
    if messenger_raw is None:
        messenger: dict[str, Any] = {}
    elif isinstance(messenger_raw, Mapping):
        messenger = dict(messenger_raw)
    else:
        raise ValueError("messenger section must be a mapping when configured.")

    if "telegram" not in messenger and "telegram" in root:
        warnings.warn(
            "Top-level 'telegram' config is deprecated; move it to 'messenger.telegram'.",
            FutureWarning,
            stacklevel=2,
        )
        legacy_telegram = root.get("telegram")
        # Historical loaders treated a non-mapping legacy value as disabled.
        # Preserve that read behavior during the migration window, while the
        # new canonical nested section remains strict.
        if legacy_telegram is None or isinstance(legacy_telegram, Mapping):
            messenger["telegram"] = legacy_telegram
    return messenger


def config_with_canonical_messenger(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a shallow config copy whose shared messenger block is canonical."""
    resolved = dict(raw)
    if "messenger" in raw or "telegram" in raw:
        resolved["messenger"] = messenger_mapping_from_root(raw)
    return resolved


def resolve_configured_path(value: Any) -> Path | None:
    text = normalize_text(value)
    return Path(text).expanduser().resolve() if text else None


def engine_config_mapping(
    raw: dict[str, Any],
    engine: str,
    *,
    inherit_keys: Iterable[str] = ("behavior", "resources", "messenger"),
) -> dict[str, Any]:
    section = raw.get(engine)
    if not isinstance(section, dict):
        return {}

    resolved = dict(section)
    for key in inherit_keys:
        inherited = raw.get(key)
        override = resolved.get(key)
        if (
            key in {"resources", "scheduler", "workflow"}
            and key in resolved
            and not isinstance(override, dict)
        ):
            raise ValueError(f"{engine}.{key} must be a mapping when configured.")
        if key == "scheduler" and isinstance(override, dict):
            mismatched = sorted(
                override_key
                for override_key, override_value in override.items()
                if not isinstance(inherited, dict)
                or override_key not in inherited
                or inherited[override_key] != override_value
            )
            if mismatched:
                joined = ", ".join(mismatched)
                raise ValueError(
                    f"{engine}.scheduler cannot override the shared top-level scheduler "
                    f"({joined}); configure machine-wide admission under scheduler instead."
                )
        if not isinstance(inherited, dict):
            continue
        if key not in resolved:
            resolved[key] = dict(inherited)
        elif key == "scheduler" and isinstance(override, dict):
            # Preserve the shared limit when an engine redundantly repeats only
            # one scheduler key. Divergent values were rejected above.
            resolved[key] = {**inherited, **override}
    return resolved


def default_shared_admission_root(runs_root: str | Path | None) -> str:
    """Default shared admission directory: hidden under the single runs root."""
    text = normalize_text(runs_root)
    if not text:
        return ""
    return str(Path(text).expanduser().resolve() / DEFAULT_SHARED_ADMISSION_DIRNAME)


def scheduler_admission_root(
    scheduler: dict[str, Any] | None,
    *,
    default_runs_root: str | Path | None = None,
) -> Path | None:
    scheduler_raw = scheduler if isinstance(scheduler, dict) else {}
    if "admission_root" in scheduler_raw:
        raw_text = normalize_text(scheduler_raw.get("admission_root"))
        validated = validated_absolute_linux_path_text(
            raw_text,
            field_name="scheduler.admission_root",
        )
        return Path(validated).expanduser().resolve()
    return resolve_configured_path(default_shared_admission_root(default_runs_root))


def runs_root_from_mapping(raw: dict[str, Any] | None) -> str:
    """Read the shared runs root from the top-level runs_root key.

    Returns the configured text as-is (no resolution) so callers can validate
    the raw value before resolving it.
    """
    return normalize_text((raw.get("runs_root") or "") if isinstance(raw, dict) else "")


def validated_absolute_linux_path_text(path_text: str, *, field_name: str) -> str:
    """Reject Windows-style and non-absolute paths before resolution."""

    if is_rejected_windows_path(path_text):
        raise ValueError(
            f"{field_name} must be a Linux path (Windows paths are not supported): {path_text!r}"
        )
    if not Path(path_text).is_absolute():
        raise ValueError(f"{field_name} must be an absolute Linux path: {path_text!r}")
    resolved = str(Path(path_text).expanduser().resolve())
    if is_rejected_windows_path(resolved):
        raise ValueError(
            f"{field_name} must resolve to a Linux path outside Windows mounts: {path_text!r}"
        )
    return resolved


def validated_runs_root_text(root_text: str) -> str:
    """Reject Windows-style and non-absolute runs_root values before resolution.

    Resolving first would silently anchor a bad value on the worker cwd, so
    every runs_root consumer must validate the raw text through this helper.
    """

    return validated_absolute_linux_path_text(root_text, field_name="runs_root")


def usable_runs_root_from_mapping(raw: dict[str, Any] | None) -> str:
    """runs_root text when present and valid, else "".

    For soft consumers (discovery, capacity preflight, systemd rendering) that
    must ignore an invalid root rather than raise: an unvalidated resolve would
    silently anchor the value on the caller cwd.
    """
    root_text = runs_root_from_mapping(raw)
    if not root_text:
        return ""
    try:
        return validated_runs_root_text(root_text)
    except ValueError:
        return ""


def shared_workflow_root_from_config(config_path: str | Path | None) -> str | None:
    if config_path is None:
        return None

    try:
        path = Path(config_path).expanduser().resolve()
    except OSError:
        return None
    if not path.exists():
        return None

    try:
        _, parsed = load_yaml_mapping(path)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        return None

    root_text = usable_runs_root_from_mapping(parsed)
    if not root_text:
        return None
    return str(Path(root_text).expanduser().resolve())
