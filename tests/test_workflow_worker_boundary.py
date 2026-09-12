from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orca_auto import cli_workers
from orca_auto.core import extensions
from orca_auto.core.engine_catalog import engine_catalog, known_engine_ids
from orca_auto.core.engines import registry


def test_catalog_retains_workflow_engine_identities_when_extension_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extensions, "workflows_available", lambda: False)

    assert known_engine_ids() == ("orca", "xtb", "crest")
    assert [(entry.engine_id, entry.requires_workflows) for entry in engine_catalog()] == [
        ("orca", False),
        ("xtb", True),
        ("crest", True),
    ]
    assert [entry.admission_source for entry in engine_catalog()] == [
        "orca_auto.orca.queue_worker",
        "orca_auto.flow.engines.xtb.queue_worker",
        "orca_auto.flow.engines.crest.queue_worker",
    ]


@pytest.mark.parametrize("engine", ["xtb", "crest"])
def test_missing_workflows_rejects_engine_resolution_before_import(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extensions, "workflows_available", lambda: False)
    monkeypatch.setattr(
        registry, "import_module", lambda _: pytest.fail("unavailable engine imported")
    )

    with pytest.raises(ValueError, match="[Ww]orkflow"):
        registry.get_engine_definition(engine)


@pytest.mark.parametrize("engine", ["xtb", "crest"])
def test_installed_workflow_engine_import_failures_are_not_hidden(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extensions, "workflows_available", lambda: True)
    error = ModuleNotFoundError("broken installed engine dependency", name="missing_dependency")

    def broken_import(module: str) -> object:
        assert module == f"orca_auto.flow.engines.{engine}.engine"
        raise error

    monkeypatch.setattr(registry, "import_module", broken_import)

    with pytest.raises(ModuleNotFoundError) as caught:
        registry.get_engine_definition(engine)
    assert caught.value is error


def test_missing_workflows_rejects_worker_before_config_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extensions, "workflows_available", lambda: False)
    monkeypatch.setattr(
        cli_workers,
        "resolve_shared_config_path",
        lambda _: pytest.fail("unavailable workflow reached config resolution"),
    )
    monkeypatch.setattr(
        cli_workers,
        "worker_module_command",
        lambda **_: pytest.fail("unavailable workflow assembled a worker command"),
    )

    with pytest.raises(ValueError, match="[Ww]orkflow"):
        cli_workers._build_worker_specs(SimpleNamespace(app=["orca", "workflow"]))


def test_orca_worker_resolution_with_workflows_physically_absent(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[1] / "src" / "orca_auto"
    isolated_src = tmp_path / "src"
    shutil.copytree(
        package_root,
        isolated_src / "orca_auto",
        ignore=shutil.ignore_patterns("flow", "__pycache__"),
    )
    shutil.copytree(Path(yaml.__file__).parent, isolated_src / "yaml")
    assert not (isolated_src / "orca_auto" / "flow").exists()
    config = tmp_path / "orca_auto.yaml"
    config.write_text(f"runs_root: {tmp_path / 'runs'}\n", encoding="utf-8")
    script = """
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
import orca_auto
from orca_auto import cli_workers
from orca_auto.core.admission import read_active_slot_count, reserve_slot
from orca_auto.core.engine_catalog import known_engine_ids
from orca_auto.core.engines.registry import get_engine_definition
from orca_auto.core.extensions import workflows_available
from orca_auto.core.queue.worker.admission import engine_queue_worker_source

assert Path(orca_auto.__file__).parent == Path(sys.argv[1]) / 'orca_auto'
assert not workflows_available()
assert get_engine_definition('orca').engine == 'orca'
assert known_engine_ids() == ('orca', 'xtb', 'crest')
specs = cli_workers._build_worker_specs(
    SimpleNamespace(app=None, orca_auto_config=sys.argv[2])
)
assert [spec.app for spec in specs] == ['orca']
assert specs[0].argv[-2:] == ('--engine', 'orca')
admission_root = Path(sys.argv[2]).parent / 'admission'
for engine in ('xtb', 'crest'):
    assert reserve_slot(
        admission_root, 2,
        source=engine_queue_worker_source(engine),
        app_name=f'orca_auto_{engine}',
    )
assert read_active_slot_count(admission_root) == 2
assert reserve_slot(admission_root, 2, source=engine_queue_worker_source('orca')) is None
assert not any(name == 'orca_auto.flow' or name.startswith('orca_auto.flow.') for name in sys.modules)
print(json.dumps({'worker_apps': [spec.app for spec in specs], 'workflows_available': False}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(isolated_src), str(config)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"worker_apps": ["orca"], "workflows_available": False}
