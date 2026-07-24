from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from orca_auto.core.config import CommonRuntimeConfig, DiscordConfig, MessengerConfig
from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig
from orca_auto.core.messaging import (
    Message,
    SendResult,
    render_discord_embed,
)
from orca_auto.core.messaging.richtext import Line
from orca_auto.core.notifications import _engine_transport
from orca_auto.core.notifications import engines as notifications


def _plain(message: Message) -> str:
    """Reconstruct the plain-text body of an engine (raw-span) notification.

    Engine job notifications carry pre-formatted plain-text lines, so the
    Doc-model title/heading and each raw line span map back to the exact text
    the Discord embed derives its title (line 0) and description (lines 1+) from.
    """
    parts: list[str] = []
    if message.author:
        parts.append(message.author)
    for group in message.groups:
        if group.heading:
            parts.append("".join(span.text for span in group.heading))
        for item in group.items:
            if isinstance(item, Line):
                parts.append("".join(span.text for span in item.spans))
    return "\n".join(parts)


class _FakeTransport:
    """Records the plain-text body of each Doc-model message sent to the channel."""

    def __init__(self, result: SendResult) -> None:
        self.result = result
        self.messages: list[str] = []
        self.documents: list[Message] = []

    @property
    def enabled(self) -> bool:
        return True

    def send(self, message: Message, *, silent: bool = False) -> SendResult:
        self.documents.append(message)
        self.messages.append(_plain(message))
        return self.result


def _make_cfg(tmp_path: Path, *, enabled: bool = False) -> AppConfig:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    discord = DiscordConfig(
        bot_token="token" if enabled else "",
        default_channel_id="123" if enabled else "",
    )
    return AppConfig(
        runtime=CommonRuntimeConfig(
            allowed_root=str(allowed_root),
        ),
        messenger=MessengerConfig(discord=discord),
    )


def test_send_returns_true_when_real_transport_skips_disabled_messenger(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, enabled=False)

    assert notifications.send_lines(cfg, ["line 1", "line 2"])


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (SendResult(sent=True), True),
        (SendResult(sent=False, skipped=True), True),
        (SendResult(sent=False, skipped=False, error="boom"), False),
    ],
)
def test_send_joins_lines_and_maps_transport_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: SendResult,
    expected: bool,
) -> None:
    cfg = _make_cfg(tmp_path, enabled=True)
    transport = _FakeTransport(result)
    monkeypatch.setattr(_engine_transport, "build_channel", lambda _messenger: transport)

    sent = notifications.send_lines(cfg, ["line 1", "line 2"])

    assert sent is expected
    assert transport.messages == ["orca_auto\nline 1\nline 2"]


def test_notify_job_queued_and_started_render_expected_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path, enabled=True)
    transport = _FakeTransport(SendResult(sent=True))
    monkeypatch.setattr(_engine_transport, "build_channel", lambda _messenger: transport)
    job_dir = tmp_path / "job-001"
    selected_xyz = tmp_path / "inputs" / "reactant.xyz"

    assert notifications.notify_xtb_job_queued(
        cfg,
        job_id="job-001",
        queue_id="queue-001",
        job_dir=job_dir,
        job_type="ranking",
        reaction_key="rxn-1",
        selected_xyz=selected_xyz,
    )
    assert notifications.notify_xtb_job_started(
        cfg,
        job_id="job-001",
        queue_id="queue-001",
        job_dir=job_dir,
        job_type="ranking",
        reaction_key="rxn-1",
        selected_xyz=selected_xyz,
    )

    assert transport.messages == [
        "\n".join(
            [
                "orca_auto\n[xTB] Job queued",
                "job_id: job-001",
                "queue_id: queue-001",
                "job_type: ranking",
                "reaction_key: rxn-1",
                "job_dir: job-001",
                "selected_input_xyz: reactant.xyz",
            ]
        ),
        "\n".join(
            [
                "orca_auto\n[xTB] Job started",
                "job_id: job-001",
                "queue_id: queue-001",
                "job_type: ranking",
                "reaction_key: rxn-1",
                "job_dir: job-001",
                "selected_input_xyz: reactant.xyz",
            ]
        ),
    ]


@pytest.mark.parametrize(
    ("status", "headline", "severity", "embed_title"),
    [
        ("completed", "Job finished", "success", "✅ [xTB] Job finished"),
        ("failed", "Job failed", "error", "❌ [xTB] Job failed"),
        ("cancelled", "Job cancelled", "warning", "⚠️ [xTB] Job cancelled"),
        ("running", "Job status unknown", "info", "[xTB] Job status unknown"),
    ],
)
def test_notify_job_finished_maps_headlines_and_optional_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    headline: str,
    severity: str,
    embed_title: str,
) -> None:
    cfg = _make_cfg(tmp_path, enabled=True)
    transport = _FakeTransport(SendResult(sent=True))
    monkeypatch.setattr(_engine_transport, "build_channel", lambda _messenger: transport)
    resource_request: dict[str, int] | None = None
    resource_actual: dict[str, int] | None = None
    if status == "completed":
        resource_request = {"max_cores": 8, "max_memory_gb": 16}
        resource_actual = {"max_cores": 6, "max_memory_gb": 12}

    assert notifications.notify_xtb_job_finished(
        cfg,
        job_id="job-003",
        queue_id="queue-003",
        status=status,
        reason="done",
        job_type="opt",
        reaction_key="rxn-3",
        job_dir=tmp_path / "job-003",
        selected_xyz=tmp_path / "inputs" / "optimized.xyz",
        candidate_count=1,
        resource_request=cast(dict[str, int] | None, resource_request),
        resource_actual=cast(dict[str, int] | None, resource_actual),
    )

    message = transport.messages[-1]
    assert message.startswith(f"orca_auto\n[xTB] {headline}\n")
    document = transport.documents[-1]
    assert document.severity == severity
    assert render_discord_embed(document)["title"] == embed_title
    assert "job_id: job-003" in message
    assert f"status: {status}" in message
    assert "job_dir: job-003" in message
    assert "selected_input_xyz: optimized.xyz" in message
    assert "candidate_count: 1" in message
    if status == "completed":
        assert "resource_request: {'max_cores': 8, 'max_memory_gb': 16}" in message
        assert "resource_actual: {'max_cores': 6, 'max_memory_gb': 12}" in message
    else:
        assert "resource_request: " not in message
        assert "resource_actual: " not in message


def test_workflow_child_notifications_are_suppressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path, enabled=True)
    transport = _FakeTransport(SendResult(sent=True))
    monkeypatch.setattr(_engine_transport, "build_channel", lambda _messenger: transport)
    workflow_job_dirs = [
        tmp_path / "wf-1" / "02_xtb" / "job-004",
        tmp_path / "wf-1" / "02_xtb" / "xtb_path_search_01",
    ]

    for workflow_job_dir in workflow_job_dirs:
        assert notifications.notify_xtb_job_queued(
            cfg,
            job_id="job-004",
            queue_id="queue-004",
            job_dir=workflow_job_dir,
            job_type="path_search",
            reaction_key="rxn-4",
            selected_xyz=workflow_job_dir / "ts.xyz",
        )
        assert notifications.notify_xtb_job_finished(
            cfg,
            job_id="job-004",
            queue_id="queue-004",
            status="completed",
            reason="done",
            job_type="path_search",
            reaction_key="rxn-4",
            job_dir=workflow_job_dir,
            selected_xyz=workflow_job_dir / "ts.xyz",
            candidate_count=2,
        )
    assert transport.messages == []
