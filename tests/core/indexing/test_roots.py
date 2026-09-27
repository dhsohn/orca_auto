from pathlib import Path
from types import SimpleNamespace

from orca_auto.core.indexing import roots


def test_orca_index_roots_use_configured_runtime_root(tmp_path: Path) -> None:
    allowed_root = tmp_path / "runs"
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(allowed_root)))
    assert roots.runtime_roots_for_cfg(cfg) == (allowed_root.resolve(),)
