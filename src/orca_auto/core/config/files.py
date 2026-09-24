from __future__ import annotations

import errno
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR as _ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.paths.validation import (
    validated_absolute_linux_path_text as _validated_absolute_linux_path_text,
)
from orca_auto.core.utils.coercion import normalize_text

from .schema import (
    CommonResourceConfig,
    MessengerConfig,
    SchedulerConfig,
    explicit_positive_int,
    messenger_config_from_mapping,
)
from .schema import (
    reject_unknown_config_fields as _reject_unknown_config_fields,
)

DEFAULT_CONFIG_FILENAME = "orca_auto.yaml"
DEFAULT_SHARED_ADMISSION_DIRNAME = ".admission"
SECURE_CONFIG_FILE_MODE = 0o600
# The one engine section. Its contents are the engine's own schema, validated
# by ``orca_auto.orca.config``; this loader only checks that it is a mapping.
ENGINE_CONFIG_SECTION = "orca"
_ROOT_CONFIG_FIELDS = frozenset(
    {"messenger", ENGINE_CONFIG_SECTION, "resources", "runs_root", "scheduler"}
)
_SCHEDULER_CONFIG_FIELDS = frozenset({"admission_root", "max_active_simulations"})
_RESOURCE_CONFIG_FIELDS = frozenset({"max_cores_per_task", "max_memory_gb_per_task"})
YAML_CONFIG_LOAD_EXCEPTIONS = (OSError, ValueError, yaml.YAMLError)


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
        # types-PyYAML leaves BaseConstructor.construct_object untyped.
        key = loader.construct_object(key_node, deep=deep)  # type: ignore[no-untyped-call]
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ValueError("YAML mapping keys must be hashable scalars") from exc
        if duplicate:
            # Do not include the key or source line: config values can contain secrets.
            raise ValueError("YAML contains a duplicate mapping key")
        mapping[key] = loader.construct_object(  # type: ignore[no-untyped-call]
            value_node,
            deep=deep,
        )
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def config_env_value(env_var: str = _ORCA_AUTO_CONFIG_ENV_VAR) -> str:
    return os.getenv(env_var, "").strip()


def secure_config_file_permissions(
    config_path: str | Path,
    *,
    mode: int = SECURE_CONFIG_FILE_MODE,
) -> None:
    Path(config_path).chmod(mode)


def home_default_config_path() -> Path:
    """The one implicit config location: ``~/orca_auto/config/orca_auto.yaml``.

    A source checkout is not probed. In a wheel or prepared-runtime install the
    package ancestry points into the virtual environment, and production runs
    prepared runtimes, so a checkout-relative location would silently differ
    between layouts.
    """

    return Path.home() / "orca_auto" / "config" / DEFAULT_CONFIG_FILENAME


def default_config_path(*, env_var: str = _ORCA_AUTO_CONFIG_ENV_VAR) -> str:
    """Where a new config is written when no explicit path is given."""

    env_path = config_env_value(env_var)
    if env_path:
        return env_path
    return str(home_default_config_path())


def discover_shared_config_path(
    explicit: str | Path | None,
    *,
    env_var: str = _ORCA_AUTO_CONFIG_ENV_VAR,
) -> str | None:
    """Discovery order: explicit path, ``ORCA_AUTO_CONFIG``, then the home default.

    An explicit or environment path is returned even when the file is missing
    so the caller reports that path; the home default is used only when it
    exists.
    """

    explicit_text = str(explicit or "").strip()
    if explicit_text:
        return str(Path(explicit_text).expanduser().resolve())

    env_path = config_env_value(env_var)
    if env_path:
        return str(Path(env_path).expanduser().resolve())

    home_default = home_default_config_path().expanduser().resolve()
    return str(home_default) if home_default.exists() else None


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


def mapping_section(raw: dict[str, Any] | None, key: str) -> dict[str, Any]:
    section = raw.get(key) if isinstance(raw, dict) else None
    return section if isinstance(section, dict) else {}


