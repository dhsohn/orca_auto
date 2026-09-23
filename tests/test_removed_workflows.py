from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.cli_parsers import build_parser
from orca_auto.core.config.files import validate_shared_config_sections
from orca_auto.core.engine_catalog import known_engine_ids
from orca_auto.core.engines import registry


@pytest.mark.parametrize(
    "argv",
    [
        ["scaffold", "conformer_search", "/tmp/retired"],
        ["queue", "worker", "--app", "workflow"],
        ["queue", "worker", "--app", "xtb"],
        ["queue", "worker", "--app", "crest"],
        ["queue", "list", "--engine", "workflow"],
        ["queue", "list", "--engine", "xtb"],
        ["queue", "list", "--engine", "crest"],
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
def test_retired_engine_resolution_rejects_before_import(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert known_engine_ids() == ("orca",)
    monkeypatch.setattr(
        registry, "import_module", lambda _name: pytest.fail("retired engine imported")
    )
    with pytest.raises(ValueError, match="unsupported engine"):
        registry.get_engine_definition(engine)


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
