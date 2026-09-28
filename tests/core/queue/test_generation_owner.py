from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core.queue import generation_owner


@pytest.mark.parametrize(
    "namespace",
    ["x" * 81, "contains spaces", "../escape", " leading", "trailing "],
)
def test_snapshot_namespace_rejects_lossy_path_normalization(
    tmp_path: Path,
    namespace: str,
) -> None:
    with pytest.raises(ValueError, match="safe path segment"):
        generation_owner.canonical_input_snapshot_namespace(namespace)
