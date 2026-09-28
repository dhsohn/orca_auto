from __future__ import annotations

import math
import os
import pwd
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from orca_auto.cli_worker_supervision import worker_stop_budget_seconds
from orca_auto.core.config.files import (
    ORCA_AUTO_CONFIG_ENV_VAR,
    SharedConfig,
    usable_runs_root_text,
)
from orca_auto.core.queue.processes import KILL_TIMEOUT_SECONDS
from orca_auto.core.runtime_bundle import (
    PROCESS_RUNTIME_BUILD_ENV,
    RUNTIME_MANIFEST_NAME,
    verify_runtime_bundle,
)
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.orca.config import (
    OrcaConfigSections,
    load_config,
    load_orca_shared_config,
    worker_config,
)

SYSTEMD_UNIT_NAMES = (
    "orca_auto-engine-workers@.target",
    "orca_auto-queue-worker@.service",
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


def running_as_root() -> bool:
    return os.geteuid() == 0


def _existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _needs_sudo(unit_dir: Path, *, is_root: Callable[[], bool] = running_as_root) -> bool:
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
_SYSTEMD_UNSUPPORTED_PATH_CHARACTERS = frozenset({'"', "'", "\\", "$"})


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
    if any(character in _SYSTEMD_UNSUPPORTED_PATH_CHARACTERS for character in text):
        raise ValueError(
            f"{label} must not contain quotes, backslashes, or dollar signs for "
            f"systemd unit rendering: {text}"
        )
    # systemd expands ``%`` specifiers in every setting where these paths are
    # inserted (WorkingDirectory, Environment, ExecStart, and ReadWritePaths).
    # Escape only the path value here, before it replaces the template-owned
    # ``/home/%i/orca_auto`` placeholder, so literal percent signs survive while
    # the template's account instance specifiers keep their intended meaning.
    return text.replace("%", "%%")


def _configured_read_write_path(shared: SharedConfig | None) -> Path | None:
    # The worker writes only under runs_root, including the admission store it
    # creates at <runs_root>/.admission. Naming that not-yet-created child as a
    # mandatory systemd path would keep the service namespace from starting.
    text = normalize_text(usable_runs_root_text(shared.runs_root)) if shared is not None else ""
    candidate = Path(text).expanduser() if text else None
    if candidate is None or not candidate.is_absolute():
        return None
    return candidate.resolve(strict=False)


def _render_read_write_paths(path: Path | None) -> str:
    if path is None:
        return "# ReadWritePaths omitted: config runtime paths unavailable at render time"
    return f"ReadWritePaths={_systemd_path_text(path, label='ReadWritePaths path')}"


def _configured_stop_timeout_seconds(shared: SharedConfig | None) -> int | None:
    """The supervisor's stop budget plus its kill wait, or None without a config."""
    if shared is None:
        return None
    budget = worker_stop_budget_seconds(shared.scheduler.max_active_simulations)
    # One extra second so systemd's SIGKILL can never precede the supervisor's own kill wait.
    return math.ceil(budget + KILL_TIMEOUT_SECONDS) + 1


def _render_unit_template(
    template: str,
    *,
    repo_text: str,
    config_text: str,
    read_write_paths: str,
    stop_timeout_seconds: int | None,
    runtime_build: str,
) -> str:
    rendered = template.replace("/home/%i/orca_auto", repo_text)
    lines = []
    config_environment_prefix = f"Environment={ORCA_AUTO_CONFIG_ENV_VAR}="
    for line in rendered.splitlines():
        if line.startswith(config_environment_prefix):
            lines.append(f"{config_environment_prefix}{config_text}")
        elif runtime_build and line.startswith("ExecStart="):
            lines.extend(
                [
                    f"Environment={PROCESS_RUNTIME_BUILD_ENV}={runtime_build}",
                    "Environment=PYTHONPATH=",
                    "Environment=PYTHONNOUSERSITE=1",
                    f"ReadOnlyPaths={repo_text}",
                    line.replace("/bin/python -m ", "/bin/python -I -m "),
                ]
            )
        elif line.strip() == _SYSTEMD_READ_WRITE_PLACEHOLDER:
            lines.append(read_write_paths)
        elif line.startswith("TimeoutStopSec=") and stop_timeout_seconds is not None:
            lines.append(f"TimeoutStopSec={stop_timeout_seconds}")
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def _default_config_for_user(target_user: str) -> Path:
    """Default rendered config path: the target user's discoverable home config.

    Matches the runtime discovery order (``--config``, ``ORCA_AUTO_CONFIG``,
    ``~/orca_auto/config/orca_auto.yaml``); the unit template carries the same
    ``/home/%i/...`` shape, so an account without a passwd entry falls back to it.
    """
    try:
        home = Path(pwd.getpwnam(target_user).pw_dir)
    except KeyError:
        home = Path("/home") / target_user
    return home / "orca_auto" / "config" / "orca_auto.yaml"


def _normalize_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def runtime_unit_for_user(target_user: str) -> str:
    return f"orca_auto-runtime@{target_user}.target"


def engine_workers_unit_for_user(target_user: str) -> str:
    return f"orca_auto-engine-workers@{target_user}.target"


def worker_unit_for_user(target_user: str) -> str:
    return f"orca_auto-queue-worker@{target_user}.service"


def _enabled_unit_for_args(*, target_user: str, worker_only: bool, no_enable: bool) -> str | None:
    if no_enable:
        return None
    if worker_only:
        return engine_workers_unit_for_user(target_user)
    return runtime_unit_for_user(target_user)


def _systemctl_transition_commands(
    *,
    target_user: str,
    worker_only: bool,
    enabled_unit: str | None,
    no_start: bool,
) -> tuple[tuple[str, ...], ...]:
    commands: list[tuple[str, ...]] = [("systemctl", "daemon-reload")]
    if enabled_unit is None:
        return tuple(commands)

    # Stage the desired boot unit before touching the currently selected mode.
    # Live services are not stopped until the desired target has restarted
    # successfully, so an intermediate command failure cannot turn an install
    # error into a worker outage.
    commands.append(("systemctl", "enable", enabled_unit))
    opposite_unit = (
        runtime_unit_for_user(target_user)
        if worker_only
        else engine_workers_unit_for_user(target_user)
    )
    if not no_start:
        # An explicit install transition is an operator-requested recovery.
        # Clear bounded service start-limit counters so they cannot block this
        # operator-requested recovery inside their five-minute windows.
        commands.append(("systemctl", "reset-failed", worker_unit_for_user(target_user)))
        # `restart` also starts an inactive unit and reapplies the runtime
        # target's Wants= graph when the requested mode was already active. It
        # does not restart member services that are already running (observed
        # on the deploy host: the queue worker kept its start timestamp), so a
        # deploy to running workers needs `orca_auto service restart`, which
        # restarts the worker services explicitly.
        commands.append(("systemctl", "restart", enabled_unit))
        # Targets use Wants=, so a successful target job does not prove that its
        # service started. Gate destructive cleanup on the actual worker unit.
        commands.append(("systemctl", "is-active", "--quiet", worker_unit_for_user(target_user)))
    # Older worker-only installs enabled the ORCA service directly. Remove only
    # its boot selection after the desired target is ready; stopping this shared
    # service would also stop the freshly restarted runtime.
    commands.append(("systemctl", "disable", worker_unit_for_user(target_user)))
    # The opposite target can share live services with the selected target.
    # Disable its boot selection without --now so the successful desired restart
    # remains intact if this final cleanup fails.
    commands.append(("systemctl", "disable", opposite_unit))
    return tuple(commands)


def _validate_worker_config(
    config: Path, loaded: tuple[Path, SharedConfig, OrcaConfigSections] | None
) -> None:
    """Apply the supervised ORCA worker's config checks to the loaded config."""

    try:
        if loaded is None:
            # Raises the worker's own missing-config error.
            load_config(str(config))
        else:
            worker_config(*loaded)
    except Exception as exc:
        raise ValueError(f"runtime config preflight failed: {exc}") from exc


def _collect_warnings(
    repo: Path,
    config: Path,
    *,
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
    if no_enable:
        warnings.append(
            "--no-enable leaves the existing boot selection and live services unchanged; "
            "only unit files are installed and systemd is reloaded"
        )
    elif no_start:
        warnings.append(
            "--no-start updates the boot selection without stopping or restarting live "
            "services; rerun without --no-start to apply the selected mode immediately"
        )
    return tuple(warnings)


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
    is_root: Callable[[], bool] = running_as_root,
) -> SystemdInstallPlan:
    user_text = normalize_text(target_user)
    if not user_text:
        raise ValueError("--user is required")
    user_text = _validate_target_user(user_text)

    repo_text = normalize_text(repo)
    if not repo_text:
        raise ValueError("--repo is required")

    repo_path = _normalize_path(repo_text)
    config_path = _normalize_path(config or _default_config_for_user(user_text))
    unit_dir_path = _normalize_path(unit_dir)
    # ``--repo`` is a required public argument and names the checkout whose
    # service code will run. Its versioned unit files are therefore the sole
    # template source, including when this installer itself came from a wheel.
    template_root = _template_dir(repo_path)
    runtime_build = ""
    if (repo_path / RUNTIME_MANIFEST_NAME).exists():
        runtime_build = verify_runtime_bundle(repo_path)["build_id"]
        if config_path.is_relative_to(repo_path):
            raise ValueError(
                "prepared runtime configuration must be outside the read-only runtime; pass --config"
            )
    if not template_root.is_dir():
        raise ValueError(
            f"--repo must name a checkout that contains a systemd/ template directory: {repo_path}"
        )
    # A config that exists but cannot be loaded fails the install: units
    # rendered without ReadWritePaths would start a worker that cannot write.
    loaded = load_orca_shared_config(config_path) if config_path.exists() else None
    shared = loaded[1] if loaded is not None else None
    repo_unit_text = _systemd_path_text(repo_path, label="--repo")
    config_unit_text = _systemd_path_text(config_path, label="--config")
    read_write_paths = _render_read_write_paths(_configured_read_write_path(shared))
    stop_timeout_seconds = _configured_stop_timeout_seconds(shared)
    units = tuple(
        RenderedUnit(
            name=name,
            destination=unit_dir_path / name,
            content=_render_unit_template(
                _read_unit_template(template_root, name),
                repo_text=repo_unit_text,
                config_text=config_unit_text,
                read_write_paths=read_write_paths,
                stop_timeout_seconds=stop_timeout_seconds,
                runtime_build=runtime_build,
            ),
        )
        for name in SYSTEMD_UNIT_NAMES
    )

    enabled_unit = _enabled_unit_for_args(
        target_user=user_text, worker_only=worker_only, no_enable=no_enable
    )
    if enabled_unit is not None:
        _validate_worker_config(config_path, loaded)
    commands = _systemctl_transition_commands(
        target_user=user_text,
        worker_only=worker_only,
        enabled_unit=enabled_unit,
        no_start=no_start,
    )

    return SystemdInstallPlan(
        target_user=user_text,
        repo=repo_path,
        config=config_path,
        unit_dir=unit_dir_path,
        units=units,
        commands=commands,
        enabled_unit=enabled_unit,
        use_sudo=False if no_sudo else _needs_sudo(unit_dir_path, is_root=is_root),
        warnings=_collect_warnings(repo_path, config_path, no_enable=no_enable, no_start=no_start),
    )


def systemd_command_argv(command: Sequence[str], *, use_sudo: bool) -> tuple[str, ...]:
    return (("sudo",) if use_sudo else ()) + tuple(command)


def format_command(command: Sequence[str], *, use_sudo: bool) -> str:
    return " ".join(systemd_command_argv(command, use_sudo=use_sudo))


__all__ = [
    "DEFAULT_SYSTEMD_UNIT_DIR",
    "SYSTEMD_UNIT_NAMES",
    "RenderedUnit",
    "SystemdInstallPlan",
    "build_systemd_install_plan",
    "engine_workers_unit_for_user",
    "format_command",
    "running_as_root",
    "runtime_unit_for_user",
    "systemd_command_argv",
    "worker_unit_for_user",
]
