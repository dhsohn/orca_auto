from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.flow import engine_runtime


def test_engine_runtime_paths_reads_top_level_runs_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    assert engine_runtime.engine_runtime_paths(str(config_path)) == {
        "workflow_root": runs_root.resolve(),
        "allowed_root": runs_root.resolve(),
        "admission_root": runs_root.resolve() / ".admission",
    }


def test_engine_runtime_paths_requires_runs_root(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("scheduler:\n  max_active_simulations: 4\n", encoding="utf-8")

    for engine in (None, "orca", "xtb", "crest"):
        with pytest.raises(ValueError, match="Missing runs_root"):
            engine_runtime.engine_runtime_paths(str(config_path), engine=engine)


def test_engine_runtime_paths_rejects_invalid_runs_root_before_resolving(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"

    config_path.write_text("runs_root: './runs'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="absolute Linux path"):
        engine_runtime.engine_runtime_paths(str(config_path), engine="orca")

    config_path.write_text("runs_root: '/mnt/c/runs'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Linux path"):
        engine_runtime.engine_runtime_paths(str(config_path), engine="xtb")


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
        engine_runtime.engine_runtime_paths(str(config_path), engine="xtb")


def test_engine_runtime_paths_all_engines_share_the_runs_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    expected = {
        "workflow_root": runs_root.resolve(),
        "allowed_root": runs_root.resolve(),
        "admission_root": runs_root.resolve() / ".admission",
    }
    for engine in ("orca", "xtb", "crest"):
        assert engine_runtime.engine_runtime_paths(str(config_path), engine=engine) == expected


def test_engine_runtime_paths_uses_scheduler_admission_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    admission_root = tmp_path / "admission"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {runs_root}",
                "scheduler:",
                "  max_active_simulations: 4",
                f"  admission_root: {admission_root}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    for engine in (None, "orca", "xtb", "crest"):
        paths = engine_runtime.engine_runtime_paths(str(config_path), engine=engine)
        assert paths["allowed_root"] == runs_root.resolve()
        assert paths["admission_root"] == admission_root.resolve()


def test_engine_runtime_paths_rejects_engine_scoped_scheduler_override(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    shared_admission = tmp_path / "shared-admission"
    orca_admission = tmp_path / "orca-admission"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {runs_root}",
                "scheduler:",
                f"  admission_root: {shared_admission}",
                "orca:",
                "  scheduler:",
                f"    admission_root: {orca_admission}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    for engine in (None, "orca", "xtb", "crest"):
        with pytest.raises(ValueError, match="Unknown orca config fields are not supported"):
            engine_runtime.engine_runtime_paths(str(config_path), engine=engine)
