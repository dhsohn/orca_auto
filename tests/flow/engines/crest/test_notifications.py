from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.config import CommonRuntimeConfig, TelegramConfig
from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig
from orca_auto.core.messaging import SendResult, render_telegram
from orca_auto.core.notifications import _engine_transport
from orca_auto.core.notifications import engines as notifications


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runtime=CommonRuntimeConfig(
            allowed_root=str(tmp_path / "allowed"),
        ),
        telegram=TelegramConfig(bot_token="bot-token", chat_id="chat-id"),
    )


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sent: bool,
    skipped: bool = False,
) -> tuple[list[tuple[Any, Any]], list[str]]:
    build_calls: list[tuple[Any, Any]] = []
    messages: list[str] = []

    class FakeChannel:
        @property
        def enabled(self) -> bool:
            return True

        def send(self, message: Any, *, silent: bool = False) -> SendResult:
            messages.append(render_telegram(message))
            return SendResult(sent=sent, skipped=skipped)

    def fake_build(messenger: Any, telegram: Any) -> FakeChannel:
        build_calls.append((messenger, telegram))
        return FakeChannel()

    monkeypatch.setattr(_engine_transport, "build_channel", fake_build)
    return build_calls, messages


@pytest.mark.parametrize(
    ("sent", "skipped", "expected"),
    [
        (True, False, True),
        (False, True, True),
        (False, False, False),
    ],
)
def test_send_joins_lines_and_maps_transport_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sent: bool,
    skipped: bool,
    expected: bool,
) -> None:
    cfg = _cfg(tmp_path)
    build_calls, messages = _patch_transport(monkeypatch, sent=sent, skipped=skipped)

    result = notifications.send_lines(cfg, ["first line", "second line"])

    assert result is expected
    assert build_calls == [(cfg.messenger, cfg.telegram)]
    assert messages == ["first line\nsecond line"]


def test_notify_job_queued_sends_expected_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    _, messages = _patch_transport(monkeypatch, sent=True)
    job_dir = tmp_path / "runs" / "job-001"
    selected_xyz = job_dir / "picked.xyz"

    result = notifications.notify_crest_job_queued(
        cfg,
        job_id="job-001",
        queue_id="queue-001",
        job_dir=job_dir,
        mode="nci",
        selected_xyz=selected_xyz,
    )

    assert result is True
    assert messages == [
        "\n".join(
            [
                "[orca_auto_crest] Job queued",
                "job_id: job-001",
                "queue_id: queue-001",
                "mode: nci",
                "job_dir: job-001",
                "selected_xyz: picked.xyz",
            ]
        )
    ]


def test_notify_job_started_sends_expected_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    _, messages = _patch_transport(monkeypatch, sent=True)
    job_dir = tmp_path / "runs" / "job-002"
    selected_xyz = job_dir / "selected_input.xyz"

    result = notifications.notify_crest_job_started(
        cfg,
        job_id="job-002",
        queue_id="queue-002",
        job_dir=job_dir,
        mode="standard",
        selected_xyz=selected_xyz,
    )

    assert result is True
    assert messages == [
        "\n".join(
            [
                "[orca_auto_crest] Job started",
                "job_id: job-002",
                "queue_id: queue-002",
                "mode: standard",
                "job_dir: job-002",
                "selected_xyz: selected_input.xyz",
            ]
        )
    ]


@pytest.mark.parametrize(
    ("status", "headline"),
    [
        ("completed", "Job finished"),
        ("failed", "Job failed"),
        ("cancelled", "Job cancelled"),
    ],
)
def test_notify_job_finished_maps_terminal_headlines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
    headline: str,
) -> None:
    cfg = _cfg(tmp_path)
    _, messages = _patch_transport(monkeypatch, sent=True)
    job_dir = tmp_path / "runs" / f"job-{status}"
    selected_xyz = job_dir / "input.xyz"

    result = notifications.notify_crest_job_finished(
        cfg,
        job_id=f"job-{status}",
        queue_id=f"queue-{status}",
        status=status,
        reason=f"{status}-reason",
        mode="nci",
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        retained_conformer_count=3,
    )

    assert result is True
    assert messages == [
        "\n".join(
            [
                f"[orca_auto_crest] {headline}",
                f"job_id: job-{status}",
                f"queue_id: queue-{status}",
                f"status: {status}",
                f"reason: {status}-reason",
                "mode: nci",
                f"job_dir: job-{status}",
                "selected_xyz: input.xyz",
                "retained_conformer_count: 3",
            ]
        )
    ]


def test_notify_job_finished_includes_optional_extra_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    _, messages = _patch_transport(monkeypatch, sent=True)
    job_dir = tmp_path / "runs" / "job-complete"
    selected_xyz = job_dir / "input.xyz"
    resource_request = {"max_cores": 6, "max_memory_gb": 14}
    resource_actual = {"assigned_cores": 4, "memory_limit_gb": 12}

    result = notifications.notify_crest_job_finished(
        cfg,
        job_id="job-complete",
        queue_id="queue-complete",
        status="completed",
        reason="ok",
        mode="standard",
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        retained_conformer_count=7,
        resource_request=resource_request,
        resource_actual=resource_actual,
    )

    assert result is True
    assert messages == [
        "\n".join(
            [
                "[orca_auto_crest] Job finished",
                "job_id: job-complete",
                "queue_id: queue-complete",
                "status: completed",
                "reason: ok",
                "mode: standard",
                "job_dir: job-complete",
                "selected_xyz: input.xyz",
                "retained_conformer_count: 7",
                f"resource_request: {resource_request}",
                f"resource_actual: {resource_actual}",
            ]
        )
    ]


def test_workflow_child_notifications_are_suppressed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    _, messages = _patch_transport(monkeypatch, sent=True)
    workflow_job_dirs = [
        tmp_path / "wf-1" / "01_crest" / "job-queued",
        tmp_path / "wf-1" / "01_crest" / "crest_reactant_01",
    ]

    for workflow_job_dir in workflow_job_dirs:
        assert notifications.notify_crest_job_queued(
            cfg,
            job_id="job-queued",
            queue_id="queue-queued",
            job_dir=workflow_job_dir,
            mode="standard",
            selected_xyz=workflow_job_dir / "input.xyz",
        )
        assert notifications.notify_crest_job_finished(
            cfg,
            job_id="job-queued",
            queue_id="queue-queued",
            status="completed",
            reason="ok",
            mode="standard",
            job_dir=workflow_job_dir,
            selected_xyz=workflow_job_dir / "input.xyz",
            retained_conformer_count=2,
        )
    assert messages == []
