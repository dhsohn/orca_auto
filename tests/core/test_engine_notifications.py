from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.messaging import Severity
from orca_auto.core.notifications import engines as engine_facade
from orca_auto.core.notifications.engine_jobs import build_engine_job_notifications


def _send_ok(_cfg: Any, _lines: list[str], _severity: Severity) -> bool:
    return True


def test_engine_job_notifications_builds_delivery_contract() -> None:
    notifications = build_engine_job_notifications(
        label="xTB",
        engine="xtb",
        selected_field_name="selected_xyz",
        detail_field_names=("mode", "molecule_key"),
        terminal_count_field="attempt_count",
        send_fn=_send_ok,
    )

    assert notifications.delivery.notifier is notifications.notifier
    assert notifications.delivery.detail_fields({"mode": "nci", "ignored": "x"}) == [
        ("mode", "nci")
    ]


def test_engine_job_notifications_builds_request_contract() -> None:
    notifications = build_engine_job_notifications(
        label="CREST",
        engine="crest",
        selected_field_name="selected_xyz",
        detail_field_names=("mode", "molecule_key"),
        terminal_count_field="attempt_count",
        send_fn=_send_ok,
    )

    request = notifications.request_factory.lifecycle_request(
        {
            "job_id": "crest-1",
            "queue_id": "queue-1",
            "job_dir": Path("/tmp/job"),
            "selected_xyz": Path("/tmp/job/input.xyz"),
            "mode": "standard",
        },
        "Job started",
    )

    assert request.headline == "Job started"
    assert request.job_id == "crest-1"
    assert request.selected_xyz.name == "input.xyz"
    assert request.detail_values == {"mode": "standard"}


def test_engine_facade_keeps_validation_helpers_private() -> None:
    helper_names = {
        "_optional_int_dict",
        "_optional_lines",
        "_optional_path",
        "_required_int",
        "_required_path",
        "_required_str",
        "_required_value",
    }

    assert helper_names.isdisjoint(engine_facade.__all__)
    for helper_name in helper_names:
        assert not hasattr(engine_facade, helper_name)
