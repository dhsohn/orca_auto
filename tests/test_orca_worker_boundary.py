from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


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
from orca_auto.core.engine_catalog import get_engine_catalog_entry

assert Path(orca_auto.__file__).parent == Path(sys.argv[1]) / 'orca_auto'
assert get_engine_definition('orca').engine == 'orca'
assert known_engine_ids() == ('orca',)
specs = cli_workers._build_worker_specs(
    SimpleNamespace(app=None, orca_auto_config=sys.argv[2])
)
assert [spec.app for spec in specs] == ['orca']
assert specs[0].argv[-2:] == ('--engine', 'orca')
admission_root = Path(sys.argv[2]).parent / 'admission'
for engine in ('xtb', 'crest'):
    assert reserve_slot(
        admission_root, 2,
        source=f'orca_auto.flow.engines.{engine}.queue_worker',
        app_name=f'orca_auto_{engine}',
    )
assert read_active_slot_count(admission_root) == 2
assert reserve_slot(admission_root, 2, source=get_engine_catalog_entry('orca').admission_source) is None
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
