from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR as CONFIG_ENV_VAR
from orca_auto.core.config.discovery import default_shared_config_path as default_config_path
from orca_auto.orca.execution import _emit
from orca_auto.orca.run_context import _validate_reaction_dir
from tests.conftest import make_app_cfg


@pytest.fixture
def allowed(tmp_path: Path) -> Path:
    root = tmp_path / "allowed"
    root.mkdir()
    return root


def test_validate_reaction_dir_under_allowed_root(allowed: Path) -> None:
    reaction = allowed / "r1"
    reaction.mkdir()
    cfg = make_app_cfg(allowed, orca_executable="/usr/bin/orca")

    assert _validate_reaction_dir(cfg, str(reaction)) == reaction.resolve()


def test_validate_reaction_dir_rejects_outside_allowed_root(tmp_path: Path, allowed: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    cfg = make_app_cfg(allowed, orca_executable="/usr/bin/orca")

    with pytest.raises(ValueError):
        _validate_reaction_dir(cfg, str(outside))


def test_validate_reaction_dir_requires_existing_directory(allowed: Path) -> None:
    cfg = make_app_cfg(allowed, orca_executable="/usr/bin/orca")

    with pytest.raises(ValueError):
        _validate_reaction_dir(cfg, str(allowed / "missing"))


def test_default_config_path_prefers_env_then_home_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    home_default = fake_home / "orca_auto" / "config" / "orca_auto.yaml"

    # The home default is the only implicit location, whether or not the
    # file exists yet; no checkout-relative path is probed.
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv(CONFIG_ENV_VAR, "")
    assert default_config_path() == str(home_default)

    monkeypatch.setenv(CONFIG_ENV_VAR, "/tmp/env.yaml")
    assert default_config_path() == "/tmp/env.yaml"


def test_emit_prints_only_known_keys(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "status": "completed",
        "reaction_dir": "/tmp/rxn",
        "selected_inp": "rxn.inp",
        "attempt_count": 2,
        "reason": "normal_termination",
        "report_json": "/tmp/report.json",
        "ignored": "value",
    }

    _emit(payload)

    output = capsys.readouterr().out
    assert "status: completed" in output
    assert "job_dir: /tmp/rxn" in output
    assert "report_json: /tmp/report.json" in output
    assert "ignored" not in output
