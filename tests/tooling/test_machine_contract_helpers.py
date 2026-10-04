from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.machine_contract_helpers import validate_common_machine

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _refusal(path: Path) -> str:
    # BaseException, so a helper that skipped instead of failing is caught too.
    with pytest.raises(BaseException) as refusal:
        validate_common_machine(path)
    assert refusal.type is pytest.fail.Exception
    return str(refusal.value)


def test_machine_contract_validation_rejects_a_nonconforming_observation(tmp_path: Path) -> None:
    machine = tmp_path / "machine.json"
    machine.write_text('{"delivery": "complete"}\n', encoding="utf-8")

    assert "machine-observation-v1.schema.json" in _refusal(machine)


def test_machine_contract_validation_never_uses_a_clone_or_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A clone whose validator accepts everything must not be consulted.
    clone = tmp_path / "machine_contracts"
    (clone / "scripts").mkdir(parents=True)
    (clone / "scripts" / "validate.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    monkeypatch.setenv("FACTORY_MACHINE_CONTRACT_REPO", str(clone))

    def no_subprocess(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("machine contract validation started a subprocess")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    machine = tmp_path / "machine.json"
    machine.write_text('{"delivery": "complete"}\n', encoding="utf-8")

    assert "machine-observation-v1.schema.json" in _refusal(machine)


def test_machine_contract_validation_fails_on_an_unreadable_machine(tmp_path: Path) -> None:
    assert "cannot read" in _refusal(tmp_path / "machine.json")


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_default_workflow_needs_no_machine_contracts_checkout(name: str) -> None:
    text = (_WORKFLOWS / name).read_text(encoding="utf-8")
    # BaseLoader keeps YAML's `on` key instead of coercing it to a boolean.
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    environments = [workflow.get("env", {})]
    for job in workflow["jobs"].values():
        environments.append(job.get("env", {}))
        for step in job["steps"]:
            environments.append(step.get("env", {}))
            assert "machine-contracts" not in step.get("with", {}).get("repository", ""), step
            assert "machine-contracts" not in step.get("run", ""), step
    assert not [env for env in environments if "FACTORY_MACHINE_CONTRACT_REPO" in env]
    assert "FACTORY_MACHINE_CONTRACT_REPO" not in text
    assert "dhsohn/machine-contracts" not in text


def test_machine_contract_validation_fails_without_jsonschema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    machine = tmp_path / "machine.json"
    machine.write_text("{}\n", encoding="utf-8")

    assert "cannot import jsonschema" in _refusal(machine)
