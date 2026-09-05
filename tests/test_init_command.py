from __future__ import annotations

import stat
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import yaml

from orca_auto.orca.commands import init


def test_prompt_text_returns_value_or_default() -> None:
    with patch("builtins.input", return_value="typed"):
        assert init._prompt_text("label", "default") == "typed"

    with patch("builtins.input", return_value="   "):
        assert init._prompt_text("label", "default") == "default"
        assert init._prompt_text("label") == ""


def test_prompt_secret_text_uses_hidden_input() -> None:
    with patch("orca_auto.orca.commands.init.getpass.getpass", return_value="  token  ") as prompt:
        assert init._prompt_secret_text("Discord bot token") == "token"

    prompt.assert_called_once_with("Discord bot token: ")


def test_prompt_yes_no_handles_defaults_and_reprompts(capsys) -> None:
    with patch("builtins.input", return_value=""):
        assert init._prompt_yes_no("Proceed?", default=True) is True

    with patch("builtins.input", side_effect=["maybe", "n"]):
        assert init._prompt_yes_no("Proceed?", default=True) is False

    assert "Please answer y or n." in capsys.readouterr().out


def test_normalize_linux_path_rejects_blank_windows_and_relative(capsys, tmp_path: Path) -> None:
    assert init._normalize_linux_path("", label="allowed_root") is None
    assert init._normalize_linux_path(r"C:\\runs", label="runs_root") is None
    assert init._normalize_linux_path("relative/path", label="allowed_root") is None

    resolved = init._normalize_linux_path(str(tmp_path / "runs"), label="allowed_root")
    assert resolved == (tmp_path / "runs").resolve()

    output = capsys.readouterr().out
    assert "allowed_root is required." in output
    assert "must be a Linux path" in output
    assert "must be an absolute Linux path" in output


def test_prompt_orca_executable_retries_until_existing_file(capsys, tmp_path: Path) -> None:
    fake_dir = tmp_path / "not_a_file"
    fake_dir.mkdir()
    not_executable = tmp_path / "not_executable"
    not_executable.write_text("", encoding="utf-8")
    not_executable.chmod(0o644)
    real_bin = tmp_path / "orca"
    real_bin.write_text("", encoding="utf-8")
    real_bin.chmod(0o755)

    with patch(
        "orca_auto.orca.commands.init._prompt_text",
        side_effect=[
            str(tmp_path / "missing.exe"),
            str(tmp_path / "missing"),
            str(fake_dir),
            str(not_executable),
            str(real_bin),
        ],
    ):
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


def test_prompt_directory_path_retries_when_existing_path_is_file(capsys, tmp_path: Path) -> None:
    file_path = tmp_path / "single_file"
    file_path.write_text("", encoding="utf-8")
    dir_path = tmp_path / "allowed"

    with patch(
        "orca_auto.orca.commands.init._prompt_text",
        side_effect=[str(file_path), str(dir_path)],
    ):
        assert init._prompt_directory_path("allowed_root directory") == dir_path.resolve()

    assert "is not a directory" in capsys.readouterr().out


