from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from orca_auto.core.commands.run_dir import load_yaml_job_manifest
from orca_auto.core.config.bounded_yaml import (
    MAX_JOB_MANIFEST_ALIASES,
    MAX_JOB_MANIFEST_BYTES,
    MAX_JOB_MANIFEST_DEPTH,
    MAX_JOB_MANIFEST_NODES,
)
from orca_auto.flow.manifest import load_flow_manifest


def _load_manifest(directory: Path, kind: str) -> dict[str, object]:
    if kind == "flow":
        return load_flow_manifest(directory)
    return load_yaml_job_manifest(
        directory,
        "job.yaml",
        invalid_message="invalid job manifest: {path}",
    )


def _manifest_path(directory: Path, kind: str) -> Path:
    return directory / ("flow.yaml" if kind == "flow" else "job.yaml")


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_accepts_normal_mapping(tmp_path: Path, kind: str) -> None:
    _manifest_path(tmp_path, kind).write_text(
        "workflow_type: conformer_screening\nresources:\n  max_cores: 4\n",
        encoding="utf-8",
    )

    assert _load_manifest(tmp_path, kind) == {
        "workflow_type": "conformer_screening",
        "resources": {"max_cores": 4},
    }


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_rejects_non_utf8_text(tmp_path: Path, kind: str) -> None:
    _manifest_path(tmp_path, kind).write_bytes(b"value: \xff\n")

    with pytest.raises(ValueError, match="must be UTF-8 text"):
        _load_manifest(tmp_path, kind)


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_rejects_duplicate_keys(tmp_path: Path, kind: str) -> None:
    _manifest_path(tmp_path, kind).write_text(
        "outer:\n  value: 1\n  value: 2\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate mapping key"):
        _load_manifest(tmp_path, kind)


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_rejects_unhashable_mapping_keys(
    tmp_path: Path,
    kind: str,
) -> None:
    _manifest_path(tmp_path, kind).write_text(
        "? [alpha, beta]\n: value\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="mapping keys must be hashable scalars"):
        _load_manifest(tmp_path, kind)


@pytest.mark.parametrize("kind", ["flow", "job"])
@pytest.mark.parametrize("payload", ["", "null\n"])
def test_bounded_manifest_loader_treats_empty_and_null_documents_as_empty_mappings(
    tmp_path: Path,
    kind: str,
    payload: str,
) -> None:
    _manifest_path(tmp_path, kind).write_text(payload, encoding="utf-8")

    assert _load_manifest(tmp_path, kind) == {}


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_preserves_malformed_yaml_errors(
    tmp_path: Path,
    kind: str,
) -> None:
    _manifest_path(tmp_path, kind).write_text("outer: [1,\n", encoding="utf-8")

    if kind == "flow":
        with pytest.raises(ValueError, match="Invalid Workflow manifest"):
            _load_manifest(tmp_path, kind)
        return

    with pytest.raises(yaml.parser.ParserError, match="while parsing a flow node"):
        _load_manifest(tmp_path, kind)


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_rejects_oversize_file(tmp_path: Path, kind: str) -> None:
    _manifest_path(tmp_path, kind).write_bytes(b"value: " + b"x" * MAX_JOB_MANIFEST_BYTES)

    with pytest.raises(ValueError, match="exceeds"):
        _load_manifest(tmp_path, kind)


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_rejects_symlink(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source.yaml"
    source.write_text("value: 1\n", encoding="utf-8")
    _manifest_path(tmp_path, kind).symlink_to(source.name)

    with pytest.raises(ValueError, match="regular file"):
        _load_manifest(tmp_path, kind)


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_rejects_hardlink(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source.yaml"
    source.write_text("value: 1\n", encoding="utf-8")
    os.link(source, _manifest_path(tmp_path, kind))

    with pytest.raises(ValueError, match="single-link"):
        _load_manifest(tmp_path, kind)


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_rejects_deep_nesting(tmp_path: Path, kind: str) -> None:
    depth = MAX_JOB_MANIFEST_DEPTH + 1
    _manifest_path(tmp_path, kind).write_text(
        "value: " + "[" * depth + "0" + "]" * depth + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="nesting"):
        _load_manifest(tmp_path, kind)


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_rejects_alias_bomb(tmp_path: Path, kind: str) -> None:
    aliases = ", ".join("*base" for _ in range(MAX_JOB_MANIFEST_ALIASES + 1))
    _manifest_path(tmp_path, kind).write_text(
        f"base: &base [1]\nvalues: [{aliases}]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="alias limit"):
        _load_manifest(tmp_path, kind)


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_rejects_recursive_alias_cycle(
    tmp_path: Path,
    kind: str,
) -> None:
    _manifest_path(tmp_path, kind).write_text("value: &value [*value]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="cycle"):
        _load_manifest(tmp_path, kind)


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_rejects_exponential_alias_expansion(
    tmp_path: Path,
    kind: str,
) -> None:
    lines = ["level0: &level0 [0]"]
    for level in range(1, 15):
        lines.append(f"level{level}: &level{level} [*level{level - 1}, *level{level - 1}]")
    _manifest_path(tmp_path, kind).write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="expands beyond"):
        _load_manifest(tmp_path, kind)


@pytest.mark.parametrize("kind", ["flow", "job"])
def test_bounded_manifest_loader_rejects_excessive_node_count(
    tmp_path: Path,
    kind: str,
) -> None:
    values = ",".join("0" for _ in range(MAX_JOB_MANIFEST_NODES + 1))
    _manifest_path(tmp_path, kind).write_text(f"values: [{values}]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="node limit"):
        _load_manifest(tmp_path, kind)
