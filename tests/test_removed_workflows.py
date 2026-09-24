from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.cli_parsers import build_parser
from orca_auto.core.config.files import validate_shared_config_sections
from orca_auto.core.engine_catalog import engine_catalog, get_engine_catalog_entry


@pytest.mark.parametrize(
    "argv",
    [
        ["scaffold", "conformer_search", "/tmp/retired"],
        ["queue", "worker", "--app", "orca"],
        ["queue", "worker", "--app", "workflow"],
        ["queue", "list", "--engine", "orca"],
        ["queue", "list", "--engine", "workflow"],
        ["queue", "list", "--kind", "job"],
        ["queue", "list", "--kind", "workflow"],
        ["queue", "worker", "--no-submit"],
        ["queue", "worker", "--refresh-registry"],
        ["queue", "worker", "--refresh-each-cycle"],
        ["queue", "worker", "--max-cycles", "1"],
        ["queue", "worker", "--interval-seconds", "1"],
        ["queue", "worker", "--lock-timeout-seconds", "1"],
        ["run-dir", "/tmp/retired", "--max-cores", "2"],
        ["run-dir", "/tmp/retired", "--max-memory-gb", "4"],
    ],
)
def test_removed_workflow_commands_are_not_parser_options(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(argv)
    assert error.value.code == 2


@pytest.mark.parametrize("engine", ["workflow", "xtb", "crest"])
def test_retired_engines_are_absent_from_the_catalog(engine: str) -> None:
    assert tuple(entry.engine_id for entry in engine_catalog()) == ("orca",)
    with pytest.raises(ValueError, match="unsupported engine"):
        get_engine_catalog_entry(engine)


@pytest.mark.parametrize(
    "section", [{}, {"root": "/tmp/old"}, {"paths": {"xtb_executable": "/tmp/xtb"}}]
)
def test_workflow_config_is_rejected_even_when_empty(section: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="Unknown top-level config fields"):
        validate_shared_config_sections({"runs_root": "/tmp/runs", "workflow": section})


def test_workflow_source_and_extension_package_are_absent() -> None:
    repo = Path(__file__).resolve().parents[1]
    assert not (repo / "src/orca_auto/flow").exists()
    assert not (repo / "extensions/workflows/pyproject.toml").exists()
