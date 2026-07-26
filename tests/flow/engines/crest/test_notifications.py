from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.config import CommonRuntimeConfig, DiscordConfig, MessengerConfig
from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig
from orca_auto.core.messaging import Message, SendResult, render_discord_embed
from orca_auto.core.messaging.richtext import Line
from orca_auto.core.notifications import engines as notifications


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runtime=CommonRuntimeConfig(
            allowed_root=str(tmp_path / "allowed"),
        ),
        messenger=MessengerConfig(
            discord=DiscordConfig(bot_token="bot-token", default_channel_id="123")
        ),
    )


def _plain(message: Message) -> str:
    """Reconstruct the plain-text body of an engine (raw-span) notification.

    Engine job notifications carry pre-formatted plain-text lines, so the
    Doc-model title/heading and each raw line span map back to the exact text
    the Discord embed derives its title (line 0) and description (lines 1+) from.
    """
    parts: list[str] = []
    if message.author:
        parts.append(message.author)
    if message.title:
        parts.append(message.title)
    for group in message.groups:
        if group.heading:
            parts.append("".join(span.text for span in group.heading))
        for item in group.items:
            if isinstance(item, Line):
                parts.append("".join(span.text for span in item.spans))
    return "\n".join(parts)


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sent: bool,
    skipped: bool = False,
) -> tuple[list[Any], list[str], list[Message]]:
    build_calls: list[Any] = []
    messages: list[str] = []
    documents: list[Message] = []

    class FakeChannel:
        @property
        def enabled(self) -> bool:
            return True

        def send(self, message: Message, *, silent: bool = False) -> SendResult:
            documents.append(message)
            messages.append(_plain(message))
            return SendResult(sent=sent, skipped=skipped)

    def fake_build(messenger: Any) -> FakeChannel:
        build_calls.append(messenger)
        return FakeChannel()

    monkeypatch.setattr(notifications, "build_channel", fake_build)
    return build_calls, messages, documents


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
    build_calls, messages, _ = _patch_transport(monkeypatch, sent=sent, skipped=skipped)

    result = notifications.send_lines(cfg, ["first line", "second line"])

    assert result is expected
    assert build_calls == [cfg.messenger]
    assert messages == ["orca_auto\nfirst line\nsecond line"]


def test_notify_job_queued_sends_expected_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    _, messages, _ = _patch_transport(monkeypatch, sent=True)
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
                "orca_auto\n[CREST] Job queued",
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
    _, messages, _ = _patch_transport(monkeypatch, sent=True)
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
                "orca_auto\n[CREST] Job started",
                "job_id: job-002",
                "queue_id: queue-002",
                "mode: standard",
                "job_dir: job-002",
                "selected_xyz: selected_input.xyz",
            ]
        )
    ]


@pytest.mark.parametrize(
    ("status", "headline", "severity", "embed_title"),
    [
        ("completed", "Job finished", "success", "✅ [CREST] Job finished"),
        ("failed", "Job failed", "error", "❌ [CREST] Job failed"),
        ("cancelled", "Job cancelled", "warning", "⚠️ [CREST] Job cancelled"),
        ("running", "Job status unknown", "info", "[CREST] Job status unknown"),
    ],
)
def test_notify_job_finished_maps_terminal_headlines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
    headline: str,
    severity: str,
    embed_title: str,
) -> None:
    cfg = _cfg(tmp_path)
    _, messages, documents = _patch_transport(monkeypatch, sent=True)
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
    document = documents[-1]
    assert document.severity == severity
    assert render_discord_embed(document)["title"] == embed_title
    assert messages == [
        "\n".join(
            [
                f"orca_auto\n[CREST] {headline}",
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
    _, messages, _ = _patch_transport(monkeypatch, sent=True)
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
                "orca_auto\n[CREST] Job finished",
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
    _, messages, _ = _patch_transport(monkeypatch, sent=True)
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
