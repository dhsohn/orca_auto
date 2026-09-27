"""``orca_auto init`` is driven through its terminal boundary: ``input``, ``getpass`` and
``sys.stdin``. Each test scripts the operator's answers and reads back the YAML it wrote."""

from __future__ import annotations

import getpass
import stat
import sys
from argparse import Namespace
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.orca.commands import init


@dataclass
class _Console:
    """Scripted answers for the wizard's prompts; an unscripted prompt fails the test."""

    answers: deque[str | BaseException] = field(default_factory=deque)
    secrets: deque[str] = field(default_factory=deque)
    prompts: list[str] = field(default_factory=list)
    secret_prompts: list[str] = field(default_factory=list)

    def type(self, *answers: str | BaseException) -> None:
        self.answers.extend(answers)

    def type_secret(self, *answers: str) -> None:
        self.secrets.extend(answers)

    def read(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.answers:
            pytest.fail(f"unexpected prompt: {prompt!r}")
        answer = self.answers.popleft()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def read_secret(self, prompt: str = "") -> str:
        self.secret_prompts.append(prompt)
        if not self.secrets:
            pytest.fail(f"unexpected secret prompt: {prompt!r}")
        return self.secrets.popleft()


@dataclass
class _Stdin:
    tty: bool

    def isatty(self) -> bool:
        return self.tty


@pytest.fixture
def console(monkeypatch: pytest.MonkeyPatch) -> _Console:
    console = _Console()
    monkeypatch.setattr("builtins.input", console.read)
    monkeypatch.setattr(getpass, "getpass", console.read_secret)
    return console


@pytest.fixture
def interactive_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=True))


@pytest.fixture
def piped_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=False))


