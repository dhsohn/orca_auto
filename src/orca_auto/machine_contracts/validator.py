"""ORCA_auto subset of the ``factory/machine-observation`` v1 validator.

Derived from ``scripts/validate.py`` of dhsohn/machine-contracts at
``bc252035d01edddf1314e6641689c6d5cb88af92`` (MIT, see ``LICENSE`` and
``PROVENANCE.md``). The checks and messages are the upstream ones; the registry
is reduced to the ``orca_auto`` routes and their payload contract.
"""

from __future__ import annotations

import hashlib
import json
import os
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import Any

UPSTREAM_COMMIT = "bc252035d01edddf1314e6641689c6d5cb88af92"
ENVELOPE_SCHEMA = "schemas/machine-observation-v1.schema.json"

# Verbatim entries of the upstream registry.json for producer orca_auto.
_PAYLOAD_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "name": "chemistry/results-bundle",
        "schema": "schemas/payloads/chemistry-results-bundle-v1.schema.json",
        "version": 1,
        "required_keys": [
            "result_kind",
            "engine",
            "summary",
            "results",
            "artifact_refs",
        ],
        "artifact_references": {
            "declared": {"path": ["artifact_refs"], "many": True},
            "nested": [],
        },
        "ready_requirements": [],
    },
)
_ROUTES: tuple[dict[str, Any], ...] = (
    {
        "producer": "orca_auto",
        "operation_kind": "chemistry/orca-run",
        "payload_contract": "chemistry/results-bundle",
        "payload_version": 1,
        "requirements": [{"path": ["result_kind"], "operator": "equals", "value": "engine-run"}],
    },
    {
        "producer": "orca_auto",
        "operation_kind": "chemistry/workflow",
        "payload_contract": "chemistry/results-bundle",
        "payload_version": 1,
        "requirements": [{"path": ["result_kind"], "operator": "equals", "value": "workflow"}],
    },
)


class ContractError(ValueError):
    pass


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_object(text: str, *, source: object) -> dict[str, Any]:
    payload = json.loads(text, object_pairs_hook=_object)
    if not isinstance(payload, dict):
        raise ContractError(f"{source}: expected a JSON object")
    return payload


def _load_json(path: Path) -> dict[str, Any]:
    return _parse_object(path.read_text(encoding="utf-8"), source=path)


def _resource(relative: str) -> Traversable:
    resource = files("orca_auto.machine_contracts")
    for part in relative.split("/"):
        resource = resource / part
    return resource


def _schema_validator() -> Any:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise ImportError(
            "orca_auto.machine_contracts needs jsonschema>=4.23,<5 to validate observations"
        ) from exc
    return Draft202012Validator


