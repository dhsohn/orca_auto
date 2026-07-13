from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from orca_auto.xtb_md import artifacts as artifacts_module
from orca_auto.xtb_md.artifacts import (
    XtbMdArtifactError,
    capture_attempt_identity,
    validate_terminal_artifacts,
)


def _trajectory(frames: int = 2, *, symbol: str = "O", coordinate: str = "0.0") -> str:
    frame = (
        "3\n"
        "energy: -5.0 gnorm: 0.1\n"
        f"{symbol} {coordinate} 0.0 0.0\n"
        "H 0.75 0.0 0.5\n"
        "H -0.75 0.0 0.5\n"
    )
    return frame * frames


def _checkpoint(rows: int = 3, *, value: str = "0.10000000000000D+00") -> str:
    return "-1.0\n" + (f"{value} 0.0 0.0 0.0 0.0 0.0\n" * rows)


def _write_success_outputs(
    attempt_dir: Path,
    *,
    stdout: str | None = None,
    stderr: str = "normal termination of xtb\n",
    trajectory: str | None = None,
    checkpoint: str | None = None,
) -> tuple[Path, Path]:
    stdout_path = attempt_dir / "stdout.log"
    stderr_path = attempt_dir / "stderr.log"
    stdout_path.write_text(
        stdout or ("max steps          :     2\nEpot (accurate SCC): NaN\nnormal exit of md()\n"),
        encoding="utf-8",
    )
    stderr_path.write_text(stderr, encoding="utf-8")
    (attempt_dir / "xtb.trj").write_text(
        trajectory if trajectory is not None else _trajectory(), encoding="utf-8"
    )
    (attempt_dir / "mdrestart").write_text(
        checkpoint if checkpoint is not None else _checkpoint(), encoding="utf-8"
    )
    (attempt_dir / "xtbmdok").touch()
    return stdout_path, stderr_path


def _validate(attempt, manifest, stdout: Path, stderr: Path, **kwargs):
    return validate_terminal_artifacts(
        attempt,
        manifest=manifest,
        exit_code=kwargs.pop("exit_code", 0),
        stdout_log=stdout,
        stderr_log=stderr,
        max_log_bytes=kwargs.pop("max_log_bytes", 100_000),
        max_trajectory_bytes=kwargs.pop("max_trajectory_bytes", 100_000),
        max_checkpoint_bytes=kwargs.pop("max_checkpoint_bytes", 100_000),
        **kwargs,
    )


def test_terminal_validation_accepts_fresh_finite_complete_artifacts_and_contextual_nan(
    tmp_path: Path,
    manifest_job,
) -> None:
    _job_dir, manifest = manifest_job()
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    attempt = capture_attempt_identity(attempt_dir)
    stdout, stderr = _write_success_outputs(attempt_dir)

    result = _validate(attempt, manifest, stdout, stderr)

    assert result.frame_count == 2
    assert result.completed_steps == 2
    assert result.atom_count == 3
    assert result.trajectory.sha256
    assert result.checkpoint.sha256
    assert result.success_marker.size_bytes == 0
    assert set(result.output_identities) == {
        "trajectory",
        "checkpoint",
        "success_marker",
        "stdout_log",
        "stderr_log",
    }


@pytest.mark.parametrize(
    "fatal_marker",
    [
        "MD is unstable, emergency exit",
        "but still taking it as converged!",
        "thermostating problem",
        "abnormal termination of xtb",
    ],
)
def test_terminal_validation_rejects_explicit_false_success_markers(
    tmp_path: Path,
    manifest_job,
    fatal_marker: str,
) -> None:
    _job_dir, manifest = manifest_job()
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    attempt = capture_attempt_identity(attempt_dir)
    stdout, stderr = _write_success_outputs(
        attempt_dir,
        stdout=(f"max steps : 2\n{fatal_marker}\nnormal exit of md()\n"),
    )

    with pytest.raises(XtbMdArtifactError, match="fatal marker"):
        _validate(attempt, manifest, stdout, stderr)


@pytest.mark.parametrize(
    ("stdout_text", "stderr_text", "message"),
    [
        ("max steps : 2\n", "normal termination of xtb\n", "MD normal-exit"),
        ("max steps : 2\nnormal exit of md()\n", "", "global normal-termination"),
        ("max steps : 1\nnormal exit of md()\n", "normal termination of xtb\n", "requested"),
        (
            "max steps : 2\nmax steps : 2\nnormal exit of md()\n",
            "normal termination of xtb\n",
            "requested",
        ),
    ],
)
def test_terminal_validation_requires_unambiguous_completion_log(
    tmp_path: Path,
    manifest_job,
    stdout_text: str,
    stderr_text: str,
    message: str,
) -> None:
    _job_dir, manifest = manifest_job()
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    attempt = capture_attempt_identity(attempt_dir)
    stdout, stderr = _write_success_outputs(attempt_dir, stdout=stdout_text, stderr=stderr_text)

    with pytest.raises(XtbMdArtifactError, match=message):
        _validate(attempt, manifest, stdout, stderr)


