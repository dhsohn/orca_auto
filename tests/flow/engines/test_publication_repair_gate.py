from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from orca_auto.flow.engines import queue_runtime_common as common
from orca_auto.flow.submitters import internal_engine_submission


class _Worker:
    """Minimal stand-in for an engine queue worker.

    The gate rebinds ``_reserve_next_entry`` in the instance ``__dict__``, so
    the double only needs the attribute and a ``cfg`` to hand to the sweep.
    """

    def __init__(self) -> None:
        self.cfg = object()
        self.reserved = 0

    def _reserve_next_entry(self) -> tuple[str, Any | None]:
        self.reserved += 1
        return "reserved", "entry"


class _Entry:
    def __init__(self, queue_id: str) -> None:
        self.queue_id = queue_id


def test_publication_repair_gate_reserves_when_repair_succeeds() -> None:
    worker = _Worker()
    common.install_publication_repair_gate(worker, engine="xTB", repair_fn=lambda _worker: True)

    assert worker._reserve_next_entry() == ("reserved", "entry")
    assert worker.reserved == 1


def test_publication_repair_gate_blocks_and_reports_when_repair_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = _Worker()
    common.install_publication_repair_gate(worker, engine="xTB", repair_fn=lambda _worker: False)

    with caplog.at_level(logging.WARNING):
        assert worker._reserve_next_entry() == ("blocked", None)

    # Repair is the only path that publishes a row whose queued record never
    # landed, so a blocked admission must never be silent.
    assert worker.reserved == 0
    assert any("xTB queued publication repair" in record.getMessage() for record in caplog.records)


def test_publication_repair_sweep_reports_a_raising_row_and_continues(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = _Worker()
    seen: list[str] = []

    def repair(
        *,
        cfg: Any,
        queue_root: Path,
        entry: Any,
        record_queued_fn: Any,
        entry_matches_fn: Any,
    ) -> bool:
        seen.append(entry.queue_id)
        if entry.queue_id == "q_raises":
            raise RuntimeError("publication repair exploded")
        return True

    monkeypatch.setattr(
        internal_engine_submission,
        "repair_internal_engine_queue_publication",
        repair,
    )

    with caplog.at_level(logging.ERROR):
        repaired = common.repair_engine_queue_publications(
            worker,
            engine="CREST",
            queue_entries_with_roots_fn=lambda _cfg: [
                (tmp_path, _Entry("q_raises")),
                (tmp_path, _Entry("q_ok")),
            ],
            is_engine_entry_fn=lambda _entry: True,
            record_queued_fn=lambda *_args, **_kwargs: None,
        )

    # One bad row must neither stop the sweep nor disappear without a trace.
    assert repaired is False
    assert seen == ["q_raises", "q_ok"]
    assert any("q_raises" in record.getMessage() for record in caplog.records)
    assert any(record.exc_info for record in caplog.records)
