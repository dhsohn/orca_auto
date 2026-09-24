"""Interactive ``orca_auto init``: prompts build a mapping, the shared validator judges it."""

from __future__ import annotations

import getpass
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

import yaml

from orca_auto.core.config.discovery import (
    default_shared_config_path as default_config_path,
)
from orca_auto.core.config.files import (
    YAML_CONFIG_LOAD_EXCEPTIONS,
    load_shared_config_mapping,
    messenger_mapping_from_root,
    secure_config_file_permissions,
    validate_shared_config_sections,
)
from orca_auto.core.config.schema import (
    CommonResourceConfig,
    SchedulerConfig,
    discord_config_from_mapping,
    explicit_positive_int,
)
from orca_auto.core.paths import validate_configured_executable_path
from orca_auto.core.paths.validation import validated_absolute_linux_path_text
from orca_auto.core.utils.persistence import atomic_write_text

logger = logging.getLogger(__name__)


class _PromptedEngineRuntime(TypedDict):
    runs_root: str
    executable: str


@dataclass(frozen=True)
class _PromptedInitValues:
    orca_runtime: _PromptedEngineRuntime
    max_active_simulations: int
    messenger: dict[str, object]


def _stdin_supports_interactive_prompts() -> bool:
    stdin = getattr(sys, "stdin", None)
    isatty = getattr(stdin, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except OSError:
        return False


def _prompt_text(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None and default != "" else ""
    value = input(f"{label}{suffix}: ").strip()
    if value:
        return value
    return default or ""


def _prompt_secret_text(label: str) -> str:
    return getpass.getpass(f"{label}: ").strip()


def _prompt_yes_no(label: str, *, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        value = input(f"{label} [{hint}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer y or n.")


def _normalize_linux_path(raw: str, *, label: str) -> Path | None:
    # Same rule and wording as the config loader, so an accepted path cannot
    # be rejected at startup and the rejected text is never echoed.
    if not raw.strip():
        print(f"{label} is required.")
        return None
    try:
        validated = validated_absolute_linux_path_text(raw.strip(), field_name=label)
    except ValueError as exc:
        print(str(exc))
        return None
    return Path(validated)


def _prompt_executable_path(prompt_label: str, label: str, display_name: str) -> str:
    while True:
        try:
            executable = validate_configured_executable_path(
                _prompt_text(prompt_label),
                label=label,
                display_name=display_name,
            )
        except ValueError as exc:
            print(str(exc))
            continue
        return str(executable)


def _prompt_orca_executable() -> str:
    return _prompt_executable_path("ORCA executable path", "orca_executable", "ORCA")


def _prompt_directory_path(label: str, *, default: str | None = None) -> Path:
    while True:
        raw = _prompt_text(label, default)
        path = _normalize_linux_path(raw, label=label)
        if path is None:
            continue
        if path.exists() and not path.is_dir():
            print(f"{label} is not a directory: {path}")
            continue
        return path


def _ensure_directory(path: Path, *, label: str) -> bool:
    if path.exists():
        return True
    if not _prompt_yes_no(f"{label} does not exist. Create it now?", default=True):
        print(f"{label} was not created.")
        return False
    path.mkdir(parents=True, exist_ok=True)
    return True


def _prompt_positive_int(label: str, *, field_name: str, default: int) -> int:
    while True:
        raw = _prompt_text(label, str(default))
        try:
            return explicit_positive_int(raw, field_name=field_name)
        except ValueError as exc:
            print(str(exc))


def _prompt_max_active_simulations() -> int:
    return _prompt_positive_int(
        "max_active_simulations",
        field_name="scheduler.max_active_simulations",
        default=SchedulerConfig.max_active_simulations,
    )


def _prompt_discord_config() -> dict[str, object]:
    empty: dict[str, object] = {
        "bot_token": "",
        "default_channel_id": "",
    }
    if not _prompt_yes_no("Configure Discord notifications now?", default=False):
        return empty

    while True:
        raw: dict[str, object] = {
            "bot_token": _prompt_secret_text("Discord bot token"),
            "default_channel_id": _prompt_text("Discord default notification channel id"),
        }
        try:
            config = discord_config_from_mapping(raw)
        except ValueError as exc:
            print(f"Invalid Discord notification configuration: {exc}")
            continue
        if config.bot_notification_enabled:
            return raw
        print("A Discord bot token and a default notification channel id are required.")


def _prompt_messenger_config() -> dict[str, object]:
    return {"provider": "discord", "discord": _prompt_discord_config()}


def _prompt_runs_root() -> str:
    """Standalone ORCA jobs and .admission share one runs root."""
    prompt_label = "runs root directory (ORCA jobs)"
    runs_root = _prompt_directory_path(prompt_label)
    while not _ensure_directory(runs_root, label="runs_root"):
        runs_root = _prompt_directory_path(prompt_label)
    return str(runs_root)


def _prompt_orca_runtime() -> _PromptedEngineRuntime:
    return {
        "runs_root": _prompt_runs_root(),
        "executable": str(_prompt_orca_executable()),
    }


def _validate_generated_config(payload: Mapping[str, object]) -> None:
    """Apply the one shared rule set to the mapping before it is written."""

    validate_shared_config_sections(payload)


def _write_config(config_path: Path, payload: Mapping[str, object]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    atomic_write_text(config_path, f"# Generated by orca_auto init\n{rendered}")
    secure_config_file_permissions(config_path)


def _resolve_init_config_path(args: Any) -> Path:
    raw_config_path = str(getattr(args, "config", "") or "").strip() or default_config_path()
    return Path(raw_config_path).expanduser().resolve()


def _confirm_existing_config_overwrite(config_path: Path) -> int | None:
    if not _stdin_supports_interactive_prompts():
        print(
            f"Config already exists at {config_path}. "
            "Re-run with --force to overwrite it without confirmation."
        )
        return 1
    try:
        overwrite = _prompt_yes_no(
            f"Config already exists at {config_path}. Overwrite it?",
            default=False,
        )
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1
    if not overwrite:
        print("Cancelled.")
        return 0
    return None


def _prompt_init_values(
    *, existing_messenger: Mapping[str, object] | None = None
) -> _PromptedInitValues:
    orca_runtime = _prompt_orca_runtime()
    max_active_simulations = _prompt_max_active_simulations()
    if existing_messenger is not None and _prompt_yes_no(
        "Keep the existing messenger settings?",
        default=True,
    ):
        messenger = dict(existing_messenger)
        print(f"Preserving existing messenger settings ({messenger.get('provider', 'discord')}).")
    else:
        messenger = _prompt_messenger_config()
    return _PromptedInitValues(
        orca_runtime=orca_runtime,
        max_active_simulations=max_active_simulations,
        messenger=messenger,
    )


def _init_config_payload(values: _PromptedInitValues) -> dict[str, object]:
    # scheduler.admission_root is intentionally omitted: the shared admission
    # directory defaults to <runs_root>/.admission. Resource defaults are the
    # schema's own so the wizard cannot drift from the loader.
    resources = CommonResourceConfig()
    return {
        "runs_root": str(values.orca_runtime["runs_root"]),
        "resources": {
            "max_cores_per_task": resources.max_cores_per_task,
            "max_memory_gb_per_task": resources.max_memory_gb_per_task,
        },
        "scheduler": {
            "max_active_simulations": values.max_active_simulations,
        },
        "messenger": values.messenger,
        "orca": {
            "runtime": {},
            "paths": {
                "orca_executable": str(values.orca_runtime["executable"]),
            },
        },
    }


def _print_init_summary(config_path: Path, values: _PromptedInitValues) -> None:
    print("Config created successfully.")
    print(f"  config: {config_path}")
    print(f"  runs_root: {values.orca_runtime['runs_root']}")
    print(f"  max_active_simulations: {values.max_active_simulations}")
    print(f"  messenger_provider: {values.messenger.get('provider', 'discord')}")


def _load_existing_messenger_mapping(config_path: Path) -> dict[str, object] | None:
    try:
        _, parsed = load_shared_config_mapping(config_path)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        print("warning: existing messenger settings could not be read and will be configured again")
        return None
    try:
        messenger = messenger_mapping_from_root(parsed)
    except ValueError as exc:
        print(f"warning: {exc}; messenger settings will be configured again")
        return None
    if not messenger:
        return None

    preserved: dict[str, object] = dict(messenger)
    provider = str(preserved.get("provider", "") or "").strip().lower()
    if not provider:
        provider = "discord"
    preserved["provider"] = provider
    return preserved


def cmd_init(args: Any) -> int:
    force = bool(getattr(args, "force", False))
    config_path = _resolve_init_config_path(args)

    if config_path.exists() and not force:
        overwrite_status = _confirm_existing_config_overwrite(config_path)
        if overwrite_status is not None:
            return overwrite_status

    existing_messenger = (
        _load_existing_messenger_mapping(config_path) if config_path.exists() else None
    )
    print(f"Creating config at: {config_path}")

    try:
        values = _prompt_init_values(existing_messenger=existing_messenger)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1

    payload = _init_config_payload(values)
    try:
        _validate_generated_config(payload)
        _write_config(config_path, payload)
    except Exception as exc:
        logger.exception("Failed to generate config: %s", exc)
        print(f"Failed to generate config: {exc}")
        return 1

    _print_init_summary(config_path, values)
    return 0
