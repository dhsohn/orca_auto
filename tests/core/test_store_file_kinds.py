"""The queue, admission and index stores read their JSON as a regular file only.

``core.utils.json_artifacts.load_json_list_file`` is the one list reader. A
missing file is an empty store; a FIFO is refused at once (each public reader
holds its store lock while it reads, so it must not block), and a dangling
symlink is a corrupt store, never an empty one. Each FIFO case runs in a child
process with a timeout so a regression fails instead of hanging the suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# (store, file under the root, public reader call, its corrupt error)
_STORES = {
    "queue": (
        "queue.json",
        "from orca_auto.core.queue.store import list_queue as read",
        "QueueStoreCorruptError",
    ),
    "admission": (
        "admission_slots.json",
        "from orca_auto.core.admission import list_all_slots as read",
        "AdmissionStoreCorruptError",
    ),
    "index": (
        "job_locations.json",
        "from orca_auto.core.indexing import list_job_locations as read",
        "JobLocationIndexCorruptError",
    ),
}


def _read_in_child(root: Path, store: str) -> list[str]:
    """``[outcome, error type]`` of the store's public reader, in a fresh interpreter."""
    _name, import_line, _error = _STORES[store]
    code = f"""
        import json
        from pathlib import Path
        {import_line}
        try:
            rows = read(Path({str(root)!r}))
            print(json.dumps(["rows", str(len(rows))]))
        except Exception as exc:
            print(json.dumps(["error", type(exc).__name__]))
        """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return list(json.loads(result.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize("store", sorted(_STORES))
def test_a_fifo_store_is_refused_without_blocking(tmp_path: Path, store: str) -> None:
    name, _import_line, error = _STORES[store]
    os.mkfifo(tmp_path / name)

    assert _read_in_child(tmp_path, store) == ["error", error]


@pytest.mark.parametrize("store", sorted(_STORES))
def test_a_dangling_store_link_is_corrupt_not_empty(tmp_path: Path, store: str) -> None:
    name, _import_line, error = _STORES[store]
    (tmp_path / name).symlink_to(tmp_path / "gone.json")

    assert _read_in_child(tmp_path, store) == ["error", error]


@pytest.mark.parametrize("store", sorted(_STORES))
def test_a_missing_store_is_still_empty(tmp_path: Path, store: str) -> None:
    assert _read_in_child(tmp_path, store) == ["rows", "0"]
