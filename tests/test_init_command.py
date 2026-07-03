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
        assert init._prompt_secret_text("Telegram bot token") == "token"

    prompt.assert_called_once_with("Telegram bot token: ")


def test_prompt_yes_no_handles_defaults_and_reprompts(capsys) -> None:
    with patch("builtins.input", return_value=""):
        assert init._prompt_yes_no("Proceed?", default=True) is True

    with patch("builtins.input", side_effect=["maybe", "n"]):
        assert init._prompt_yes_no("Proceed?", default=True) is False

    assert "Please answer y or n." in capsys.readouterr().out


def test_normalize_linux_path_rejects_blank_windows_and_relative(capsys, tmp_path: Path) -> None:
    assert init._normalize_linux_path("", label="allowed_root") is None
    assert init._normalize_linux_path(r"C:\\orca.exe", label="orca_executable") is None
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

    output = capsys.readouterr().out
    assert "Windows .exe" in output
    assert "File not found" in output
    assert "Path is not a file" in output
    assert "Path is not executable" in output


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


def test_prompt_organized_root_retries_when_nested_under_allowed_root(
    capsys, tmp_path: Path
) -> None:
    allowed_root = (tmp_path / "allowed").resolve()
    allowed_root.mkdir()
    nested = allowed_root / "organized"
    valid = (tmp_path / "organized").resolve()

    with (
        patch(
            "orca_auto.orca.commands.init._prompt_directory_path",
            side_effect=[nested, valid],
        ),
        patch("orca_auto.orca.commands.init._ensure_directory", return_value=True),
    ):
        assert init._prompt_organized_root(
            allowed_root, engine_key="orca", engine_label="ORCA"
        ) == str(valid)

    assert "must not contain each other" in capsys.readouterr().out


def test_prompt_default_max_retries_and_max_active_simulations_validate(capsys) -> None:
    with patch("orca_auto.orca.commands.init._prompt_text", side_effect=["abc", "-1", "2"]):
        assert init._prompt_default_max_retries() == 2

    with patch("orca_auto.orca.commands.init._prompt_text", side_effect=["abc", "0", "4"]):
        assert init._prompt_max_active_simulations() == 4

    output = capsys.readouterr().out
    assert "default_max_retries must be an integer >= 0." in output
    assert "max_active_simulations must be an integer >= 1." in output


def test_prompt_telegram_config_covers_skip_and_retry(capsys) -> None:
    with patch("orca_auto.orca.commands.init._prompt_yes_no", return_value=False):
        assert init._prompt_telegram_config() == {"bot_token": "", "chat_id": ""}

    with (
        patch("orca_auto.orca.commands.init._prompt_yes_no", return_value=True),
        patch(
            "orca_auto.orca.commands.init._prompt_secret_text",
            side_effect=["token-only", "token"],
        ),
        patch("orca_auto.orca.commands.init._prompt_text", side_effect=["", "123"]),
    ):
        assert init._prompt_telegram_config() == {"bot_token": "token", "chat_id": "123"}

    assert "Both Telegram bot token and chat id are required" in capsys.readouterr().out


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
            "orca_auto.orca.commands.init._prompt_workflow_root",
            side_effect=KeyboardInterrupt,
        ),
    ):
        assert init.cmd_init(Namespace(force=True)) == 1

    assert "Cancelled." in capsys.readouterr().out


def test_cmd_init_handles_write_or_load_failure(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    workflow_root = tmp_path / "workflow_root"
    orca_allowed_root = tmp_path / "orca_allowed"

    with (
        patch("orca_auto.orca.commands.init.default_config_path", return_value=str(config_path)),
        patch(
            "orca_auto.orca.commands.init._prompt_workflow_root",
            return_value=str(workflow_root),
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_orca_runtime",
            return_value={
                "allowed_root": str(orca_allowed_root),
                "organized_root": str(tmp_path / "orca_organized"),
                "default_max_retries": 2,
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
            "orca_auto.orca.commands.init._prompt_telegram_config",
            return_value={"bot_token": "", "chat_id": ""},
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
    workflow_root = tmp_path / "workflow_root"
    orca_allowed_root = tmp_path / "orca_allowed"
    orca_organized_root = tmp_path / "orca_organized"

    with (
        patch("orca_auto.orca.commands.init.default_config_path", return_value=str(config_path)),
        patch(
            "orca_auto.orca.commands.init._prompt_workflow_root",
            return_value=str(workflow_root),
        ),
        patch(
            "orca_auto.orca.commands.init._prompt_orca_runtime",
            return_value={
                "allowed_root": str(orca_allowed_root),
                "organized_root": str(orca_organized_root),
                "default_max_retries": 2,
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
            "orca_auto.orca.commands.init._prompt_telegram_config",
            return_value={"bot_token": "token", "chat_id": "123"},
        ),
        patch(
            "orca_auto.orca.commands.init._validate_generated_config"
        ) as validate_generated_config,
    ):
        assert init.cmd_init(Namespace(force=True)) == 0

    validate_generated_config.assert_called_once_with(str(config_path.resolve()))
    output = capsys.readouterr().out
    assert "Config created successfully." in output
    assert "workflow.root" in output
    assert "orca_allowed_root" in output
    assert "xtb_executable" in output
    assert "crest_executable" in output
    assert "max_active_simulations: 4" in output
    assert yaml.safe_load(config_path.read_text(encoding="utf-8").split("\n", 1)[1]) == {
        "resources": {
            "max_cores_per_task": 8,
            "max_memory_gb_per_task": 32,
        },
        "scheduler": {
            "max_active_simulations": 4,
        },
        "workflow": {
            "root": str(workflow_root),
            "paths": {
                "xtb_executable": "/usr/bin/xtb",
                "crest_executable": "/usr/bin/crest",
            },
        },
        "telegram": {"bot_token": "token", "chat_id": "123"},
        "orca": {
            "runtime": {
                "allowed_root": str(orca_allowed_root),
                "organized_root": str(orca_organized_root),
                "default_max_retries": 2,
            },
            "paths": {"orca_executable": "/usr/bin/orca"},
        },
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