@pytest.fixture
def default_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where ``init`` writes when no ``--config`` is given: the environment override."""

    config_path = tmp_path / "orca_auto.yaml"
    monkeypatch.setenv(ORCA_AUTO_CONFIG_ENV_VAR, str(config_path))
    return config_path


def _written_yaml(config_path: Path) -> dict[str, object]:
    return yaml.safe_load(config_path.read_text(encoding="utf-8").split("\n", 1)[1])


# --- individual prompts ---------------------------------------------------------------------


def test_prompt_text_returns_value_or_default(console: _Console) -> None:
    console.type("typed")
    assert init._prompt_text("label", "default") == "typed"

    console.type("   ", "   ")
    assert init._prompt_text("label", "default") == "default"
    assert init._prompt_text("label") == ""

    assert console.prompts == ["label [default]: ", "label [default]: ", "label: "]


def test_prompt_secret_text_uses_hidden_input(console: _Console) -> None:
    console.type_secret("  token  ")

    assert init._prompt_secret_text("Discord bot token") == "token"

    assert console.secret_prompts == ["Discord bot token: "]
    assert console.prompts == []


def test_prompt_yes_no_handles_defaults_and_reprompts(
    console: _Console, capsys: pytest.CaptureFixture[str]
) -> None:
    console.type("")
    assert init._prompt_yes_no("Proceed?", default=True) is True

    console.type("maybe", "n")
    assert init._prompt_yes_no("Proceed?", default=True) is False

    assert "Please answer y or n." in capsys.readouterr().out


def test_normalize_linux_path_rejects_blank_windows_and_relative(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert init._normalize_linux_path("", label="allowed_root") is None
    assert init._normalize_linux_path(r"C:\\runs", label="runs_root") is None
    assert init._normalize_linux_path("relative/path", label="allowed_root") is None

    resolved = init._normalize_linux_path(str(tmp_path / "runs"), label="allowed_root")
    assert resolved == (tmp_path / "runs").resolve()

    output = capsys.readouterr().out
    assert "allowed_root is required." in output
    assert "must be a Linux path" in output
    assert "must be an absolute Linux path" in output


def test_prompt_orca_executable_retries_until_existing_file(
    console: _Console, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake_dir = tmp_path / "not_a_file"
    fake_dir.mkdir()
    not_executable = tmp_path / "not_executable"
    not_executable.write_text("", encoding="utf-8")
    not_executable.chmod(0o644)
    real_bin = tmp_path / "orca"
    real_bin.write_text("", encoding="utf-8")
    real_bin.chmod(0o755)
    console.type(
        str(tmp_path / "missing.exe"),
        str(tmp_path / "missing"),
        str(fake_dir),
        str(not_executable),
        str(real_bin),
    )

    assert init._prompt_orca_executable() == str(real_bin.resolve())

    # The wizard now reports exactly what the config loader reports, so a path
    # the prompt accepts cannot be rejected at startup. These messages also do
    # not echo the rejected path, matching the validator's redaction contract.
    output = capsys.readouterr().out
    assert "must point to a Linux ORCA binary, not a Windows executable" in output
    assert "orca_executable not found" in output
    assert "orca_executable is not a file." in output
    assert "orca_executable is not executable." in output
    assert str(tmp_path / "missing.exe") not in output
    assert console.prompts == ["ORCA executable path: "] * 5


def test_prompt_directory_path_retries_when_existing_path_is_file(
    console: _Console, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    file_path = tmp_path / "single_file"
    file_path.write_text("", encoding="utf-8")
    dir_path = tmp_path / "allowed"
    console.type(str(file_path), str(dir_path))

    assert init._prompt_directory_path("allowed_root directory") == dir_path.resolve()

    assert "is not a directory" in capsys.readouterr().out


def test_ensure_directory_covers_existing_decline_and_create(
    console: _Console, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    assert init._ensure_directory(existing, label="allowed_root") is True
    assert console.prompts == []

    missing = tmp_path / "missing"
    console.type("n")
    assert init._ensure_directory(missing, label="allowed_root") is False
    assert "allowed_root was not created." in capsys.readouterr().out
    assert not missing.exists()

    console.type("y")
    assert init._ensure_directory(missing, label="allowed_root") is True
    assert missing.is_dir()
    assert console.prompts == ["allowed_root does not exist. Create it now? [Y/n]: "] * 2


def test_prompt_max_active_simulations_validates(
    console: _Console, capsys: pytest.CaptureFixture[str]
) -> None:
    console.type("abc", "0", "4")

    assert init._prompt_max_active_simulations() == 4

    assert "max_active_simulations must be an integer >= 1." in capsys.readouterr().out
    assert console.prompts == ["max_active_simulations [4]: "] * 3


def test_prompt_discord_config_covers_skip_and_retry(
    console: _Console, capsys: pytest.CaptureFixture[str]
) -> None:
    console.type("n")
    assert init._prompt_discord_config() == {"bot_token": "", "default_channel_id": ""}
    assert console.secret_prompts == []

    # An empty token is rejected and both values are asked for again.
    console.type("y", "456", "456")
    console.type_secret("", "bot-token")
    assert init._prompt_discord_config() == {
        "bot_token": "bot-token",
        "default_channel_id": "456",
    }

    assert "Discord bot token" in capsys.readouterr().out
    assert console.secret_prompts == ["Discord bot token: "] * 2


def test_prompt_messenger_config_uses_discord_adapter(console: _Console) -> None:
    console.type("y", "123")
    console.type_secret("bot-token")

    assert init._prompt_messenger_config() == {
        "provider": "discord",
        "discord": {"bot_token": "bot-token", "default_channel_id": "123"},
    }


def test_write_config_adds_generated_header(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "orca_auto.yaml"
    payload = {"runtime": {"allowed_root": "/tmp/runs"}}

    init._write_config(config_path, payload)

    written = config_path.read_text(encoding="utf-8")
    assert written.startswith("# Generated by orca_auto init\n")
    assert yaml.safe_load(written.split("\n", 1)[1]) == payload
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


# --- cmd_init -------------------------------------------------------------------------------


@pytest.mark.usefixtures("interactive_stdin")
def test_cmd_init_returns_zero_when_existing_config_not_overwritten(
    default_config_path: Path, console: _Console, capsys: pytest.CaptureFixture[str]
) -> None:
    default_config_path.write_text("existing: true\n", encoding="utf-8")
    console.type("n")

    assert init.cmd_init(Namespace(force=False)) == 0

    assert "Cancelled." in capsys.readouterr().out
    assert console.prompts == [
        f"Config already exists at {default_config_path}. Overwrite it? [y/N]: "
    ]
    assert default_config_path.read_text(encoding="utf-8") == "existing: true\n"


@pytest.mark.usefixtures("piped_stdin")
def test_cmd_init_existing_config_in_noninteractive_mode_requires_force(
    default_config_path: Path, console: _Console, capsys: pytest.CaptureFixture[str]
) -> None:
    default_config_path.write_text("existing: true\n", encoding="utf-8")

    assert init.cmd_init(Namespace(force=False)) == 1

    # No prompt was attempted: an unscripted prompt would have failed the test.
    assert console.prompts == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: Config already exists at ")
    assert "Re-run with --force to overwrite it without confirmation." in captured.err


def test_cmd_init_handles_interrupt(
    default_config_path: Path, console: _Console, capsys: pytest.CaptureFixture[str]
) -> None:
    console.type(KeyboardInterrupt())

    assert init.cmd_init(Namespace(force=True)) == 1

    assert "Cancelled." in capsys.readouterr().out
    assert not default_config_path.exists()


def test_cmd_init_handles_write_or_load_failure(
    tmp_path: Path,
    fake_orca: Path,
    console: _Console,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The config's parent path is an existing file, so the generated config
    # cannot be written after every prompt was answered.
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    config_path = blocker / "orca_auto.yaml"
    monkeypatch.setenv(ORCA_AUTO_CONFIG_ENV_VAR, str(config_path))
    orca_allowed_root = tmp_path / "orca_allowed"
    console.type(str(orca_allowed_root), "y", str(fake_orca), "4", "n")

    assert init.cmd_init(Namespace(force=True)) == 1

    captured = capsys.readouterr()
    assert "Failed to generate config:" not in captured.out
    assert "error: Failed to generate config:" in captured.err
    assert not config_path.exists()
    assert blocker.is_file()


def test_cmd_init_success_writes_config_and_prints_summary(
    tmp_path: Path,
    fake_orca: Path,
    default_config_path: Path,
    console: _Console,
    capsys: pytest.CaptureFixture[str],
) -> None:
    orca_allowed_root = tmp_path / "orca_allowed"
    console.type(str(orca_allowed_root), "y", str(fake_orca), "4", "y", "123")
    console.type_secret("token")

    assert init.cmd_init(Namespace(force=True)) == 0

    assert orca_allowed_root.is_dir()
    output = capsys.readouterr().out
    assert "Config created successfully." in output
    assert "runs_root" in output
    assert str(orca_allowed_root) in output
    assert "max_active_simulations: 4" in output
    assert "messenger_provider: discord" in output
    assert "token" not in output
    assert _written_yaml(default_config_path) == {
        "runs_root": str(orca_allowed_root.resolve()),
        "resources": {
            "max_cores_per_task": 8,
            "max_memory_gb_per_task": 32,
        },
        "scheduler": {
            "max_active_simulations": 4,
        },
        "messenger": {
            "provider": "discord",
            "discord": {"bot_token": "token", "default_channel_id": "123"},
        },
        "orca": {
            "runtime": {},
            "paths": {"orca_executable": str(fake_orca.resolve())},
        },
    }
    assert stat.S_IMODE(default_config_path.stat().st_mode) == 0o600


def test_cmd_init_force_preserves_existing_discord_messenger(
    tmp_path: Path,
    fake_orca: Path,
    default_config_path: Path,
    console: _Console,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bot_token = "existing-bot-secret"
    existing_discord = {"bot_token": bot_token, "default_channel_id": "123456789012345678"}
    default_config_path.write_text(
        yaml.safe_dump(
            {"messenger": {"provider": "discord", "discord": existing_discord}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orca_allowed_root = tmp_path / "orca_allowed"
    console.type(str(orca_allowed_root), "y", str(fake_orca), "4", "y")

    assert init.cmd_init(Namespace(force=True)) == 0

    # Keeping the existing settings skips the Discord prompts entirely.
    assert console.prompts[-1] == "Keep the existing messenger settings? [Y/n]: "
    assert console.secret_prompts == []
    assert _written_yaml(default_config_path)["messenger"] == {
        "provider": "discord",
        "discord": existing_discord,
    }
    output = capsys.readouterr().out
    assert "Preserving existing messenger settings (discord)." in output
    assert bot_token not in output


def test_prompt_init_values_can_replace_existing_messenger(
    tmp_path: Path, fake_orca: Path, console: _Console
) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    console.type(str(runs_root), str(fake_orca), "4", "n", "y", "200")
    console.type_secret("token")

    values = init._prompt_init_values(
        existing_messenger={"provider": "discord", "discord": {"bot_token": "old"}}
    )

    assert values.messenger == {
        "provider": "discord",
        "discord": {"bot_token": "token", "default_channel_id": "200"},
    }
    assert values.orca_runtime == {
        "runs_root": str(runs_root.resolve()),
        "executable": str(fake_orca.resolve()),
    }
    assert values.max_active_simulations == 4
