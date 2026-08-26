from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.flow.engines.crest import queue_runtime


def _entry(metadata: Any) -> SimpleNamespace:
    return SimpleNamespace(
        queue_id="q_test",
        task_id="t_test",
        source="orca_auto_crest",
        engine="crest",
        status="completed",
        metadata=metadata,
    )


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({"job_dir": "/tmp/with\x00null"}, id="nul-byte"),
        pytest.param(None, id="metadata-is-not-a-mapping"),
        pytest.param({"job_dir": 5}, id="job-dir-not-a-string"),
        pytest.param({"job_dir": "relative/path"}, id="relative-job-dir"),
        pytest.param({}, id="job-dir-missing"),
        pytest.param({"job_dir": "   "}, id="job-dir-blank"),
    ],
)
def test_terminal_repair_declines_a_malformed_job_dir_instead_of_raising(
    metadata: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The terminal-repair sweep has no per-entry guard, so anything raised here
    # escapes the worker loop, which catches only KeyboardInterrupt.
    monkeypatch.setattr(queue_runtime, "_is_crest_queue_entry", lambda _entry: True)

    assert (
        queue_runtime._terminal_entry_needs_repair(
            object(),
            _entry(metadata),
            status="completed",
            indexed_record=None,
            index_loaded=False,
        )
        is False
    )


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({"job_dir": "/tmp/with\x00null"}, id="nul-byte"),
        pytest.param(None, id="metadata-is-not-a-mapping"),
        pytest.param({"job_dir": 5}, id="job-dir-not-a-string"),
    ],
)
def test_terminal_artifact_adoption_declines_a_malformed_job_dir(
    metadata: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(queue_runtime, "_is_crest_queue_entry", lambda _entry: True)

    assert queue_runtime._adopt_terminal_artifacts(object(), tmp_path, _entry(metadata)) is False
