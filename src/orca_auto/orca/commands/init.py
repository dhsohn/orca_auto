from __future__ import annotations

import getpass
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

import yaml

from orca_auto.core.config.files import secure_config_file_permissions
from orca_auto.core.engine_runner import validate_executable_file
from orca_auto.core.paths import is_rejected_windows_path
from orca_auto.core.utils.persistence import atomic_write_text

from ..config import load_config
from ._helpers import default_config_path

logger = logging.getLogger(__name__)


class _PromptedEngineRuntime(TypedDict):
    runs_root: str
    executable: str


class _PromptedOrcaRuntime(_PromptedEngineRuntime):
    default_max_retries: int


@dataclass(frozen=True)
class _PromptedInitValues:
    orca_runtime: _PromptedOrcaRuntime
    xtb_runtime: dict[str, str]
    crest_runtime: dict[str, str]
    max_active_simulations: int
    telegram: dict[str, str]


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
    if not raw.strip():
        print(f"{label} is required.")
        return None
    if is_rejected_windows_path(raw):
        print(f"{label} must be a Linux path, not a Windows path: {raw}")
        return None

    path = Path(raw).expanduser()
    if not path.is_absolute():
        print(f"{label} must be an absolute Linux path.")
        return None
    return path.resolve(strict=False)


def _prompt_executable_path(prompt_label: str, *, label: str) -> str:
    while True:
        raw = _prompt_text(prompt_label)
        path = _normalize_linux_path(raw, label=label)
        if path is None:
            continue
        if str(path).lower().endswith(".exe"):
            print(f"{label} must point to a Linux binary, not a Windows .exe.")
            continue
        try:
            executable = validate_executable_file(
                path,
                missing_message=lambda resolved: f"File not found: {resolved}",
                not_file_message=lambda resolved: f"Path is not a file: {resolved}",
                not_executable_message=lambda resolved: f"Path is not executable: {resolved}",
            )
        except ValueError as exc:
            print(str(exc))
            continue
        return str(executable)


def _prompt_orca_executable() -> str:
    return _prompt_executable_path("ORCA executable path", label="orca_executable")


def _prompt_xtb_executable() -> str:
    return _prompt_executable_path("xTB executable path", label="xtb_executable")


def _prompt_crest_executable() -> str:
    return _prompt_executable_path("CREST executable path", label="crest_executable")


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


def _prompt_int(label: str, *, default: str, minimum: int) -> int:
    while True:
        raw = _prompt_text(label, default)
        try:
            value = int(raw)
        except ValueError:
            print(f"{label} must be an integer >= {minimum}.")
            continue
        if value < minimum:
            print(f"{label} must be an integer >= {minimum}.")
            continue
        return value


def _prompt_default_max_retries() -> int:
    return _prompt_int("default_max_retries", default="2", minimum=0)


def _prompt_max_active_simulations() -> int:
    return _prompt_int("max_active_simulations", default="4", minimum=1)


def _prompt_telegram_config() -> dict[str, str]:
    if not _prompt_yes_no("Configure Telegram notifications now?", default=False):
        return {"bot_token": "", "chat_id": ""}

    while True:
        bot_token = _prompt_secret_text("Telegram bot token")
        chat_id = _prompt_text("Telegram chat id")
        if bot_token and chat_id:
            return {"bot_token": bot_token, "chat_id": chat_id}
        print(
            "Both Telegram bot token and chat id are required, or choose not to configure Telegram."
        )


def _prompt_runs_root() -> str:
    """Single runs root: ORCA jobs, workflow workspaces, and .admission live here."""
    prompt_label = "runs root directory (ORCA jobs + workflows)"
    runs_root = _prompt_directory_path(prompt_label)
    while not _ensure_directory(runs_root, label="runs_root"):
        runs_root = _prompt_directory_path(prompt_label)
    return str(runs_root)


def _prompt_orca_runtime() -> _PromptedOrcaRuntime:
    return {
        "runs_root": _prompt_runs_root(),
        "executable": str(_prompt_orca_executable()),
        "default_max_retries": _prompt_default_max_retries(),
    }


def _prompt_xtb_runtime() -> dict[str, str]:
    return {"executable": _prompt_xtb_executable()}


def _prompt_crest_runtime() -> dict[str, str]:
    return {"executable": _prompt_crest_executable()}


def _validate_generated_config(config_path: str) -> None:
    from orca_auto.core.config.engines import load_crest_config, load_xtb_config

    load_config(config_path)
    load_xtb_config(config_path)
    load_crest_config(config_path)


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


def _prompt_init_values() -> _PromptedInitValues:
    return _PromptedInitValues(
        orca_runtime=_prompt_orca_runtime(),
        xtb_runtime=_prompt_xtb_runtime(),
        crest_runtime=_prompt_crest_runtime(),
        max_active_simulations=_prompt_max_active_simulations(),
        telegram=_prompt_telegram_config(),
    )


def _init_config_payload(values: _PromptedInitValues) -> dict[str, object]:
    # scheduler.admission_root is intentionally omitted: the shared admission
    # directory defaults to <runs_root>/.admission.
    return {
        "runs_root": str(values.orca_runtime["runs_root"]),
        "resources": {
            "max_cores_per_task": 8,
            "max_memory_gb_per_task": 32,
        },
        "scheduler": {
            "max_active_simulations": values.max_active_simulations,
        },
        "workflow": {
            "paths": {
                "xtb_executable": str(values.xtb_runtime["executable"]),
                "crest_executable": str(values.crest_runtime["executable"]),
            },
        },
        "telegram": values.telegram,
        "orca": {
            "runtime": {
                "default_max_retries": values.orca_runtime["default_max_retries"],
            },
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
    print(f"  xtb_executable: {values.xtb_runtime['executable']}")
    print(f"  crest_executable: {values.crest_runtime['executable']}")


def cmd_init(args: Any) -> int:
    force = bool(getattr(args, "force", False))
    config_path = _resolve_init_config_path(args)

    if config_path.exists() and not force:
        overwrite_status = _confirm_existing_config_overwrite(config_path)
        if overwrite_status is not None:
            return overwrite_status

    print(f"Creating config at: {config_path}")

    try:
        values = _prompt_init_values()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1

    try:
        _write_config(config_path, _init_config_payload(values))
        _validate_generated_config(str(config_path))
    except Exception as exc:
        logger.exception("Failed to generate config: %s", exc)
        print(f"Failed to generate config: {exc}")
        return 1

    _print_init_summary(config_path, values)
    return 0
