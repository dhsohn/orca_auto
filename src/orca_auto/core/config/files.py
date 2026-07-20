from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent, NodeEvent

from orca_auto.core.paths import is_rejected_windows_path
from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.core.utils.coercion import normalize_text

from .schema import explicit_nonnegative_int, explicit_positive_int, messenger_config_from_mapping

ORCA_AUTO_CONFIG_ENV_VAR = "ORCA_AUTO_CONFIG"
DEFAULT_CONFIG_FILENAME = "orca_auto.yaml"
DEFAULT_SHARED_ADMISSION_DIRNAME = ".admission"
SECURE_CONFIG_FILE_MODE = 0o600
YAML_CONFIG_LOAD_EXCEPTIONS = (OSError, ValueError, yaml.YAMLError)
MAX_JOB_MANIFEST_BYTES = 1024 * 1024
MAX_JOB_MANIFEST_ALIASES = 32
MAX_JOB_MANIFEST_NODES = 10_000
MAX_JOB_MANIFEST_DEPTH = 64
_ROOT_CONFIG_FIELDS = frozenset(
    {"messenger", "orca", "resources", "runs_root", "scheduler", "workflow"}
)
_SCHEDULER_CONFIG_FIELDS = frozenset(
    {"admission_root", "max_active_simulations", "max_active_xtb_md"}
)
_RESOURCE_CONFIG_FIELDS = frozenset({"max_cores_per_task", "max_memory_gb_per_task"})
_WORKFLOW_CONFIG_FIELDS = frozenset({"paths"})
_WORKFLOW_PATH_CONFIG_FIELDS = frozenset({"crest_executable", "xtb_executable"})
_ORCA_CONFIG_FIELDS = frozenset({"paths", "runtime"})
_ORCA_RUNTIME_CONFIG_FIELDS = frozenset(
    {"default_max_retries", "scratch_min_free_gb", "scratch_root"}
)
_ORCA_PATH_CONFIG_FIELDS = frozenset({"orca_executable"})


class UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeySafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ValueError("YAML mapping keys must be hashable scalars") from exc
        if duplicate:
            # Do not include the key or source line: config values can contain secrets.
            raise ValueError("YAML contains a duplicate mapping key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


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
    parsed = yaml.load(text, Loader=UniqueKeySafeLoader)
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
            payload = handle.read()
        if not payload.strip():
            parsed = {}
        else:
            parsed = yaml.load(payload, Loader=UniqueKeySafeLoader)
        if parsed is None:
            node = yaml.compose(payload, Loader=UniqueKeySafeLoader)
            empty_document = node is None
        else:
            empty_document = False
        if empty_document:
            # No YAML node (empty, whitespace, or comments-only) means that all
            # settings are omitted. Every explicit null node remains malformed,
            # including an otherwise-empty explicit document marker.
            parsed = {}
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


def _reject_unknown_config_fields(
    raw: Mapping[Any, Any],
    *,
    allowed: frozenset[str],
    section: str,
) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(f"Unknown {section} config fields: {', '.join(unknown)}.")


def _validate_optional_text_field(
    raw: Mapping[str, Any],
    key: str,
    *,
    field_name: str,
) -> None:
    if key in raw and not isinstance(raw.get(key), str):
        raise ValueError(f"{field_name} must be a string when configured.")


def validate_shared_config_sections(raw: Mapping[str, Any]) -> None:
    """Validate the complete public shared-config shape before any defaults apply."""

    # Preserve the pointed migration error for the removed top-level Telegram block.
    messenger_raw = messenger_mapping_from_root(raw)
    _reject_unknown_config_fields(
        raw,
        allowed=_ROOT_CONFIG_FIELDS,
        section="top-level",
    )
    _validate_optional_text_field(raw, "runs_root", field_name="runs_root")

    scheduler = _configured_mapping_section(raw, "scheduler")
    _reject_unknown_config_fields(
        scheduler,
        allowed=_SCHEDULER_CONFIG_FIELDS,
        section="scheduler",
    )
    for key in ("max_active_simulations", "max_active_xtb_md"):
        if key in scheduler:
            explicit_positive_int(
                scheduler.get(key),
                field_name=f"scheduler.{key}",
            )
    resources = _configured_mapping_section(raw, "resources")
    _reject_unknown_config_fields(
        resources,
        allowed=_RESOURCE_CONFIG_FIELDS,
        section="resources",
    )
    workflow = _configured_mapping_section(raw, "workflow")
    _reject_unknown_config_fields(
        workflow,
        allowed=_WORKFLOW_CONFIG_FIELDS,
        section="workflow",
    )
    workflow_paths = _configured_mapping_section(
        workflow,
        "paths",
        field_name="workflow.paths",
    )
    _reject_unknown_config_fields(
        workflow_paths,
        allowed=_WORKFLOW_PATH_CONFIG_FIELDS,
        section="workflow.paths",
    )
    for key in _WORKFLOW_PATH_CONFIG_FIELDS:
        _validate_optional_text_field(
            workflow_paths,
            key,
            field_name=f"workflow.paths.{key}",
        )

    orca = _configured_mapping_section(raw, "orca")
    _reject_unknown_config_fields(orca, allowed=_ORCA_CONFIG_FIELDS, section="orca")
    orca_runtime = _configured_mapping_section(orca, "runtime", field_name="orca.runtime")
    _reject_unknown_config_fields(
        orca_runtime,
        allowed=_ORCA_RUNTIME_CONFIG_FIELDS,
        section="orca.runtime",
    )
    if "default_max_retries" in orca_runtime:
        explicit_nonnegative_int(
            orca_runtime.get("default_max_retries"),
            field_name="orca.runtime.default_max_retries",
        )
    if "scratch_root" in orca_runtime and (
        not isinstance(orca_runtime.get("scratch_root"), str)
        or not str(orca_runtime.get("scratch_root")).strip()
    ):
        raise ValueError("orca.runtime.scratch_root must be a non-empty string when configured")
    orca_paths = _configured_mapping_section(orca, "paths", field_name="orca.paths")
    _reject_unknown_config_fields(
        orca_paths,
        allowed=_ORCA_PATH_CONFIG_FIELDS,
        section="orca.paths",
    )
    _validate_optional_text_field(
        orca_paths,
        "orca_executable",
        field_name="orca.paths.orca_executable",
    )

    messenger_config_from_mapping(messenger_raw)


def load_shared_config_mapping(
    config_path: str | Path,
    *,
    invalid_message: str = "YAML top-level is not a mapping: {path}",
) -> tuple[Path, dict[str, Any]]:
    """Load and validate one complete shared ``orca_auto.yaml`` mapping."""

    path, raw = load_yaml_mapping(config_path, invalid_message=invalid_message)
    validate_shared_config_sections(raw)
    return path, raw


def load_required_shared_config_mapping(
    config_path: str | Path,
    *,
    missing_error: Callable[[Path], Exception] | None = None,
    invalid_message: str = "YAML top-level is not a mapping: {path}",
) -> tuple[Path, dict[str, Any]]:
    """Require, load, and validate one complete shared ``orca_auto.yaml`` mapping."""

    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        if missing_error is not None:
            raise missing_error(path)
        raise FileNotFoundError(path)
    return load_shared_config_mapping(path, invalid_message=invalid_message)


def messenger_mapping_from_root(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the canonical ``messenger`` mapping.

    ``messenger.telegram`` is the only supported location. A leftover top-level
    ``telegram`` block fails closed with a migration hint instead of being
    silently ignored, which would silently disable notifications.
    """
    root = raw if isinstance(raw, Mapping) else {}
    if "telegram" in root:
        raise ValueError(
            "Top-level 'telegram' config is no longer supported; "
            "move the block to 'messenger.telegram'."
        )
    if "messenger" not in root:
        return {}
    messenger_raw = root.get("messenger")
    if isinstance(messenger_raw, Mapping):
        return dict(messenger_raw)
    raise ValueError("messenger section must be a mapping when configured.")


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
    if engine not in raw:
        return {}
    section = raw.get(engine)
    if not isinstance(section, dict):
        raise ValueError(f"{engine} section must be a mapping when configured.")

    resolved = dict(section)
    for key in inherit_keys:
        inherited = raw.get(key)
        if key in resolved:
            raise ValueError(
                f"{engine}.{key} is not supported; configure shared {key} at the top level."
            )
        if not isinstance(inherited, dict):
            continue
        resolved[key] = dict(inherited)
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
        _, parsed = load_shared_config_mapping(path)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        return None

    root_text = usable_runs_root_from_mapping(parsed)
    if not root_text:
        return None
    return str(Path(root_text).expanduser().resolve())
