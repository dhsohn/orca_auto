from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from orca_auto.machine_contracts import (
    ContractError,
    validate_document,
    validate_machine_path,
    validate_path,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "machine_contracts"
# SHA-256 of the files at dhsohn/machine-contracts bc252035d01edddf1314e6641689c6d5cb88af92.
UPSTREAM_SHA256 = {
    "LICENSE": "25a57a182f456e94b38019bc404e069e86e28b3c4a0196e6498e4f5c7ae03382",
    "schemas/machine-observation-v1.schema.json": (
        "deda0ca05d46a68382b20a43601a1ddc2826d6f15a62b8b6273c672d39f8de1b"
    ),
    "schemas/payloads/chemistry-results-bundle-v1.schema.json": (
        "c03ec18fa42f7ca6370331310bbcd4f290ac0fe35622efd3a3f254e28a8a18fa"
    ),
}
UPSTREAM_FIXTURE_SHA256 = {
    "orca-completed.json": "d116b6d3a660be67172764dc782fbf20fe602367ba48385d9e89c15ddc9fbb33",
    "orca-uncertain.json": "ce5ac3f9a9b768bde3bec28ca298cecbca88ac3e60a42da8a458cb2f503b5c9f",
}
_MISSING_REQUIRED_REPORT = {
    "status": "missing",
    "required": True,
    "role": "human-report",
    "path": None,
    "media_type": "text/html",
    "bytes": None,
    "byte_sha256": None,
}


def _completed() -> dict[str, Any]:
    document = json.loads((FIXTURES / "orca-completed.json").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _machine(root: Path) -> Path:
    """The completed fixture published with real artifact bytes in ``root``."""
    root.mkdir()
    document = _completed()
    for artifact_id, receipt in document["artifacts"].items():
        content = f"{artifact_id} bytes\n".encode()
        (root / receipt["path"]).write_bytes(content)
        receipt["bytes"] = len(content)
        receipt["byte_sha256"] = hashlib.sha256(content).hexdigest()
    machine = root / "machine.json"
    machine.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return machine


@pytest.mark.parametrize("relative", sorted(UPSTREAM_SHA256))
def test_packaged_contract_files_are_the_pinned_upstream_bytes(relative: str) -> None:
    resource = files("orca_auto.machine_contracts")
    for part in relative.split("/"):
        resource = resource / part
    assert hashlib.sha256(resource.read_bytes()).hexdigest() == UPSTREAM_SHA256[relative]


@pytest.mark.parametrize("name", sorted(UPSTREAM_FIXTURE_SHA256))
def test_upstream_orca_fixtures_are_pinned_and_valid(name: str) -> None:
    fixture = FIXTURES / name
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == UPSTREAM_FIXTURE_SHA256[name]
    validate_path(fixture)


def test_checked_in_ready_observation_with_a_missing_required_receipt_is_rejected() -> None:
    with pytest.raises(
        ContractError, match="complete delivery contains an unavailable required artifact"
    ):
        validate_path(FIXTURES / "invalid-orca-ready-missing-required.json")


def _failed(document: dict[str, Any]) -> None:
    document["lifecycle"] = {"phase": "finished", "outcome": "failed", "codes": []}
    document["handoff"] = {"status": "blocked", "codes": []}
    document["delivery"] = {"status": "incomplete", "codes": []}
    document["artifacts"]["human-report"] = dict(_MISSING_REQUIRED_REPORT)


def _running(document: dict[str, Any]) -> None:
    document["lifecycle"] = {"phase": "running", "outcome": "pending", "codes": []}
    document["handoff"] = {"status": "pending", "codes": []}
    document["delivery"] = {"status": "pending", "codes": []}
    document["payload"] = None


def _blocked_without_payload(document: dict[str, Any]) -> None:
    document["lifecycle"] = {"phase": "finished", "outcome": "uncertain", "codes": []}
    document["handoff"] = {"status": "blocked", "codes": []}
    document["payload"] = None


@pytest.mark.parametrize(
    "mutate",
    [_failed, _running, _blocked_without_payload],
    ids=["failed-blocked-incomplete", "running-pending", "blocked-without-payload"],
)
def test_non_ready_observations_keep_their_meaning(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    document = _completed()
    mutate(document)
    validate_document(document)


def _set(path: tuple[str, ...], value: object) -> Callable[[dict[str, Any]], None]:
    def mutate(document: dict[str, Any]) -> None:
        target = document
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value

    return mutate


def _drop(path: tuple[str, ...]) -> Callable[[dict[str, Any]], None]:
    def mutate(document: dict[str, Any]) -> None:
        target = document
        for part in path[:-1]:
            target = target[part]
        del target[path[-1]]

    return mutate


def _complete_with_missing_required(document: dict[str, Any]) -> None:
    document["artifacts"]["human-report"] = dict(_MISSING_REQUIRED_REPORT)


def _incomplete_with_everything_available(document: dict[str, Any]) -> None:
    document["handoff"] = {"status": "blocked", "codes": []}
    document["delivery"] = {"status": "incomplete", "codes": []}
    document["artifacts"]["human-report"]["required"] = True


def _duplicate_lineage(document: dict[str, Any]) -> None:
    upstream = document["lineage"]["upstream"]
    upstream.append(copy.deepcopy(upstream[0]))


_ENVELOPE = "machine-observation-v1.schema.json"
_PAYLOAD = "chemistry-results-bundle-v1.schema.json"
_BAD_DOCUMENTS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("missing-envelope-field", _drop(("lineage",)), _ENVELOPE),
    ("extra-envelope-field", _set(("extra",), {}), _ENVELOPE),
    ("finished-pending-handoff", _set(("handoff", "status"), "pending"), _ENVELOPE),
    ("finished-pending-delivery", _set(("delivery", "status"), "pending"), _ENVELOPE),
    ("ready-but-failed", _set(("lifecycle", "outcome"), "failed"), _ENVELOPE),
    ("ready-without-payload", _set(("payload",), None), _ENVELOPE),
    ("absolute-artifact-path", _set(("artifacts", "human-report", "path"), "/etc/x"), _ENVELOPE),
    ("parent-artifact-path", _set(("artifacts", "human-report", "path"), "../x"), _ENVELOPE),
    (
        "complete-with-missing-required",
        _complete_with_missing_required,
        "complete delivery contains an unavailable required artifact",
    ),
    (
        "incomplete-with-everything-available",
        _incomplete_with_everything_available,
        "incomplete delivery needs an unavailable required artifact",
    ),
    ("duplicate-lineage", _duplicate_lineage, "lineage contains a duplicate upstream envelope"),
    (
        "other-producer",
        _set(("producer", "name"), "chemvas"),
        "producer and operation route is not registered",
    ),
    (
        "other-operation",
        _set(("operation", "kind"), "chemistry/elementary-step-export"),
        "producer and operation route is not registered",
    ),
    (
        "unregistered-payload-version",
        _set(("payload", "contract", "version"), 2),
        "payload contract is not registered",
    ),
    (
        "workflow-payload-on-orca-run",
        _set(("payload", "data", "result_kind"), "workflow"),
        "route requires result_kind='engine-run'",
    ),
    ("payload-without-engine", _drop(("payload", "data", "engine")), _PAYLOAD),
    (
        "unknown-artifact-reference",
        _set(("payload", "data", "artifact_refs"), ["human-report", "absent"]),
        "payload references unknown artifacts: absent",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [(mutate, message) for _, mutate, message in _BAD_DOCUMENTS],
    ids=[name for name, _, _ in _BAD_DOCUMENTS],
)
def test_nonconforming_observations_are_rejected(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    document = _completed()
    mutate(document)
    with pytest.raises(ContractError, match=re.escape(message)):
        validate_document(document)


def test_published_machine_with_matching_receipts_is_valid_and_untouched(tmp_path: Path) -> None:
    machine = _machine(tmp_path / "generation")
    before = {path.name: path.read_bytes() for path in machine.parent.iterdir()}
    validate_machine_path(machine)
    assert {path.name: path.read_bytes() for path in machine.parent.iterdir()} == before


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    machine = _machine(tmp_path / "generation")
    text = machine.read_text(encoding="utf-8")
    machine.write_text(
        text.replace('"lineage":', '"payload": null,\n  "lineage":', 1), encoding="utf-8"
    )
    with pytest.raises(ContractError, match="duplicate JSON key: payload"):
        validate_machine_path(machine)


def test_public_machine_basename_is_required(tmp_path: Path) -> None:
    machine = _machine(tmp_path / "generation")
    renamed = machine.rename(machine.with_name("observation.json"))
    with pytest.raises(ContractError, match="basename must be machine.json"):
        validate_machine_path(renamed)
    validate_path(renamed)


def _remove(path: Path) -> None:
    path.unlink()


def _append(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b"x")


def _flip(path: Path) -> None:
    data = path.read_bytes()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])


def _escape(path: Path) -> None:
    outside = path.parent.parent / "outside.html"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_remove, "artifact file is missing: human-report"),
        (_append, "artifact byte count mismatch: human-report"),
        (_flip, "artifact sha256 mismatch: human-report"),
        (_escape, "artifact path escapes the generation: job_report.html"),
    ],
    ids=["missing", "byte-count", "sha256", "symlink-escape"],
)
def test_available_receipts_must_match_confined_artifact_bytes(
    tmp_path: Path, change: Callable[[Path], None], message: str
) -> None:
    machine = _machine(tmp_path / "generation")
    change(machine.parent / "job_report.html")
    with pytest.raises(ContractError, match=message):
        validate_machine_path(machine)


def test_validation_fails_without_jsonschema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    with pytest.raises(ImportError, match="jsonschema"):
        validate_path(FIXTURES / "orca-completed.json")
