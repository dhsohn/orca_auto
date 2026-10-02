"""Slack through the public config loader and the lifecycle notifier boundary.

A synthetic ``orca_auto.yaml`` in ``tmp_path`` is read by the real
``load_config``; the channel comes from the real
``orca.queue.notifications.notification_channel`` (the seam the
``recording_channel`` fixture replaces) and the message goes through the real
``notify_*`` delivery functions and adapter. Only the lowest transport is
replaced: ``HTTPSHandler.https_open`` and ``HTTPHandler.http_open`` answer with
standard-library ``addinfourl`` responses and record every request. Sockets are
refused, proxies and the cached global opener are isolated, and ``time.sleep``
only records delays. No ORCA run, queue row or result is written.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import Message as EmailMessage
from io import BytesIO
from pathlib import Path
from urllib.response import addinfourl

import pytest
import yaml

import orca_auto
from orca_auto.core.messaging import DisabledChannel, MessageChannel
from orca_auto.core.messaging.discord_bot import DiscordBotChannel
from orca_auto.core.messaging.slack_bot import SlackBotChannel
from orca_auto.orca.config import load_config
from orca_auto.orca.notifications import (
    build_run_finished_notification,
    notify_queue_enqueued_event,
    notify_run_finished_event,
    notify_run_started_event,
)
from orca_auto.orca.queue import notifications as queue_notifications
from orca_auto.orca.types import QueueEnqueuedNotification, RunStartedNotification
from tests.conftest import write_fake_orca

# Obviously synthetic values: no real Slack/Discord workspace, token or channel.
_SLACK_TOKEN = "xoxb-synthetic-lifecycle-token"
_SLACK_CHANNEL = "C0SYNTHETIC1"
_SLACK_URL = "https://slack.com/api/chat.postMessage"
_SLACK_CONFIRMED = {"ok": True, "channel": _SLACK_CHANNEL, "ts": "1700000000.000100"}
_DISCORD_TOKEN = "synthetic-discord-lifecycle-token"
_DISCORD_CHANNEL = "123"
_DISCORD_URL = f"https://discord.com/api/v10/channels/{_DISCORD_CHANNEL}/messages"
_DISCORD_CONFIRMED = {"id": "223456789012345678"}
_SLACK_MESSENGER = {
    "provider": "slack",
    "slack": {"bot_token": _SLACK_TOKEN, "default_channel_id": _SLACK_CHANNEL},
}
_DISCORD_SECTION = {"bot_token": _DISCORD_TOKEN, "default_channel_id": _DISCORD_CHANNEL}
_JOB_NAME = "synthetic_lifecycle_job"
_PROXY_VARIABLES = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)
_ADAPTER_MODULES = (
    "orca_auto.core.messaging.slack_bot",
    "orca_auto.core.messaging.discord_bot",
)


@dataclass(frozen=True)
class _Outgoing:
    url: str
    method: str
    headers: dict[str, str]
    body: bytes | None


@dataclass
class _Transport:
    """Confirms the fixed Slack and Discord endpoints; any other URL fails the test."""

    outgoing: list[_Outgoing] = field(default_factory=list)
    streams: list[BytesIO] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)

    def respond(self, request: urllib.request.Request) -> addinfourl:
        data = request.data
        self.outgoing.append(
            _Outgoing(
                url=request.full_url,
                method=request.get_method(),
                headers=dict(request.header_items()),
                body=data if isinstance(data, bytes) else None,
            )
        )
        body: Mapping[str, object]
        if request.full_url == _SLACK_URL:
            body = _SLACK_CONFIRMED
        elif request.full_url == _DISCORD_URL:
            body = _DISCORD_CONFIRMED
        else:
            raise AssertionError("unexpected notification target")
        headers = EmailMessage()
        headers["Content-Type"] = "application/json; charset=utf-8"
        stream = BytesIO(json.dumps(body).encode("utf-8"))
        self.streams.append(stream)
        response = addinfourl(stream, headers, request.full_url, 200)
        response.msg = "OK"  # type: ignore[attr-defined]
        return response


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> _Transport:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a lifecycle notification test must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    for variable in _PROXY_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    # A fresh default opener for this test; the cached one is restored afterwards.
    monkeypatch.setattr(urllib.request, "_opener", None)

    fake = _Transport()

    def https_open(_handler: object, request: urllib.request.Request) -> addinfourl:
        return fake.respond(request)

    def http_open(_handler: object, request: urllib.request.Request) -> addinfourl:
        return fake.respond(request)

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", http_open)
    monkeypatch.setattr(time, "sleep", fake.sleeps.append)
    return fake


def _write_config(
    tmp_path: Path,
    messenger: Mapping[str, object] | None,
    *,
    name: str = "orca_auto.yaml",
) -> Path:
    """A minimal valid ``orca_auto.yaml`` in ``tmp_path`` with an empty runs root."""

    runs_root = tmp_path / "runs"
    runs_root.mkdir(exist_ok=True)
    fake_orca = write_fake_orca(tmp_path / "fake_orca")
    payload: dict[str, object] = {
        "runs_root": str(runs_root),
        "orca": {"paths": {"orca_executable": str(fake_orca)}},
    }
    if messenger is not None:
        payload["messenger"] = dict(messenger)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _queued_event(job_dir: Path) -> QueueEnqueuedNotification:
    return {
        "queue_id": "q-synthetic-lifecycle",
        "reaction_dir": str(job_dir),
        "priority": 10,
        "force": False,
        "enqueued_at": "2026-10-02T00:00:00+00:00",
    }


def _notify(kind: str, channel: MessageChannel, job_dir: Path) -> bool:
    """Deliver one synthetic lifecycle event through the real ``notify_*`` function."""

    selected_inp = job_dir / "rxn.inp"
    if kind == "queued":
        return notify_queue_enqueued_event(channel, _queued_event(job_dir))
    if kind == "started":
        started: RunStartedNotification = {
            "reaction_dir": str(job_dir),
            "selected_inp": str(selected_inp),
            "current_inp": str(selected_inp),
            "run_id": "run-synthetic-lifecycle",
            "attempt_index": 1,
            "status": "running",
            "attempt_started_at": "2026-10-02T00:00:01+00:00",
            "resumed": False,
        }
        return notify_run_started_event(channel, started)
    finished = build_run_finished_notification(
        reaction_dir=job_dir,
        selected_inp=selected_inp,
        state={"run_id": "run-synthetic-lifecycle", "attempts": []},
        status="completed",
        final_result={
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "normal_termination",
            "completed_at": "2026-10-02T00:00:02+00:00",
            "last_out_path": None,
        },
    )
    return notify_run_finished_event(channel, finished)


# --------------------------------------------------------------------------- #
# Slack selected in the config file reaches Slack, and only Slack
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("kind", "title"),
    [
        ("queued", "ORCA queued"),
        ("started", "ORCA started"),
        ("finished", "ORCA completed"),
    ],
)
def test_slack_config_file_delivers_lifecycle_notifications_to_slack_only(
    tmp_path: Path,
    transport: _Transport,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    title: str,
) -> None:
    config_file = _write_config(tmp_path, _SLACK_MESSENGER)

    with caplog.at_level("DEBUG"):
        cfg = load_config(str(config_file))
        channel = queue_notifications.notification_channel(cfg)
        sent = _notify(kind, channel, tmp_path / "runs" / _JOB_NAME)

    assert (cfg.messenger.provider, cfg.messenger.enabled) == ("slack", True)
    assert isinstance(channel, SlackBotChannel)
    assert channel.config is cfg.messenger.slack
    assert sent is True
    assert [request.url for request in transport.outgoing] == [_SLACK_URL]
    [request] = transport.outgoing
    assert request.method == "POST"
    assert request.headers.get("Authorization") == f"Bearer {_SLACK_TOKEN}"
    assert request.body is not None
    payload = json.loads(request.body)
    assert payload["channel"] == _SLACK_CHANNEL
    assert title in payload["text"]
    assert _JOB_NAME in payload["text"]
    assert transport.sleeps == []
    assert all(stream.closed for stream in transport.streams)
    assert _SLACK_TOKEN not in caplog.text
    # Delivery is advisory: nothing is written under the runs root.
    assert list((tmp_path / "runs").iterdir()) == []


# --------------------------------------------------------------------------- #
# Discord delivery: the same path still reaches Discord, and only Discord
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "messenger",
    [
        pytest.param({"provider": "discord", "discord": _DISCORD_SECTION}, id="explicit"),
        pytest.param({"discord": _DISCORD_SECTION}, id="default-provider"),
    ],
)
def test_discord_config_file_still_delivers_to_discord_only(
    tmp_path: Path,
    transport: _Transport,
    caplog: pytest.LogCaptureFixture,
    messenger: Mapping[str, object],
) -> None:
    config_file = _write_config(tmp_path, messenger)

    with caplog.at_level("DEBUG"):
        cfg = load_config(str(config_file))
        channel = queue_notifications.notification_channel(cfg)
        sent = _notify("queued", channel, tmp_path / "runs" / _JOB_NAME)

    assert (cfg.messenger.provider, cfg.messenger.enabled) == ("discord", True)
    assert isinstance(channel, DiscordBotChannel)
    assert sent is True
    assert [request.url for request in transport.outgoing] == [_DISCORD_URL]
    [request] = transport.outgoing
    assert request.headers.get("Authorization") == f"Bot {_DISCORD_TOKEN}"
    assert transport.sleeps == []
    assert _DISCORD_TOKEN not in caplog.text


# --------------------------------------------------------------------------- #
# Default or incomplete Slack config: no channel, no transport, no state touched
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "messenger",
    [
        pytest.param(None, id="no-messenger-section"),
        pytest.param({"provider": "slack"}, id="slack-without-section"),
        pytest.param({"provider": "slack", "slack": {}}, id="slack-empty-section"),
        pytest.param(
            {"provider": "slack", "slack": {"bot_token": _SLACK_TOKEN}},
            id="slack-token-only",
        ),
        pytest.param(
            {"provider": "slack", "slack": {"default_channel_id": _SLACK_CHANNEL}},
            id="slack-channel-only",
        ),
    ],
)
def test_default_or_incomplete_slack_config_sends_nothing_anywhere(
    tmp_path: Path,
    transport: _Transport,
    messenger: Mapping[str, object] | None,
) -> None:
    config_file = _write_config(tmp_path, messenger)
    runs_root = tmp_path / "runs"

    cfg = load_config(str(config_file))
    channel = queue_notifications.notification_channel(cfg)

    assert cfg.messenger.enabled is False
    assert isinstance(channel, DisabledChannel)
    assert _notify("queued", channel, runs_root / _JOB_NAME) is False
    # The parent's real claim entry points stop before reading any queue or state.
    queue_notifications.notify_queued_jobs(cfg)
    assert queue_notifications.claim_and_send_terminal(cfg, str(runs_root / _JOB_NAME)) is False
    assert transport.outgoing == []
    assert transport.sleeps == []
    assert list(runs_root.iterdir()) == []


def test_slack_selected_with_a_discord_section_is_rejected_by_the_loader(
    tmp_path: Path,
    transport: _Transport,
) -> None:
    config_file = _write_config(
        tmp_path,
        {"provider": "slack", "discord": _DISCORD_SECTION},
    )

    with pytest.raises(ValueError, match="Unknown messenger config fields") as excinfo:
        load_config(str(config_file))

    assert _DISCORD_TOKEN not in str(excinfo.value)
    assert transport.outgoing == []


# --------------------------------------------------------------------------- #
# Adapters load only when the loaded config selects them
# --------------------------------------------------------------------------- #
_IMPORT_PROBE = """
import json
import socket
import sys