def configured_mapping_section(
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


def validate_optional_text_field(
    raw: Mapping[str, Any],
    key: str,
    *,
    field_name: str,
) -> None:
    if key in raw and not isinstance(raw.get(key), str):
        raise ValueError(f"{field_name} must be a string when configured.")


@dataclass(frozen=True)
class SharedConfig:
    """Every validated top-level section of one ``orca_auto.yaml``; defaults applied.

    ``runs_root`` is the configured text ("" when omitted). Its path rules are
    applied by the consumers that require it, because soft consumers (systemd
    rendering, discovery) must tolerate a missing or invalid root instead of
    failing the whole file.

    ``engine_section`` is the raw ``orca`` mapping ({} when omitted). Only its
    mapping shape is checked here; ``orca_auto.orca.config`` validates its
    fields.
    """

    runs_root: str = ""
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    resources: CommonResourceConfig = field(default_factory=CommonResourceConfig)
    messenger: MessengerConfig = field(default_factory=MessengerConfig)
    engine_section: dict[str, Any] = field(default_factory=dict)


def _scheduler_config_from_mapping(scheduler: Mapping[str, Any]) -> SchedulerConfig:
    _reject_unknown_config_fields(
        scheduler,
        allowed=_SCHEDULER_CONFIG_FIELDS,
        section="scheduler",
    )
    max_active = SchedulerConfig.max_active_simulations
    if "max_active_simulations" in scheduler:
        max_active = explicit_positive_int(
            scheduler.get("max_active_simulations"),
            field_name="scheduler.max_active_simulations",
        )
    admission_root = ""
    if "admission_root" in scheduler:
        admission_root = _validated_absolute_linux_path_text(
            normalize_text(scheduler.get("admission_root")),
            field_name="scheduler.admission_root",
        )
    return SchedulerConfig(
        max_active_simulations=max_active,
        admission_root=admission_root,
        configured=bool(scheduler),
    )


def _resource_config_from_mapping(resources: Mapping[str, Any]) -> CommonResourceConfig:
    _reject_unknown_config_fields(
        resources,
        allowed=_RESOURCE_CONFIG_FIELDS,
        section="resources",
    )

    def configured(key: str, default: int) -> int:
        if key not in resources:
            return default
        return explicit_positive_int(resources.get(key), field_name=f"resources.{key}")

    return CommonResourceConfig(
        max_cores_per_task=configured(
            "max_cores_per_task", CommonResourceConfig.max_cores_per_task
        ),
        max_memory_gb_per_task=configured(
            "max_memory_gb_per_task", CommonResourceConfig.max_memory_gb_per_task
        ),
    )


def validate_shared_config_sections(raw: Mapping[str, Any]) -> SharedConfig:
    """Validate the top-level shared-config shape in one pass.

    Returns the validated sections so callers never re-parse a section they
    already validated. The ``resources``, ``scheduler`` and ``messenger``
    sections are top-level only; the ``orca`` engine section is returned raw
    for ``orca_auto.orca.config`` to validate.
    """

    messenger_raw = messenger_mapping_from_root(raw)
    _reject_unknown_config_fields(
        raw,
        allowed=_ROOT_CONFIG_FIELDS,
        section="top-level",
    )
    validate_optional_text_field(raw, "runs_root", field_name="runs_root")

    scheduler = _scheduler_config_from_mapping(configured_mapping_section(raw, "scheduler"))
    resources = _resource_config_from_mapping(configured_mapping_section(raw, "resources"))
    engine_section = configured_mapping_section(raw, ENGINE_CONFIG_SECTION)

    return SharedConfig(
        runs_root=normalize_text(raw.get("runs_root")),
        scheduler=scheduler,
        resources=resources,
        messenger=messenger_config_from_mapping(messenger_raw),
        engine_section=engine_section,
    )


def load_shared_config_mapping(
    config_path: str | Path,
    *,
    invalid_message: str = "YAML top-level is not a mapping: {path}",
) -> tuple[Path, dict[str, Any]]:
    """Load and validate one shared ``orca_auto.yaml``, returning the raw mapping.

    For callers that need the file's own text back (for example to preserve a
    section verbatim). Consumers of settings use ``load_shared_config``. Only
    the top-level sections are validated here; ``orca_auto.orca.config`` adds
    the engine section.
    """

    path, raw = load_yaml_mapping(config_path, invalid_message=invalid_message)
    validate_shared_config_sections(raw)
    return path, raw


def load_shared_config(
    config_path: str | Path,
    *,
    missing_error: Callable[[Path], Exception] | None = None,
    invalid_message: str = "YAML top-level is not a mapping: {path}",
) -> tuple[Path, SharedConfig]:
    """Require, load, and validate the top-level sections of one ``orca_auto.yaml``."""

    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        if missing_error is not None:
            raise missing_error(path)
        # Same shape as the OSError ``open()`` raised before existence was
        # checked up front, so callers keep naming "No such file".
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
    path, raw = load_yaml_mapping(path, invalid_message=invalid_message)
    return path, validate_shared_config_sections(raw)


def messenger_mapping_from_root(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the configured ``messenger`` mapping."""
    root = raw if isinstance(raw, Mapping) else {}
    if "messenger" not in root:
        return {}
    messenger_raw = root.get("messenger")
    if isinstance(messenger_raw, Mapping):
        return dict(messenger_raw)
    raise ValueError("messenger section must be a mapping when configured.")


def resolve_configured_path(value: Any) -> Path | None:
    text = normalize_text(value)
    return Path(text).expanduser().resolve() if text else None


def default_shared_admission_root(runs_root: str | Path | None) -> str:
    """Default shared admission directory: hidden under the single runs root."""
    text = normalize_text(runs_root)
    if not text:
        return ""
    return str(Path(text).expanduser().resolve() / DEFAULT_SHARED_ADMISSION_DIRNAME)


def resolved_admission_root(
    scheduler: SchedulerConfig,
    *,
    runs_root: str | Path | None = None,
) -> Path | None:
    """Explicit ``scheduler.admission_root`` or ``<runs_root>/.admission``; None without either."""

    if scheduler.admission_root:
        return Path(scheduler.admission_root).expanduser().resolve()
    return resolve_configured_path(default_shared_admission_root(runs_root))


def validated_runs_root_text(root_text: str) -> str:
    """Reject Windows-style and non-absolute runs_root values before resolution.

    Resolving first would silently anchor a bad value on the worker cwd, so
    every runs_root consumer must validate the raw text through this helper.
    """

    return _validated_absolute_linux_path_text(root_text, field_name="runs_root")


def usable_runs_root_text(root_text: str) -> str:
    """runs_root text when present and valid, else "".

    For soft consumers (discovery, capacity preflight, systemd rendering) that
    must ignore an invalid root rather than raise: an unvalidated resolve would
    silently anchor the value on the caller cwd.
    """
    if not root_text:
        return ""
    try:
        return validated_runs_root_text(root_text)
    except ValueError:
        return ""


def shared_runs_root_from_config(config_path: str | Path | None) -> str | None:
    if config_path is None:
        return None

    try:
        path = Path(config_path).expanduser().resolve()
    except OSError:
        return None
    if not path.exists():
        return None

    try:
        _, shared = load_shared_config(path)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        return None

    root_text = usable_runs_root_text(shared.runs_root)
    if not root_text:
        return None
    return str(Path(root_text).expanduser().resolve())