def test_terminal_validation_rejects_nonzero_exit_even_with_artifacts(
    tmp_path: Path,
    manifest_job,
) -> None:
    _job_dir, manifest = manifest_job()
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    attempt = capture_attempt_identity(attempt_dir)
    stdout, stderr = _write_success_outputs(attempt_dir)

    with pytest.raises(XtbMdArtifactError, match="exited with code 7"):
        _validate(attempt, manifest, stdout, stderr, exit_code=7)


@pytest.mark.parametrize(
    ("trajectory", "message"),
    [
        (_trajectory(frames=1), "frame count"),
        (_trajectory(symbol="C"), "atom order"),
        (_trajectory(coordinate="nan"), "non-finite"),
        (_trajectory(coordinate="1e308"), "magnitude limit"),
        (_trajectory().replace("O 0.0 0.0 0.0", "O 0.0 0.0 0.0 NaN"), "exactly four"),
        (_trajectory()[:-10], "truncated"),
    ],
)
def test_terminal_validation_rejects_invalid_or_incomplete_trajectory(
    tmp_path: Path,
    manifest_job,
    trajectory: str,
    message: str,
) -> None:
    _job_dir, manifest = manifest_job()
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    attempt = capture_attempt_identity(attempt_dir)
    stdout, stderr = _write_success_outputs(attempt_dir, trajectory=trajectory)

    with pytest.raises(XtbMdArtifactError, match=message):
        _validate(attempt, manifest, stdout, stderr)


@pytest.mark.parametrize(
    ("checkpoint", "message"),
    [
        (_checkpoint(rows=2), "atom count"),
        (_checkpoint(value="NaN"), "non-finite"),
        (_checkpoint(value="1e308"), "magnitude limit"),
        (_checkpoint(value="1_0"), "invalid number"),
        ("0.0\n" + _checkpoint().split("\n", 1)[1], "format marker"),
        ("-1.0\n1 2 3\n", "six values"),
    ],
)
def test_terminal_validation_rejects_invalid_checkpoint(
    tmp_path: Path,
    manifest_job,
    checkpoint: str,
    message: str,
) -> None:
    _job_dir, manifest = manifest_job()
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    attempt = capture_attempt_identity(attempt_dir)
    stdout, stderr = _write_success_outputs(attempt_dir, checkpoint=checkpoint)

    with pytest.raises(XtbMdArtifactError, match=message):
        _validate(attempt, manifest, stdout, stderr)


def test_terminal_validation_rejects_nonempty_marker(
    tmp_path: Path,
    manifest_job,
) -> None:
    _job_dir, manifest = manifest_job()
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    attempt = capture_attempt_identity(attempt_dir)
    stdout, stderr = _write_success_outputs(attempt_dir)
    marker = attempt_dir / "xtbmdok"
    marker.write_text("success", encoding="utf-8")
    with pytest.raises(XtbMdArtifactError, match="byte limit|fresh empty"):
        _validate(attempt, manifest, stdout, stderr)


def test_attempt_capture_rejects_stale_outputs_and_replaced_directory(tmp_path: Path) -> None:
    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    (stale_dir / "xtbmdok").touch()
    with pytest.raises(XtbMdArtifactError, match="stale canonical output"):
        capture_attempt_identity(stale_dir)

    attempt_dir = tmp_path / "replace"
    attempt_dir.mkdir()
    attempt = capture_attempt_identity(attempt_dir)
    shutil.rmtree(attempt_dir)
    attempt_dir.mkdir()
    stdout, stderr = _write_success_outputs(attempt_dir)
    with pytest.raises(XtbMdArtifactError, match="identity changed"):
        _validate(attempt, object(), stdout, stderr)


