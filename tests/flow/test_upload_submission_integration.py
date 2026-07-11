"""End-to-end: an uploaded archive, once confirmed, reaches the real queue.

This exercises the actual CLI submission handlers (not a stub), so it guards the
``argparse.Namespace`` contract in ``_submit_extracted_run_dir`` against drift and
proves the extracted run-dir enqueues under its directory name.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from orca_auto.core.ingest import UploadPolicy
from orca_auto.core.messaging.channel import SendResult
from orca_auto.core.messaging.interactive import (
    Actor,
    BotReply,
    ConversationAddress,
    IncomingAction,
    IncomingUpload,
)
from orca_auto.flow.bot import BotApplication, settings_from_config

ADDRESS = ConversationAddress(provider="discord", channel_id="100")
ACTOR = Actor(user_id="42", label="operator")
_WATER_INP = "! HF STO-3G Opt\n* xyz 0 1\nO 0 0 0\nH 0 0.75 0.58\nH 0 -0.75 0.58\n*\n"


class _Messenger:
    provider = "discord"

    def __init__(self) -> None:
        self.replies: list[BotReply] = []

    def send_reply(
        self, address: ConversationAddress, reply: BotReply, *, silent: bool = False
    ) -> SendResult:
        del address, silent
        self.replies.append(reply)
        return SendResult(sent=True, provider="discord", message_id=str(len(self.replies)))

    def edit_actions(
        self, address: ConversationAddress, message_id: str, actions: object
    ) -> SendResult:
        del address, actions
        return SendResult(sent=True, provider="discord", message_id=message_id)

    def acknowledge(self, action: IncomingAction, text: str) -> SendResult:
        del action, text
        return SendResult(sent=True, provider="discord")


def _write_config(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir()
    fake_orca = tmp_path / "fake_orca"
    fake_orca.write_text("#!/usr/bin/env bash\necho ok\n")
    fake_orca.chmod(0o755)
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        "\n".join(
            [
                f"runs_root: {runs}",
                "scheduler:",
                "  max_active_simulations: 1",
                f"  admission_root: {runs}/.admission",
                "resources:",
                "  max_cores_per_task: 2",
                "  max_memory_gb_per_task: 4",
                "orca:",
                "  runtime:",
                "    default_max_retries: 0",
                "  paths:",
                f"    orca_executable: {fake_orca}",
                "messenger:",
                "  provider: discord",
                "  discord:",
                "    bot_token: x",
                '    channel_ids: ["100"]',
                '    allowed_user_ids: ["42"]',
                "    uploads:",
                "      enabled: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config


def test_uploaded_orca_run_dir_enqueues_under_its_name(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    runs = tmp_path / "runs"

    archive = tmp_path / "water_opt.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("water_opt/water_opt.inp", _WATER_INP)

    app = BotApplication(
        settings=settings_from_config(str(config)),
        upload_policy=UploadPolicy(enabled=True),
    )

    staged = app.stage_upload_path("water_opt.zip")
    staged.write_bytes(archive.read_bytes())
    upload = IncomingUpload(
        address=ADDRESS,
        actor=ACTOR,
        filename="water_opt.zip",
        size=staged.stat().st_size,
        archive_path=str(staged),
    )

    messenger = _Messenger()
    assert app.dispatch_upload(upload, messenger=messenger) == "upload-confirmation-sent"
    confirm_id = messenger.replies[-1].actions[0][0].action_id

    action = IncomingAction(
        address=ADDRESS, actor=ACTOR, action_id=confirm_id, ack_token="t", message_id="1"
    )
    assert app.dispatch_action(action, messenger=messenger) == "run-submitted"
    assert "Queued water_opt" in messenger.replies[-1].text

    # The run-dir was materialized under its archive directory name...
    assert (runs / "water_opt" / "water_opt.inp").exists()
    # ...and a real pending queue entry was written for it.
    queue_files = list(runs.rglob("queue.json"))
    assert queue_files, "expected the submission to write a queue.json"
    entries = json.loads(queue_files[0].read_text(encoding="utf-8"))
    assert any(
        entry.get("engine") == "orca" and entry.get("task_kind") == "orca_run_inp"
        for entry in entries
    )
