from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.messaging import Severity
from orca_auto.core.notifications import engines as engine_facade
from orca_auto.core.notifications.engines import EngineJobNotifications


def _notifications(send_fn: Any) -> EngineJobNotifications:
    return EngineJobNotifications(
        label="xTB",
        engine="xtb",
        selected_field_name="selected_xyz",
        detail_field_names=("mode", "molecule_key"),
        terminal_count_field="attempt_count",
        send_fn=send_fn,
    )


def test_notify_job_queued_renders_expected_lines() -> None:
    sent: list[tuple[list[str], Severity]] = []

    def send_fn(_cfg: Any, lines: list[str], severity: Severity) -> bool:
        sent.append((lines, severity))
        return True

    notifications = _notifications(send_fn)
    assert notifications.notify_job_queued(
        object(),
        job_id="job-1",
        queue_id="queue-1",
        job_dir=Path("/tmp/job-1"),
        selected_xyz=Path("/tmp/job-1/input.xyz"),
        mode="nci",
        ignored="x",
    )

    assert sent == [
        (
            [
                "[xTB] Job queued",
                "job_id: job-1",
                "queue_id: queue-1",
                "mode: nci",
                "job_dir: job-1",
                "selected_xyz: input.xyz",
            ],
            "info",
        )
    ]


def test_notify_job_finished_maps_status_and_appends_resource_lines() -> None:
    sent: list[tuple[list[str], Severity]] = []

    def send_fn(_cfg: Any, lines: list[str], severity: Severity) -> bool:
        sent.append((lines, severity))
        return True

    notifications = _notifications(send_fn)
    assert notifications.notify_job_finished(
        object(),
        job_id="job-2",
        queue_id="queue-2",
        status="failed",
        reason="xtb_error",
        job_dir=Path("/tmp/job-2"),
        selected_xyz=Path("/tmp/job-2/candidate.xyz"),
        attempt_count=3,
        resource_request={"max_cores": 4},
    )

    assert sent == [
        (
            [
                "[xTB] Job failed",
                "job_id: job-2",
                "queue_id: queue-2",
                "status: failed",
                "reason: xtb_error",
                "job_dir: job-2",
                "selected_xyz: candidate.xyz",
                "attempt_count: 3",
                "resource_request: {'max_cores': 4}",
            ],
            "error",
        )
    ]


def test_engine_facade_keeps_validation_helpers_private() -> None:
    helper_names = {
        "_optional_int_dict",
        "_required_int",
        "_required_path",
        "_required_str",
        "_required_value",
    }

    assert helper_names.isdisjoint(engine_facade.__all__)