def test_terminal_validation_rejects_outputs_renamed_from_pre_attempt_files(
    tmp_path: Path,
    manifest_job,
) -> None:
    _job_dir, manifest = manifest_job()
    attempt_dir = tmp_path / "renamed"
    attempt_dir.mkdir()
    old_outputs = {
        "old-stdout.log": "max steps : 2\nnormal exit of md()\n",
        "old-stderr.log": "normal termination of xtb\n",
        "old.trj": _trajectory(),
        "old.restart": _checkpoint(),
        "old.ok": "",
    }
    for name, payload in old_outputs.items():
        (attempt_dir / name).write_text(payload, encoding="utf-8")
    attempt = capture_attempt_identity(attempt_dir)
    renames = {
        "old-stdout.log": "stdout.log",
        "old-stderr.log": "stderr.log",
        "old.trj": "xtb.trj",
        "old.restart": "mdrestart",
        "old.ok": "xtbmdok",
    }
    for source, destination in renames.items():
        (attempt_dir / source).rename(attempt_dir / destination)

    with pytest.raises(XtbMdArtifactError, match="renamed from a pre-attempt file"):
        _validate(
            attempt,
            manifest,
            attempt_dir / "stdout.log",
            attempt_dir / "stderr.log",
        )


def test_terminal_validation_rejects_symlink_hardlink_and_byte_overflow(
    tmp_path: Path,
    manifest_job,
) -> None:
    _job_dir, manifest = manifest_job()

    symlink_dir = tmp_path / "symlink"
    symlink_dir.mkdir()
    symlink_attempt = capture_attempt_identity(symlink_dir)
    stdout, stderr = _write_success_outputs(symlink_dir)
    outside = tmp_path / "outside.trj"
    outside.write_text(_trajectory(), encoding="utf-8")
    (symlink_dir / "xtb.trj").unlink()
    (symlink_dir / "xtb.trj").symlink_to(outside)
    with pytest.raises(XtbMdArtifactError, match="direct regular file"):
        _validate(symlink_attempt, manifest, stdout, stderr)

    hardlink_dir = tmp_path / "hardlink"
    hardlink_dir.mkdir()
    hardlink_attempt = capture_attempt_identity(hardlink_dir)
    stdout, stderr = _write_success_outputs(hardlink_dir)
    (tmp_path / "checkpoint-link").hardlink_to(hardlink_dir / "mdrestart")
    with pytest.raises(XtbMdArtifactError, match="single-link"):
        _validate(hardlink_attempt, manifest, stdout, stderr)

    overflow_dir = tmp_path / "overflow"
    overflow_dir.mkdir()
    overflow_attempt = capture_attempt_identity(overflow_dir)
    stdout, stderr = _write_success_outputs(overflow_dir)
    with pytest.raises(XtbMdArtifactError, match="byte limit"):
        _validate(
            overflow_attempt,
            manifest,
            stdout,
            stderr,
            max_trajectory_bytes=10,
        )


def test_streaming_trajectory_validation_caps_individual_lines(
    tmp_path: Path,
    manifest_job,
) -> None:
    _job_dir, manifest = manifest_job()
    attempt_dir = tmp_path / "long-line"
    attempt_dir.mkdir()
    attempt = capture_attempt_identity(attempt_dir)
    trajectory = _trajectory().replace(
        "energy: -5.0 gnorm: 0.1",
        "x" * (1024 * 1024 + 1),
        1,
    )
    stdout, stderr = _write_success_outputs(attempt_dir, trajectory=trajectory)

    with pytest.raises(XtbMdArtifactError, match="overlong text line"):
        _validate(
            attempt,
            manifest,
            stdout,
            stderr,
            max_trajectory_bytes=2 * 1024 * 1024,
        )


def test_terminal_validation_rejects_artifact_fsync_failure(
    tmp_path: Path,
    manifest_job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _job_dir, manifest = manifest_job()
    attempt_dir = tmp_path / "file-fsync"
    attempt_dir.mkdir()
    attempt = capture_attempt_identity(attempt_dir)
    stdout, stderr = _write_success_outputs(attempt_dir)

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("simulated artifact fsync failure")

    monkeypatch.setattr(artifacts_module.os, "fsync", fail_fsync)
    with pytest.raises(XtbMdArtifactError, match="artifact could not be durably synchronized"):
        _validate(attempt, manifest, stdout, stderr)


def test_terminal_validation_rejects_attempt_directory_fsync_failure(
    tmp_path: Path,
    manifest_job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _job_dir, manifest = manifest_job()
    attempt_dir = tmp_path / "directory-fsync"
    attempt_dir.mkdir()
    attempt = capture_attempt_identity(attempt_dir)
    stdout, stderr = _write_success_outputs(attempt_dir)

    def fail_fsync_directory(_path: str | Path) -> None:
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(artifacts_module, "fsync_directory", fail_fsync_directory)
    with pytest.raises(
        XtbMdArtifactError,
        match="attempt directory could not be durably synchronized",
    ):
        _validate(attempt, manifest, stdout, stderr)
