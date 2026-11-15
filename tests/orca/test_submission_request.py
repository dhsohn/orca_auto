"""Typed submission request and explicit publication guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.orca.run_dir_guard import PinnedPublication
from orca_auto.orca.submission import SubmissionRequest, _prepare_submission_inputs
from tests.conftest import make_app_cfg


def test_submission_request_carries_raw_priority_for_normalization() -> None:
    request = SubmissionRequest(path="/tmp/job", priority=5, force=True)
    assert request.priority == 5
    assert request.force is True
    assert normalize_queue_priority(request.priority) != normalize_queue_priority(None)


def test_pinned_publication_invokes_check_and_skips_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "job.inp"
    selected.write_text("! SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
    stages: list[str] = []

    def reject(stage: str) -> None:
        stages.append(stage)
        raise RuntimeError(f"rejected at {stage}")

    publication = PinnedPublication(reject)
    with pytest.raises(RuntimeError, match="rejected at"):
        _prepare_submission_inputs(
            make_app_cfg(tmp_path),
            selected,
            publication=publication,
        )
    assert stages == ["ORCA target mutation preflight"]

    prepared = _prepare_submission_inputs(make_app_cfg(tmp_path), selected)
    assert prepared.force is False
