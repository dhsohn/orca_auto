from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.cli_common import _repo_root
from orca_auto.core.config import MessengerConfig, messenger_config_from_mapping
from orca_auto.core.config.files import (
    YAML_CONFIG_LOAD_EXCEPTIONS,
    engine_config_mapping,
    load_yaml_mapping,
    mapping_section,
    messenger_mapping_from_root,
    scheduler_admission_root,
    usable_runs_root_from_mapping,
)
from orca_auto.core.utils.coercion import normalize_text

SYSTEMD_UNIT_NAMES = (
    "orca_auto-queue-worker@.service",
    "orca_auto-workflow-worker@.service",
    "orca_auto-bot@.service",
    "orca_auto-runtime@.target",
)

DEFAULT_SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")


@dataclass(frozen=True)
class RenderedUnit:
    name: str
    destination: Path
    content: str


@dataclass(frozen=True)
class SystemdInstallPlan:
    target_user: str
    repo: Path
    config: Path
    unit_dir: Path
    units: tuple[RenderedUnit, ...]
    commands: tuple[tuple[str, ...], ...]
    enabled_unit: str | None
    use_sudo: bool
    warnings: tuple[str, ...]
    requires_inactive_preflight: bool = False
    live_transition: bool = False


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    return int(geteuid()) == 0


@dataclass(frozen=True)
class SystemdInstallOptions:
    target_user: str
    repo: Path
    config: Path
    unit_dir: Path
    worker_only: bool = False
    no_enable: bool = False
    no_start: bool = False
    no_sudo: bool = False
    repo_root: Path | None = None
    is_root: Callable[[], bool] = _is_root


def _existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _needs_sudo(unit_dir: Path, *, is_root: Callable[[], bool] = _is_root) -> bool:
    if is_root():
        return False
    writable_target = unit_dir if unit_dir.exists() else _existing_parent(unit_dir)
    return not os.access(writable_target, os.W_OK)


def _template_dir(repo_root: Path) -> Path:
    return repo_root / "systemd"


def _read_unit_template(template_root: Path, name: str) -> str:
    return (template_root / name).read_text(encoding="utf-8")


_SYSTEMD_READ_WRITE_PLACEHOLDER = "# ORCA_AUTO_READ_WRITE_PATHS"
_TARGET_USER_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$")


def _validate_target_user(target_user: str) -> str:
    if not _TARGET_USER_PATTERN.fullmatch(target_user):
        raise ValueError("--user must be a Linux account name matching ^[a-z_][a-z0-9_-]*[$]?$")
    return target_user


def _systemd_path_text(path: Path, *, label: str) -> str:
    text = str(path)
    if not text.startswith("/"):
        raise ValueError(f"{label} must be absolute for systemd unit rendering: {text}")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in text
    ):
        raise ValueError(
            f"{label} must not contain whitespace or control characters for "
            f"systemd unit rendering: {text}"
        )
    return text


def _append_absolute_path(paths: list[Path], value: Any) -> None:
    text = normalize_text(value)
    if not text:
        return
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        return
    paths.append(candidate.resolve(strict=False))


def _dedupe_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        text = str(path)
        if text in seen:
            continue
        seen.add(text)
        deduped.append(path)
    return tuple(deduped)


def _configured_read_write_paths(config: Path) -> tuple[Path, ...]:
    if not config.exists():
        return ()
    try:
        config_path, raw = load_yaml_mapping(config)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        return ()

    paths: list[Path] = []
    scheduler_raw = mapping_section(raw, "scheduler")
    runs_root = usable_runs_root_from_mapping(raw)
    admission_root = scheduler_admission_root(
        scheduler_raw,
        default_runs_root=runs_root or None,
    )
    if admission_root is not None:
        paths.append(admission_root)

    # Resolve the ORCA view as well so a split-brain engine-scoped scheduler is
    # rejected during installation instead of producing a service that starts
    # against a different admission pool.
    orca_scheduler_raw = mapping_section(
        engine_config_mapping(raw, "orca", inherit_keys=("scheduler",)),
        "scheduler",
    )
    orca_admission_root = scheduler_admission_root(
        orca_scheduler_raw,
        default_runs_root=runs_root or None,
    )
    if orca_admission_root is not None:
        paths.append(orca_admission_root)

    _append_absolute_path(paths, runs_root)

    return _dedupe_paths(paths)