def refuse(*_args, **_kwargs):
    raise AssertionError("the import probe must not open a network connection")


socket.create_connection = refuse
socket.socket.connect = refuse

from orca_auto.orca.config import load_config
from orca_auto.orca.queue.notifications import notification_channel

ADAPTERS = {adapters!r}


def loaded():
    return [name for name in ADAPTERS if name in sys.modules]


report = {{"after_import": loaded()}}
disabled = notification_channel(load_config(sys.argv[1]))
report["disabled_channel"] = type(disabled).__name__
report["after_disabled"] = loaded()
slack = notification_channel(load_config(sys.argv[2]))
report["slack_channel"] = type(slack).__name__
report["after_slack"] = loaded()
print(json.dumps(report))
"""


def test_loading_a_slack_config_imports_only_the_slack_adapter(tmp_path: Path) -> None:
    disabled_config = _write_config(tmp_path, {"provider": "slack"}, name="disabled.yaml")
    slack_config = _write_config(tmp_path, _SLACK_MESSENGER, name="slack.yaml")
    source_root = Path(orca_auto.__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
    )
    for variable in _PROXY_VARIABLES:
        env.pop(variable, None)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _IMPORT_PROBE.format(adapters=_ADAPTER_MODULES),
            str(disabled_config),
            str(slack_config),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report == {
        "after_import": [],
        "disabled_channel": "DisabledChannel",
        "after_disabled": [],
        "slack_channel": "SlackBotChannel",
        "after_slack": ["orca_auto.core.messaging.slack_bot"],
    }
    assert _SLACK_TOKEN not in completed.stdout + completed.stderr