def test_ensure_directory_covers_existing_decline_and_create(capsys, tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    assert init._ensure_directory(existing, label="allowed_root") is True

    missing = tmp_path / "missing"
    with patch("orca_auto.orca.commands.init._prompt_yes_no", return_value=False):
        assert init._ensure_directory(missing, label="allowed_root") is False
    assert "allowed_root was not created." in capsys.readouterr().out

    with patch("orca_auto.orca.commands.init._prompt_yes_no", return_value=True):
        assert init._ensure_directory(missing, label="allowed_root") is True
    assert missing.is_dir()


def test_prompt_max_active_simulations_validates(capsys) -> None:
    with patch("orca_auto.orca.commands.init._prompt_text", side_effect=["abc", "0", "4"]):
        assert init._prompt_max_active_simulations() == 4

    output = capsys.readouterr().out
    assert "max_active_simulations must be an integer >= 1." in output


def test_prompt_discord_config_covers_skip_and_retry(capsys) -> None:
    with patch("orca_auto.orca.commands.init._prompt_yes_no", return_value=False):
        assert init._prompt_discord_config() == {
            "bot_token": "",
            "default_channel_id": "",
        }

    with (
        patch("orca_auto.orca.commands.init._prompt_yes_no", return_value=True),
        patch(
            "orca_auto.orca.commands.init._prompt_secret_text",
            side_effect=["", "bot-token"],
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_text",
            side_effect=["456", "456"],
        ),
    ):
        assert init._prompt_discord_config() == {
            "bot_token": "bot-token",
            "default_channel_id": "456",
        }

    assert "Discord bot token" in capsys.readouterr().out


def test_prompt_messenger_config_uses_discord_adapter() -> None:
    with patch(
        "orca_auto.orca.commands.init._prompt_discord_config",
        return_value={"bot_token": "bot-token", "default_channel_id": "123"},
    ):
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


def test_cmd_init_returns_zero_when_existing_config_not_overwritten(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text("existing: true\n", encoding="utf-8")

    with (
        patch("orca_auto.orca.commands.init.default_config_path", return_value=str(config_path)),
        patch(
            "orca_auto.orca.commands.init._stdin_supports_interactive_prompts",
            return_value=True,
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_yes_no",
            return_value=False,
        ),
    ):
        assert init.cmd_init(Namespace(force=False)) == 0

    assert "Cancelled." in capsys.readouterr().out


def test_cmd_init_existing_config_in_noninteractive_mode_requires_force(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text("existing: true\n", encoding="utf-8")

    with (
        patch("orca_auto.orca.commands.init.default_config_path", return_value=str(config_path)),
        patch(
            "orca_auto.orca.commands.init._stdin_supports_interactive_prompts",
            return_value=False,
        ),
        patch("orca_auto.orca.commands.init._prompt_yes_no") as prompt_yes_no,
    ):
        assert init.cmd_init(Namespace(force=False)) == 1

    prompt_yes_no.assert_not_called()
    output = capsys.readouterr().out
    assert "Re-run with --force to overwrite it without confirmation." in output


def test_cmd_init_handles_interrupt(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "orca_auto.yaml"

    with (
        patch("orca_auto.orca.commands.init.default_config_path", return_value=str(config_path)),
        patch(
            "orca_auto.orca.commands.init._prompt_orca_runtime",
            side_effect=KeyboardInterrupt,
        ),
    ):
        assert init.cmd_init(Namespace(force=True)) == 1

    assert "Cancelled." in capsys.readouterr().out


def test_cmd_init_handles_write_or_load_failure(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    orca_allowed_root = tmp_path / "orca_allowed"

    with (
        patch("orca_auto.orca.commands.init.default_config_path", return_value=str(config_path)),
        patch(
            "orca_auto.orca.commands.init._prompt_orca_runtime",
            return_value={
                "runs_root": str(orca_allowed_root),
                "executable": "/usr/bin/orca",
            },
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_xtb_runtime",
            return_value={
                "executable": "/usr/bin/xtb",
            },
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_crest_runtime",
            return_value={
                "executable": "/usr/bin/crest",
            },
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_max_active_simulations",
            return_value=4,
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_messenger_config",
            return_value={
                "provider": "discord",
                "discord": {"bot_token": "", "default_channel_id": ""},
            },
        ),
        patch(
            "orca_auto.orca.commands.init._validate_generated_config",
            side_effect=RuntimeError("bad config"),
        ),
    ):
        assert init.cmd_init(Namespace(force=True)) == 1

    assert "Failed to generate config: bad config" in capsys.readouterr().out


def test_cmd_init_success_writes_config_and_prints_summary(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    orca_allowed_root = tmp_path / "orca_allowed"

    with (
        patch("orca_auto.orca.commands.init.default_config_path", return_value=str(config_path)),
        patch(
            "orca_auto.orca.commands.init._prompt_orca_runtime",
            return_value={
                "runs_root": str(orca_allowed_root),
                "executable": "/usr/bin/orca",
            },
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_xtb_runtime",
            return_value={
                "executable": "/usr/bin/xtb",
            },
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_crest_runtime",
            return_value={
                "executable": "/usr/bin/crest",
            },
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_max_active_simulations",
            return_value=4,
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_messenger_config",
            return_value={
                "provider": "discord",
                "discord": {"bot_token": "token", "default_channel_id": "123"},
            },
        ),
        patch(
            "orca_auto.orca.commands.init._validate_generated_config"
        ) as validate_generated_config,
    ):
        assert init.cmd_init(Namespace(force=True)) == 0

    validate_generated_config.assert_called_once_with(str(config_path.resolve()))
    output = capsys.readouterr().out
    assert "Config created successfully." in output
    assert "runs_root" in output
    assert str(orca_allowed_root) in output
    assert "xtb_executable" in output
    assert "crest_executable" in output
    assert "max_active_simulations: 4" in output
    assert "messenger_provider: discord" in output
    assert yaml.safe_load(config_path.read_text(encoding="utf-8").split("\n", 1)[1]) == {
        "runs_root": str(orca_allowed_root),
        "resources": {
            "max_cores_per_task": 8,
            "max_memory_gb_per_task": 32,
        },
        "scheduler": {
            "max_active_simulations": 4,
        },
        "workflow": {
            "paths": {
                "xtb_executable": "/usr/bin/xtb",
                "crest_executable": "/usr/bin/crest",
            },
        },
        "messenger": {
            "provider": "discord",
            "discord": {"bot_token": "token", "default_channel_id": "123"},
        },
        "orca": {
            "runtime": {},
            "paths": {"orca_executable": "/usr/bin/orca"},
        },
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_cmd_init_force_preserves_existing_discord_messenger(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    bot_token = "existing-bot-secret"
    existing_discord = {"bot_token": bot_token, "default_channel_id": "123456789012345678"}
    config_path.write_text(
        yaml.safe_dump(
            {
                "messenger": {
                    "provider": "discord",
                    "discord": existing_discord,
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orca_allowed_root = tmp_path / "orca_allowed"

    with (
        patch("orca_auto.orca.commands.init.default_config_path", return_value=str(config_path)),
        patch(
            "orca_auto.orca.commands.init._prompt_orca_runtime",
            return_value={
                "runs_root": str(orca_allowed_root),
                "executable": "/usr/bin/orca",
            },
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_xtb_runtime",
            return_value={"executable": "/usr/bin/xtb"},
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_crest_runtime",
            return_value={"executable": "/usr/bin/crest"},
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_max_active_simulations",
            return_value=4,
        ),
        patch("orca_auto.orca.commands.init._prompt_yes_no", return_value=True),
        patch("orca_auto.orca.commands.init._prompt_messenger_config") as messenger_prompt,
        patch("orca_auto.orca.commands.init._validate_generated_config"),
    ):
        assert init.cmd_init(Namespace(force=True)) == 0

    messenger_prompt.assert_not_called()
    written = yaml.safe_load(config_path.read_text(encoding="utf-8").split("\n", 1)[1])
    assert written["messenger"] == {
        "provider": "discord",
        "discord": existing_discord,
    }
    output = capsys.readouterr().out
    assert "Preserving existing messenger settings (discord)." in output
    assert bot_token not in output


def test_prompt_init_values_can_replace_existing_messenger() -> None:
    replacement = {
        "provider": "discord",
        "discord": {
            "bot_token": "token",
            "default_channel_id": "200",
        },
    }
    with (
        patch("orca_auto.orca.commands.init._prompt_yes_no", return_value=False),
        patch(
            "orca_auto.orca.commands.init._prompt_messenger_config",
            return_value=replacement,
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_orca_runtime",
            return_value={
                "runs_root": "/runs",
                "executable": "/usr/bin/orca",
            },
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_xtb_runtime",
            return_value={"executable": "/usr/bin/xtb"},
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_crest_runtime",
            return_value={"executable": "/usr/bin/crest"},
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_max_active_simulations",
            return_value=4,
        ),
    ):
        values = init._prompt_init_values(
            existing_messenger={"provider": "discord", "discord": {"bot_token": "old"}}
        )

    assert values.messenger == replacement