def _render_read_write_paths(config: Path) -> str:
    paths = _configured_read_write_paths(config)
    if not paths:
        return "# ReadWritePaths omitted: config runtime paths unavailable at render time"
    joined = " ".join(_systemd_path_text(path, label="ReadWritePaths path") for path in paths)
    return f"ReadWritePaths={joined}"


def _render_unit_template(template: str, *, repo: Path, config: Path) -> str:
    repo_text = _systemd_path_text(repo, label="--repo")
    config_text = _systemd_path_text(config, label="--config")
    read_write_paths = _render_read_write_paths(config)
    rendered = template.replace("/home/%i/orca_auto", repo_text)
    lines = []
    for line in rendered.splitlines():
        if line.startswith("Environment=ORCA_AUTO_CONFIG="):
            lines.append(f"Environment=ORCA_AUTO_CONFIG={config_text}")
        elif line.strip() == _SYSTEMD_READ_WRITE_PLACEHOLDER:
            lines.append(read_write_paths)
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def _default_config_for_repo(repo: Path) -> Path:
    return repo / "config" / "orca_auto.yaml"


def _normalize_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _enabled_unit_for_args(*, target_user: str, worker_only: bool, no_enable: bool) -> str | None:
    if no_enable:
        return None
    if worker_only:
        return f"orca_auto-queue-worker@{target_user}.service"
    return f"orca_auto-runtime@{target_user}.target"


def _runtime_unit_for_user(target_user: str) -> str:
    return f"orca_auto-runtime@{target_user}.target"


def _worker_unit_for_user(target_user: str) -> str:
    return f"orca_auto-queue-worker@{target_user}.service"


def _workflow_unit_for_user(target_user: str) -> str:
    return f"orca_auto-workflow-worker@{target_user}.service"


def _bot_unit_for_user(target_user: str) -> str:
    return f"orca_auto-bot@{target_user}.service"


def managed_runtime_units_for_user(target_user: str) -> tuple[str, ...]:
    """Return every systemd unit that can retain a loaded orca_auto build."""

    return (
        _runtime_unit_for_user(target_user),
        _worker_unit_for_user(target_user),
        _bot_unit_for_user(target_user),
        _workflow_unit_for_user(target_user),
    )


def _systemctl_transition_commands(
    *,
    target_user: str,
    worker_only: bool,
    enabled_unit: str | None,
) -> tuple[tuple[str, ...], ...]:
    commands: list[tuple[str, ...]] = [("systemctl", "daemon-reload")]
    if enabled_unit is None:
        return tuple(commands)

    opposite_unit = (
        _runtime_unit_for_user(target_user) if worker_only else _worker_unit_for_user(target_user)
    )
    # Remove the opposite boot mode first so a partial transition cannot leave
    # both modes enabled. If the selected enable then fails, neither mode is
    # enabled and the existing live services remain untouched.
    commands.append(("systemctl", "disable", opposite_unit))
    commands.append(("systemctl", "enable", enabled_unit))
    return tuple(commands)


def _messenger_mapping(config: Path) -> dict[str, Any]:
    _, parsed = load_yaml_mapping(
        config,
        invalid_message=(
            "could not read messenger settings from {path}: top-level YAML is not a mapping"
        ),
    )
    try:
        return messenger_mapping_from_root(parsed)
    except ValueError as exc:
        raise ValueError(f"could not read messenger settings from {config}: {exc}") from exc


def _telegram_mapping(config: Path) -> dict[str, Any]:
    messenger = _messenger_mapping(config)
    telegram_raw = messenger.get("telegram")
    if telegram_raw is None:
        return {}
    if not isinstance(telegram_raw, dict):
        raise ValueError(
            f"could not read Telegram settings from {config}: "
            "messenger.telegram section is not a mapping"
        )
    return telegram_raw


def _selected_messenger_config(config: Path) -> MessengerConfig:
    return messenger_config_from_mapping(_messenger_mapping(config))


