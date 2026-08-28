"""Bounded, ambiguity-free YAML loading for untrusted local manifests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent, NodeEvent

from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file

YAML_CONFIG_LOAD_EXCEPTIONS = (OSError, ValueError, yaml.YAMLError)
MAX_JOB_MANIFEST_BYTES = 1024 * 1024
MAX_JOB_MANIFEST_ALIASES = 32
MAX_JOB_MANIFEST_NODES = 10_000
MAX_JOB_MANIFEST_DEPTH = 64


class UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeySafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        # types-PyYAML leaves BaseConstructor.construct_object untyped.
        key = loader.construct_object(key_node, deep=deep)  # type: ignore[no-untyped-call]
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ValueError("YAML mapping keys must be hashable scalars") from exc
        if duplicate:
            # Do not include the key or source line: config values can contain secrets.
            raise ValueError("YAML contains a duplicate mapping key")
        mapping[key] = loader.construct_object(  # type: ignore[no-untyped-call]
            value_node,
            deep=deep,
        )
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _validate_yaml_events(payload: str) -> None:
    aliases = 0
    nodes = 0
    depth = 0
    active_anchors: list[str | None] = []
    for event in yaml.parse(payload, Loader=yaml.SafeLoader):
        if isinstance(event, AliasEvent):
            aliases += 1
            if aliases > MAX_JOB_MANIFEST_ALIASES:
                raise ValueError(
                    f"YAML manifest exceeds the {MAX_JOB_MANIFEST_ALIASES}-alias limit"
                )
            if event.anchor in active_anchors:
                raise ValueError("YAML manifest contains a recursive alias cycle")
            continue
        if isinstance(event, NodeEvent):
            nodes += 1
            if nodes > MAX_JOB_MANIFEST_NODES:
                raise ValueError(f"YAML manifest exceeds the {MAX_JOB_MANIFEST_NODES}-node limit")
        if isinstance(event, CollectionStartEvent):
            depth += 1
            if depth > MAX_JOB_MANIFEST_DEPTH:
                raise ValueError(
                    f"YAML manifest exceeds the {MAX_JOB_MANIFEST_DEPTH}-level nesting limit"
                )
            active_anchors.append(event.anchor)
        elif isinstance(event, CollectionEndEvent):
            depth -= 1
            active_anchors.pop()


def _validate_yaml_object_graph(value: Any) -> None:
    expanded_nodes = 0
    active_containers: set[int] = set()

    def visit(current: Any, depth: int) -> None:
        nonlocal expanded_nodes
        expanded_nodes += 1
        if expanded_nodes > MAX_JOB_MANIFEST_NODES:
            raise ValueError(
                f"YAML manifest expands beyond the {MAX_JOB_MANIFEST_NODES}-node limit"
            )
        if depth > MAX_JOB_MANIFEST_DEPTH:
            raise ValueError(
                f"YAML manifest exceeds the {MAX_JOB_MANIFEST_DEPTH}-level nesting limit"
            )
        if not isinstance(current, (dict, list)):
            return
        identity = id(current)
        if identity in active_containers:
            raise ValueError("YAML manifest contains a recursive object graph")
        active_containers.add(identity)
        try:
            if isinstance(current, dict):
                for key, item in current.items():
                    visit(key, depth + 1)
                    visit(item, depth + 1)
            else:
                for item in current:
                    visit(item, depth + 1)
        finally:
            active_containers.remove(identity)

    visit(value, 0)


def load_bounded_yaml_data(
    path: str | Path,
    *,
    max_bytes: int = MAX_JOB_MANIFEST_BYTES,
) -> Any:
    """Load one bounded regular YAML file and reject pathological object graphs."""

    manifest_path = Path(path).expanduser()
    payload = read_stable_regular_file(
        manifest_path,
        max_bytes=max_bytes,
        require_single_link=True,
    )
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError(f"YAML manifest must be UTF-8 text: {manifest_path}") from exc
    _validate_yaml_events(text)
    parsed = yaml.load(text, Loader=UniqueKeySafeLoader)
    _validate_yaml_object_graph(parsed)
    return parsed
