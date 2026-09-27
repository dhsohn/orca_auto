from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core.paths.validation import (
    is_rejected_windows_path,
    is_subpath,
    validate_configured_executable_path,
)


@pytest.mark.parametrize(
    "path_text",
    [
        r"C:\chem\job",
        "/mnt/c/chem/job",
    ],
)
def test_windows_path_rejection(path_text: str) -> None:
    assert is_rejected_windows_path(path_text)


def test_executable_validation_rejects_symlink_to_windows_executable(tmp_path: Path) -> None:
    target = tmp_path / "cmd.exe"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    alias = tmp_path / "orca"
    alias.symlink_to(target)

    with pytest.raises(ValueError, match="must resolve to a Linux ORCA binary"):
        validate_configured_executable_path(
            alias,
            label="orca.paths.orca_executable",
            display_name="ORCA",
        )


def test_configured_executable_validation_errors_redact_every_raw_path_category(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private-executable-secret-directory"
    directory.mkdir()
    non_executable = tmp_path / "private-executable-secret-nonexec"
    non_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    windows_target = tmp_path / "private-executable-secret.exe"
    windows_target.write_text("#!/bin/sh\n", encoding="utf-8")
    windows_target.chmod(0o755)
    windows_alias = tmp_path / "private-executable-secret-alias"
    windows_alias.symlink_to(windows_target)
    cases = (
        (r"C:\private-executable-secret\tool.exe", "Linux path"),
        ("private-executable-secret-relative", "absolute Linux path"),
        (str(tmp_path / "private-executable-secret-missing"), "not found"),
        (str(tmp_path / "private-executable-secret.exe"), "Windows executable"),
        (str(directory), "not a file"),
        (str(non_executable), "not executable"),
        (str(windows_alias), "must resolve to a Linux ORCA binary"),
    )

    for raw_path, category in cases:
        with pytest.raises(ValueError) as captured:
            validate_configured_executable_path(
                raw_path,
                label="orca.paths.orca_executable",
                display_name="ORCA",
            )

        message = str(captured.value)
        assert "orca.paths.orca_executable" in message
        assert category in message
        assert "private-executable-secret" not in message


def test_configured_executable_validation_redacts_raced_resolution_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "private-raced-resolution-secret"

    def fail_raced_resolution(*_args: object, **_kwargs: object) -> Path:
        raise ValueError(f"symlink race exposed {candidate}")

    monkeypatch.setattr(
        "orca_auto.core.paths.validation.validate_executable_file",
        fail_raced_resolution,
    )

    with pytest.raises(ValueError) as captured:
        validate_configured_executable_path(
            candidate,
            label="orca.paths.orca_executable",
            display_name="ORCA",
        )

    message = str(captured.value)
    assert message == "orca.paths.orca_executable must resolve to a valid Linux ORCA binary."
    assert "private-raced-resolution-secret" not in message


def test_is_subpath(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    child = root / "nested" / "job"
    child.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    assert is_subpath(child, root)
    assert not is_subpath(outside, root)