def _validate_schema(instance: dict[str, Any], schema_name: str) -> None:
    validator = _schema_validator()
    resource = _resource(schema_name)
    schema = _parse_object(resource.read_text(encoding="utf-8"), source=resource.name)
    errors = sorted(
        validator(schema).iter_errors(instance),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return
    first = errors[0]
    location = "/".join(str(part) for part in first.absolute_path) or "<root>"
    raise ContractError(f"{resource.name}:{location}: {first.message}")


def _value_at(data: Any, path: list[str], *, location: str) -> Any:
    current = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            joined = "/".join(path)
            raise ContractError(f"{location} references missing payload path: {joined}")
        current = current[part]
    return current


def _values_at(data: Any, path: list[str], *, location: str) -> list[Any]:
    if not path:
        return [data]
    part, *remaining = path
    if part == "*":
        if not isinstance(data, list):
            raise ContractError(f"{location} wildcard expects an array")
        values: list[Any] = []
        for item in data:
            values.extend(_values_at(item, remaining, location=location))
        return values
    if not isinstance(data, dict) or part not in data:
        joined = "/".join(path)
        raise ContractError(f"{location} references missing payload path: {joined}")
    return _values_at(data[part], remaining, location=location)


def _check_requirements(
    data: dict[str, Any], requirements: list[dict[str, Any]], *, location: str
) -> None:
    for requirement in requirements:
        path = [str(part) for part in requirement["path"]]
        value = _value_at(data, path, location=location)
        operator = requirement["operator"]
        if operator == "equals" and value != requirement.get("value"):
            joined = "/".join(path)
            raise ContractError(f"{location} requires {joined}={requirement.get('value')!r}")
        if operator == "not_null" and value is None:
            joined = "/".join(path)
            raise ContractError(f"{location} requires non-null {joined}")


def _reference_values(
    data: dict[str, Any], descriptor: dict[str, Any], *, location: str
) -> set[str]:
    values = _values_at(
        data,
        [str(part) for part in descriptor["path"]],
        location=location,
    )
    references: list[Any] = []
    if descriptor["many"]:
        for value in values:
            if not isinstance(value, list):
                raise ContractError(f"{location} expects an artifact id array")
            references.extend(value)
    else:
        references.extend(values)
    if any(not isinstance(item, str) or not item for item in references):
        raise ContractError(f"{location} contains an invalid artifact id")
    return set(references)


def _validate_artifact_references(
    data: dict[str, Any], payload_entry: dict[str, Any], artifacts: dict[str, Any]
) -> None:
    contract_name = str(payload_entry["name"])
    reference_contract = payload_entry.get("artifact_references")
    if reference_contract is None:
        return
    declared = _reference_values(
        data,
        reference_contract["declared"],
        location=f"{contract_name}.artifact_references.declared",
    )
    missing = sorted(declared - set(artifacts))
    if missing:
        raise ContractError(f"payload references unknown artifacts: {', '.join(missing)}")
    nested: set[str] = set()
    for descriptor in reference_contract["nested"]:
        nested.update(
            _reference_values(
                data,
                descriptor,
                location=f"{contract_name}.artifact_references.nested",
            )
        )
    unlisted = sorted(nested - declared)
    if unlisted:
        raise ContractError(
            f"payload contains unlisted nested artifact refs: {', '.join(unlisted)}"
        )


def _validate_semantics(document: dict[str, Any]) -> None:
    lifecycle = document["lifecycle"]
    handoff = document["handoff"]
    delivery = document["delivery"]
    artifacts = document["artifacts"]
    payload = document["payload"]

    if lifecycle["phase"] == "finished" and handoff["status"] == "pending":
        raise ContractError("finished observations cannot have pending handoff")
    if lifecycle["phase"] == "finished" and delivery["status"] == "pending":
        raise ContractError("finished observations cannot have pending delivery")
    if lifecycle["outcome"] != "succeeded" and handoff["status"] == "ready":
        raise ContractError("only succeeded observations can be ready")

    required_statuses = [receipt["status"] for receipt in artifacts.values() if receipt["required"]]
    if delivery["status"] == "complete" and any(
        status != "available" for status in required_statuses
    ):
        raise ContractError("complete delivery contains an unavailable required artifact")
    if delivery["status"] == "incomplete" and not any(
        status != "available" for status in required_statuses
    ):
        raise ContractError("incomplete delivery needs an unavailable required artifact")

    upstream_keys = [
        (item["producer"]["name"], item["operation_id"], item["byte_sha256"])
        for item in document["lineage"]["upstream"]
    ]
    if len(upstream_keys) != len(set(upstream_keys)):
        raise ContractError("lineage contains a duplicate upstream envelope")

    producer_operation = (
        document["producer"]["name"],
        document["operation"]["kind"],
    )
    matching_routes = [
        route
        for route in _ROUTES
        if (route["producer"], route["operation_kind"]) == producer_operation
    ]
    if not matching_routes:
        raise ContractError("producer and operation route is not registered")

    if payload is None:
        if handoff["status"] == "ready":
            raise ContractError("ready handoff needs a payload")
        return

    contract = payload["contract"]
    payload_key = (contract["name"], contract["version"])
    payload_entry = next(
        (
            item
            for item in _PAYLOAD_CONTRACTS
            if (str(item["name"]), int(item["version"])) == payload_key
        ),
        None,
    )
    if payload_entry is None:
        raise ContractError("payload contract is not registered")
    route = next(
        (
            item
            for item in matching_routes
            if (item["payload_contract"], item["payload_version"]) == payload_key
        ),
        None,
    )
    if route is None:
        raise ContractError("producer, operation, and payload route is not registered")

    _validate_schema(payload["data"], str(payload_entry["schema"]))
    _check_requirements(
        payload["data"],
        route["requirements"],
        location="route",
    )
    _validate_artifact_references(payload["data"], payload_entry, artifacts)
    if handoff["status"] == "ready":
        _check_requirements(
            payload["data"],
            payload_entry["ready_requirements"],
            location=f"ready {contract['name']}",
        )


def validate_document(document: dict[str, Any]) -> None:
    """Reject ``document`` with :class:`ContractError` unless it is a valid observation."""
    _validate_schema(document, ENVELOPE_SCHEMA)
    _validate_semantics(document)


def validate_path(path: str | Path) -> None:
    validate_document(_load_json(Path(path)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_path(root: Path, raw: str) -> Path:
    relative = PurePosixPath(raw)
    target = (root / Path(*relative.parts)).resolve()
    try:
        common = os.path.commonpath((str(root), str(target)))
    except ValueError as exc:
        raise ContractError(f"artifact path is on another volume: {raw}") from exc
    # Upstream raises this inside the try, where ``except ValueError`` relabels
    # it as another volume; both refuse the observation.
    if common != str(root):
        raise ContractError(f"artifact path escapes the generation: {raw}")
    return target


def validate_machine_path(path: str | Path) -> None:
    """Validate a published ``machine.json`` and every available artifact's bytes."""
    candidate = Path(path).resolve()
    if candidate.name != "machine.json":
        raise ContractError("public machine metadata basename must be machine.json")
    document = _load_json(candidate)
    validate_document(document)
    root = candidate.parent
    for artifact_id, receipt in document["artifacts"].items():
        if receipt["status"] != "available":
            continue
        target = _artifact_path(root, receipt["path"])
        if not target.is_file():
            raise ContractError(f"artifact file is missing: {artifact_id}")
        if target.stat().st_size != receipt["bytes"]:
            raise ContractError(f"artifact byte count mismatch: {artifact_id}")
        if _sha256(target) != receipt["byte_sha256"]:
            raise ContractError(f"artifact sha256 mismatch: {artifact_id}")
