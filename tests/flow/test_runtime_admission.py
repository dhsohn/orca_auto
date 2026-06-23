from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.admission import AdmissionStoreCorruptError
from orca_auto.flow import runtime_admission


def test_submission_admission_limit_returns_none_for_unreadable_config(tmp_path: Path) -> None:
    assert (
        runtime_admission.submission_admission_limit_from_config(tmp_path / "missing.yaml") is None
    )


def test_submission_admission_has_capacity_fails_closed_for_unreadable_config(
    tmp_path: Path,
) -> None:
    assert runtime_admission.submission_admission_has_capacity(tmp_path / "missing.yaml") is False


@pytest.mark.parametrize("raw_limit", ["not-an-int", "true"])
def test_submission_admission_has_capacity_fails_closed_for_invalid_configured_limit(
    tmp_path: Path,
    raw_limit: str,
) -> None:
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        f"scheduler:\n  max_active_simulations: {raw_limit}\n",
        encoding="utf-8",
    )

    assert runtime_admission.submission_admission_has_capacity(config) is False


def test_submission_admission_root_for_internal_engine_requires_workflow_root(
    tmp_path: Path,
) -> None:
    config = tmp_path / "orca_auto.yaml"
    config.write_text("scheduler:\n  max_active_simulations: 2\n", encoding="utf-8")

    assert runtime_admission._submission_admission_root_from_config(config, engine="xtb") is None


def test_submission_admission_root_for_internal_engine_uses_scheduler_default(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflows"
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        f"workflow:\n  root: {workflow_root}\nscheduler:\n  max_active_simulations: 2\n",
        encoding="utf-8",
    )

    root = runtime_admission._submission_admission_root_from_config(config, engine="crest")

    assert root == (tmp_path / "admission").resolve()


def test_submission_admission_root_for_orca_ignores_runtime_admission_setting(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime-admission"
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        "\n".join(
            [
                "orca:",
                "  runtime:",
                "    allowed_root: /tmp/runs",
                f"    admission_root: {runtime_root}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    root = runtime_admission._submission_admission_root_from_config(config, engine="orca")

    assert root is None


def test_submission_admission_root_for_orca_uses_scheduler_with_runtime_key_present(
    tmp_path: Path,
) -> None:
    scheduler_root = tmp_path / "scheduler-admission"
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        "\n".join(
            [
                "scheduler:",
                f"  admission_root: {scheduler_root}",
                "orca:",
                "  runtime:",
                "    allowed_root: /tmp/runs",
                "    admission_root: /tmp/runtime-admission",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert (
        runtime_admission._submission_admission_root_from_config(config, engine="orca")
        == scheduler_root.resolve()
    )


def test_submission_admission_limit_ignores_orca_runtime_scheduler_keys(tmp_path: Path) -> None:
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        "\n".join(
            [
                "orca:",
                "  runtime:",
                "    allowed_root: /tmp/runs",
                "    max_concurrent: 6",
                "    admission_limit: 3",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert runtime_admission.submission_admission_limit_from_config(config) is None


def test_submission_admission_has_capacity_uses_first_resolved_engine_root(
    tmp_path: Path,
) -> None:
    admission_root = tmp_path / "admission"
    config = tmp_path / "orca_auto.yaml"
    config.write_text("scheduler:\n  max_active_simulations: 2\n", encoding="utf-8")
    calls: list[str | None] = []

    def engine_runtime_paths(config_path: str, *, engine: str | None = None) -> dict[str, Any]:
        assert config_path == str(config)
        calls.append(engine)
        if engine is None:
            raise ValueError("no root")
        return {"admission_root": admission_root}

    assert (
        runtime_admission.submission_admission_has_capacity(
            config,
            active_slot_count_fn=lambda root: 1 if root == admission_root else 99,
            engine_runtime_paths_fn=engine_runtime_paths,
        )
        is True
    )
    assert calls == [None, "xtb"]


def test_submission_admission_has_capacity_fails_closed_when_configured_root_lookup_fails(
    tmp_path: Path,
) -> None:
    config = tmp_path / "orca_auto.yaml"
    config.write_text("scheduler:\n  max_active_simulations: 2\n", encoding="utf-8")

    def broken_engine_runtime_paths(
        _config_path: str,
        *,
        engine: str | None = None,
    ) -> dict[str, Any]:
        raise ValueError(f"cannot resolve {engine or 'shared'} admission root")

    assert (
        runtime_admission.submission_admission_has_capacity(
            config,
            engine_runtime_paths_fn=broken_engine_runtime_paths,
        )
        is False
    )


@pytest.mark.parametrize(
    "exc",
    [OSError("cannot inspect slots"), AdmissionStoreCorruptError("corrupt slots")],
)
def test_submission_admission_has_capacity_returns_false_when_slot_count_fails(
    tmp_path: Path,
    exc: Exception,
) -> None:
    admission_root = tmp_path / "admission"
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        f"scheduler:\n  max_active_simulations: 2\n  admission_root: {admission_root}\n",
        encoding="utf-8",
    )

    def broken_slot_count(_root: Path) -> int:
        raise exc

    assert (
        runtime_admission.submission_admission_has_capacity(
            config,
            active_slot_count_fn=broken_slot_count,
        )
        is False
    )


def test_workflow_submission_has_capacity_uses_first_informative_config() -> None:
    calls: list[str] = []

    def has_capacity(config_path: str | Path) -> bool | None:
        calls.append(str(config_path))
        return None if len(calls) == 1 else False

    assert (
        runtime_admission.workflow_submission_has_capacity(
            "",
            "/tmp/first.yaml",
            "/tmp/second.yaml",
            submission_admission_has_capacity_fn=has_capacity,
        )
        is False
    )
    assert calls == ["/tmp/first.yaml", "/tmp/second.yaml"]


def test_workflow_submission_has_capacity_allows_unconfigured_capacity_checks() -> None:
    calls: list[str] = []

    def has_capacity(config_path: str | Path) -> bool | None:
        calls.append(str(config_path))
        return None

    assert (
        runtime_admission.workflow_submission_has_capacity(
            "",
            "/tmp/unknown.yaml",
            submission_admission_has_capacity_fn=has_capacity,
        )
        is True
    )
    assert calls == ["/tmp/unknown.yaml"]


def test_workflow_submission_has_capacity_allows_missing_preflight_configs() -> None:
    assert runtime_admission.workflow_submission_has_capacity("", None) is True
