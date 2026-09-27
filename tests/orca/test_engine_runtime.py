from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca import engine_runtime


def test_engine_runtime_paths_reads_top_level_runs_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    assert engine_runtime.engine_runtime_paths(str(config_path)) == {
        "allowed_root": runs_root.resolve(),
        "admission_root": runs_root.resolve() / ".admission",
    }


def test_engine_runtime_paths_requires_runs_root(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("scheduler:\n  max_active_simulations: 4\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Missing runs_root"):
        engine_runtime.engine_runtime_paths(str(config_path))


def test_engine_runtime_paths_rejects_invalid_runs_root_before_resolving(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"

    config_path.write_text("runs_root: './runs'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="absolute Linux path"):
        engine_runtime.engine_runtime_paths(str(config_path))

    config_path.write_text("runs_root: '/mnt/c/runs'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Linux path"):
        engine_runtime.engine_runtime_paths(str(config_path))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            "runs_root: /tmp/runs\nschedulr: {}\n",
            "Unknown top-level config fields are not supported",
        ),
        (
            "runs_root: /tmp/runs\nscheduler: []\n",
            "scheduler section must be a mapping",
        ),
        (
            "runs_root: /tmp/runs\nmessenger:\n  discord:\n    default_channel_id:\n",
            "messenger.discord.default_channel_id",
        ),
    ],
)
def test_engine_runtime_paths_validates_complete_shared_config(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        engine_runtime.engine_runtime_paths(str(config_path))


def test_engine_runtime_paths_rejects_the_removed_admission_root(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"runs_root: {tmp_path / 'runs'}\nscheduler:\n  admission_root: {tmp_path / 'pool'}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"scheduler\.admission_root was removed"):
        engine_runtime.engine_runtime_paths(str(config_path))


def test_engine_runtime_paths_rejects_engine_scoped_scheduler_override(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {runs_root}",
                "orca:",
                "  scheduler:",
                "    max_active_simulations: 2",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unknown orca config fields are not supported"):
        engine_runtime.engine_runtime_paths(str(config_path))
