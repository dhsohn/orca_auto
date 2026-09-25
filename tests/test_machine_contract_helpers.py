from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests import machine_contract_helpers
from tests.machine_contract_helpers import validate_common_machine


def _refusal(path: Path) -> str:
    # BaseException, so a helper that skipped instead of failing is caught too.
    with pytest.raises(BaseException) as refusal:
        validate_common_machine(path)
    assert refusal.type is pytest.fail.Exception
    return str(refusal.value)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_machine_contract_validation_rejects_a_nonconforming_observation(tmp_path: Path) -> None:
    machine = tmp_path / "machine.json"
    machine.write_text('{"delivery": "complete"}\n', encoding="utf-8")

    assert "machine-observation-v1.schema.json" in _refusal(machine)


def test_machine_contract_validation_fails_without_the_pinned_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_MACHINE_CONTRACT_REPO", str(tmp_path / "missing"))
    machine = tmp_path / "machine.json"
    machine.write_text('{"delivery": "complete"}\n', encoding="utf-8")

    assert "cannot provide CI pin" in _refusal(machine)


def test_machine_contract_validation_uses_the_pinned_commit_not_the_clone_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "machine_contracts"
    validator = repo / "scripts" / "validate.py"
    validator.parent.mkdir(parents=True)
    _git(tmp_path, "init", "--quiet", str(repo))
    validator.write_text("raise SystemExit('pinned validator ran')\n", encoding="utf-8")
    _git(repo, "add", "scripts/validate.py")
    _git(repo, "commit", "--quiet", "-m", "pinned")
    pinned = _git(repo, "rev-parse", "HEAD")
    validator.write_text("raise SystemExit(0)\n", encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", "later")
    validator.write_text("raise SystemExit(0)  # uncommitted\n", encoding="utf-8")
    monkeypatch.setenv("FACTORY_MACHINE_CONTRACT_REPO", str(repo))
    monkeypatch.setattr(machine_contract_helpers, "machine_contract_pin", lambda: pinned)
    machine = tmp_path / "machine.json"
    machine.write_text("{}\n", encoding="utf-8")

    assert "pinned validator ran" in _refusal(machine)


def test_machine_contract_validation_fails_without_jsonschema(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(machine_contract_helpers.importlib.util, "find_spec", lambda _name: None)
    assert "cannot import jsonschema" in _refusal(tmp_path / "machine.json")
