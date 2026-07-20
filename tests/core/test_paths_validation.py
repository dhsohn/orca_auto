from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core.paths.validation import (
    ensure_directory,
    is_rejected_windows_path,
    is_subpath,
    require_subpath,
    resolve_artifact_path,
    resolve_local_path,
    validate_configured_executable_path,
    validate_job_dir,
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
    with pytest.raises(ValueError, match="Windows-style and /mnt/<drive> paths are not supported"):
        resolve_local_path(path_text)


@pytest.mark.parametrize("path_text", ["", "   "])
def test_empty_path_rejection(path_text: str) -> None:
    with pytest.raises(ValueError, match="Path must not be empty"):
        resolve_local_path(path_text)


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


def test_ensure_directory_success(tmp_path: Path) -> None:
    directory = tmp_path / "input"
    directory.mkdir()

    assert ensure_directory(str(directory), label="Input dir") == directory.resolve()


def test_ensure_directory_failure_for_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match=r"Input dir not found: .*missing"):
        ensure_directory(str(missing), label="Input dir")


def test_ensure_directory_failure_for_file(tmp_path: Path) -> None:
    file_path = tmp_path / "artifact.txt"
    file_path.write_text("payload", encoding="utf-8")

    with pytest.raises(ValueError, match=r"Input dir is not a directory: .*artifact\.txt"):
        ensure_directory(str(file_path), label="Input dir")


def test_is_subpath_and_require_subpath(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    child = root / "nested" / "job"
    child.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    assert is_subpath(child, root)
    assert not is_subpath(outside, root)
    assert require_subpath(child, root, label="Job dir") == child.resolve()

    with pytest.raises(ValueError, match=r"Job dir must be under allowed root: .*got=.*elsewhere"):
        require_subpath(outside, root, label="Job dir")


def test_validate_job_dir(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    job_dir = allowed_root / "job-1"
    job_dir.mkdir()

    assert validate_job_dir(str(job_dir), str(allowed_root)) == job_dir.resolve()


def test_validate_job_dir_rejects_outside_root(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    job_dir = tmp_path / "job-1"
    job_dir.mkdir()

    with pytest.raises(
        ValueError, match=r"Job directory must be under allowed root: .*got=.*job-1"
    ):
        validate_job_dir(str(job_dir), str(allowed_root))


def test_resolve_artifact_path_relative_and_absolute_and_missing(tmp_path: Path) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    relative_dir = base_dir / "runs" / "run-1"
    relative_dir.mkdir(parents=True)
    basename_dir = base_dir / "artifacts"
    basename_dir.mkdir()
    absolute_dir = tmp_path / "absolute-artifact"
    absolute_dir.mkdir()

    relative_candidate = relative_dir / "result.json"
    relative_candidate.write_text("relative", encoding="utf-8")
    basename_candidate = basename_dir / "output.json"
    basename_candidate.write_text("basename", encoding="utf-8")
    absolute_candidate = absolute_dir / "summary.json"
    absolute_candidate.write_text("absolute", encoding="utf-8")

    assert resolve_artifact_path("runs/run-1/result.json", base_dir) == relative_candidate.resolve()
    assert (
        resolve_artifact_path("nested/path/output.json", basename_dir)
        == basename_candidate.resolve()
    )
    assert resolve_artifact_path(str(absolute_candidate), base_dir) == absolute_candidate.resolve()
    assert resolve_artifact_path("missing.json", base_dir) is None
    assert resolve_artifact_path("   ", base_dir) is None


def test_resolve_artifact_path_skips_oserror_and_finds_later_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    nested_dir = base_dir / "runs" / "run-1"
    nested_dir.mkdir(parents=True)

    first_candidate = nested_dir / "result.json"
    second_candidate = base_dir / "result.json"
    second_candidate.write_text("payload", encoding="utf-8")

    original_resolve = Path.resolve

    def fake_resolve(self: Path, *args, **kwargs) -> Path:
        if self == first_candidate:
            raise OSError("cannot resolve candidate")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve, raising=True)

    assert resolve_artifact_path("runs/run-1/result.json", base_dir) == second_candidate.resolve()
