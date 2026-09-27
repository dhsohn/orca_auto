"""Install the contract effect log in worker children spawned by a contract test.

Loaded only when this directory is on ``PYTHONPATH``; ``effect_log`` then logs
only from a worker child while ``ORCA_AUTO_TEST_EFFECT_LOG`` is set.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

if os.environ.get("ORCA_AUTO_TEST_EFFECT_LOG"):
    _spec = importlib.util.spec_from_file_location(
        "_orca_auto_contract_effect_log",
        Path(__file__).resolve().parent.parent / "effect_log.py",
    )
    if _spec is not None and _spec.loader is not None:
        _module = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_module)
        _module.install_in_worker_child()