def _bot_runtime_warning(config: Path, *, worker_only: bool) -> str | None:
    if worker_only or not config.exists():
        return None
    try:
        messenger = _selected_messenger_config(config)
    except ValueError as exc:
        return str(exc)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        return f"could not read messenger settings from {config}: invalid configuration"
    provider = messenger.normalized_provider
    configured = (
        messenger.telegram.interactive_enabled
        if provider == "telegram"
        else messenger.discord.interactive_enabled
    )
    if not configured:
        if provider == "discord":
            return (
                "Discord interactive bot settings are incomplete; configure "
                "messenger.discord.bot_token, allowed_user_ids, and at least one "
                "channel_ids entry to run the interactive bot"
            )
        if provider == "telegram":
            if messenger.telegram.enabled:
                return (
                    "Telegram interactive polling requires a numeric messenger.telegram.chat_id; "
                    "group chats also require messenger.telegram.allowed_user_ids"
                )
            return (
                "Telegram is not fully configured; set messenger.telegram.bot_token and "
                "messenger.telegram.chat_id to run the interactive bot"
            )
        return (
            f"full runtime target includes the {provider} bot, but its interactive "
            "credentials or channel allowlist are incomplete; use --worker-only until ready"
        )
    return None


def _bot_configured(config: Path) -> bool:
    if not config.exists():
        return False
    try:
        messenger = _selected_messenger_config(config)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        return False
    if messenger.normalized_provider == "telegram":
        return messenger.telegram.interactive_enabled
    return messenger.discord.interactive_enabled


def _auto_worker_only(config: Path, *, worker_only: bool, no_enable: bool) -> bool:
    if worker_only or no_enable:
        return worker_only
    return not _bot_configured(config)


def _validate_worker_config(config: Path) -> None:
    """Run the same config loaders used by supervised engine workers."""

    try:
        from orca_auto.core.config.engines import load_crest_config, load_xtb_config
        from orca_auto.orca.config import load_config

        load_config(str(config))
        load_xtb_config(str(config))
        load_crest_config(str(config))
    except Exception as exc:
        raise ValueError(f"runtime config preflight failed: {exc}") from exc


def _collect_warnings(
    repo: Path,
    config: Path,
    *,
    worker_only: bool,
    auto_selected_worker_only: bool,
    no_enable: bool,
    no_start: bool,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if not repo.exists():
        warnings.append(f"repo path does not exist yet: {repo}")
    elif not repo.is_dir():
        warnings.append(f"repo path is not a directory: {repo}")
    python_path = repo / ".venv" / "bin" / "python"
    if not python_path.is_file() or not os.access(python_path, os.X_OK):
        warnings.append(
            f"service Python is missing or not executable: {python_path}; run `make venv`"
        )
    if not config.exists():
        warnings.append(f"config file does not exist yet: {config}")
    if auto_selected_worker_only and not worker_only:
        bot_warning = _bot_runtime_warning(config, worker_only=False)
        warnings.append(
            bot_warning
            or (
                "The selected messenger is not configured for interactive bot operation; "
                "only the queue worker will be enabled"
            )
        )
    elif not auto_selected_worker_only:
        bot_warning = _bot_runtime_warning(config, worker_only=worker_only)
        if bot_warning:
            warnings.append(bot_warning)
    if no_enable:
        warnings.append(
            "--no-enable requires every supervised unit to be inactive before writing; "
            "it leaves the boot selection unchanged and does not stop or start services"
        )
    elif no_start:
        warnings.append(
            "--no-start requires every supervised unit to be inactive before writing; "
            "it updates the boot selection without starting services"
        )
    return tuple(warnings)


def _build_systemd_install_plan(options: SystemdInstallOptions) -> SystemdInstallPlan:
    template_root = _template_dir(options.repo_root or _repo_root())
    units = tuple(
        RenderedUnit(
            name=name,
            destination=options.unit_dir / name,
            content=_render_unit_template(
                _read_unit_template(template_root, name),
                repo=options.repo,
                config=options.config,
            ),
        )
        for name in SYSTEMD_UNIT_NAMES
    )

    effective_worker_only = _auto_worker_only(
        options.config,
        worker_only=options.worker_only,
        no_enable=options.no_enable,
    )
    enabled_unit = _enabled_unit_for_args(
        target_user=options.target_user,
        worker_only=effective_worker_only,
        no_enable=options.no_enable,
    )
    if enabled_unit is not None:
        _validate_worker_config(options.config)
    commands = _systemctl_transition_commands(
        target_user=options.target_user,
        worker_only=effective_worker_only,
        enabled_unit=enabled_unit,
    )

    return SystemdInstallPlan(
        target_user=options.target_user,
        repo=options.repo,
        config=options.config,
        unit_dir=options.unit_dir,
        units=units,
        commands=commands,
        enabled_unit=enabled_unit,
        use_sudo=False
        if options.no_sudo
        else _needs_sudo(options.unit_dir, is_root=options.is_root),
        warnings=_collect_warnings(
            options.repo,
            options.config,
            worker_only=options.worker_only,
            auto_selected_worker_only=effective_worker_only,
            no_enable=options.no_enable,
            no_start=options.no_start,
        ),
        requires_inactive_preflight=options.no_start or options.no_enable,
        live_transition=enabled_unit is not None and not options.no_start,
    )


def build_systemd_install_plan(
    *,
    target_user: str | None,
    repo: str | Path | None,
    config: str | Path | None = None,
    unit_dir: str | Path = DEFAULT_SYSTEMD_UNIT_DIR,
    worker_only: bool = False,
    no_enable: bool = False,
    no_start: bool = False,
    no_sudo: bool = False,
    repo_root: Path | None = None,
    is_root: Callable[[], bool] = _is_root,
) -> SystemdInstallPlan:
    user_text = normalize_text(target_user)
    if not user_text:
        raise ValueError("--user is required")
    user_text = _validate_target_user(user_text)

    repo_text = normalize_text(repo)
    if not repo_text:
        raise ValueError("--repo is required")

    repo_path = _normalize_path(repo_text)
    options = SystemdInstallOptions(
        target_user=user_text,
        repo=repo_path,
        config=_normalize_path(config or _default_config_for_repo(repo_path)),
        unit_dir=_normalize_path(unit_dir),
        worker_only=worker_only,
        no_enable=no_enable,
        no_start=no_start,
        no_sudo=no_sudo,
        repo_root=repo_root,
        is_root=is_root,
    )
    return _build_systemd_install_plan(options)


def _systemd_command_argv(command: Sequence[str], *, use_sudo: bool) -> tuple[str, ...]:
    parts = (("sudo",) if use_sudo else ()) + tuple(command)
    return parts


def _format_command(command: Sequence[str], *, use_sudo: bool) -> str:
    return " ".join(_systemd_command_argv(command, use_sudo=use_sudo))


def _print_plan(plan: SystemdInstallPlan) -> None:
    print("systemd install plan:")
    print(f"  user: {plan.target_user}")
    print(f"  repo: {plan.repo}")
    print(f"  config: {plan.config}")
    print(f"  unit_dir: {plan.unit_dir}")
    if plan.enabled_unit:
        print(f"  enable: {plan.enabled_unit}")
    else:
        print("  enable: skipped")
    if plan.requires_inactive_preflight:
        print("  pre-write inactive preflight (fail closed):")
        for managed_unit in managed_runtime_units_for_user(plan.target_user):
            preflight_command = ("systemctl", "is-active", managed_unit)
            formatted_preflight_command = _format_command(
                preflight_command,
                use_sudo=plan.use_sudo,
            )
            print(f"    require exit 3/inactive: {formatted_preflight_command}")
    if plan.live_transition:
        managed_units = managed_runtime_units_for_user(plan.target_user)
        workflow_unit = _workflow_unit_for_user(plan.target_user)
        print("  pre-write live drain (fail closed):")
        print(
            "    snapshot: "
            f"{_format_command(('systemctl', 'is-active', workflow_unit), use_sudo=plan.use_sudo)}"
        )
        print(
            "    stop: "
            f"{_format_command(('systemctl', 'stop', *managed_units), use_sudo=plan.use_sudo)}"
        )
        print("    verify inactive: each managed runtime unit")
    print("  write:")
    for unit in plan.units:
        print(f"    {unit.destination}")
    if plan.commands:
        print("  run:")
        for command in plan.commands:
            print(f"    {_format_command(command, use_sudo=plan.use_sudo)}")
    if plan.live_transition:
        workflow_unit = _workflow_unit_for_user(plan.target_user)
        print("  post-write live start (fail closed):")
        print(
            "    start: "
            f"{_format_command(('systemctl', 'start', plan.enabled_unit or ''), use_sudo=plan.use_sudo)}"
        )
        print(f"    restore if previously active: {workflow_unit}")


def _print_warnings(plan: SystemdInstallPlan) -> None:
    for warning in plan.warnings:
        print(f"warning: {warning}")


__all__ = [
    "DEFAULT_SYSTEMD_UNIT_DIR",
    "SYSTEMD_UNIT_NAMES",
    "RenderedUnit",
    "SystemdInstallOptions",
    "SystemdInstallPlan",
    "build_systemd_install_plan",
    "managed_runtime_units_for_user",
]
