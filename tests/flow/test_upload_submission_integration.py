"""End-to-end: an uploaded archive, once confirmed, reaches the real queue.

This exercises the direct submission API (not a stub), so it guards the typed
upload adapter contract in ``_submit_extracted_run_dir`` against drift and proves
the extracted run-dir enqueues under its directory name.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from orca_auto.core.ingest import UploadPolicy
from orca_auto.core.messaging.channel import SendResult
from orca_auto.core.messaging.interactive import (
    Actor,
    BotReply,
    ConversationAddress,
    IncomingAction,
    IncomingUpload,
)
from orca_auto.flow.bot import UploadApplication, settings_from_config

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

    app = UploadApplication(
        settings=settings_from_config(str(config)),
        upload_policy=UploadPolicy(enabled=True),
    )

    reservation = app.reserve_upload(
        address=ADDRESS,
        actor=ACTOR,
        message_id="message:water_opt.zip",
        attachment_ids=("attachment:water_opt.zip",),
        expected_bytes=archive.stat().st_size,
    )
    reservation.session.archive_path.write_bytes(archive.read_bytes())
    session = app.finalize_upload(reservation.session.upload_id)
    upload = IncomingUpload(
        address=ADDRESS,
        actor=ACTOR,
        filename="water_opt.zip",
        size=session.actual_bytes or 0,
        archive_path=str(session.archive_path),
        message_id=session.message_id,
        attachment_id="attachment:water_opt.zip",
        upload_id=session.upload_id,
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


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_postcommit_notification_exception_returns_queue_receipt_and_preserves_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    from orca_auto.orca import submission

    config = _write_config(tmp_path)
    runs = tmp_path / "runs"
    job_dir = runs / "postcommit"
    job_dir.mkdir()
    (job_dir / "postcommit.inp").write_text(_WATER_INP, encoding="utf-8")
    app = UploadApplication(
        settings=settings_from_config(str(config)),
        upload_policy=UploadPolicy(enabled=True),
    )

    def _raise_after_enqueue(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise error_type("notification transport failed")

    monkeypatch.setattr(submission, "notify_queue_enqueued_event", _raise_after_enqueue)

    receipt = app._submit_extracted_run_dir(job_dir, run_dir_kind="orca")

    assert receipt.committed is True
    assert receipt.kind == "orca"
    assert receipt.submission_id
    assert "notification transport failed" in receipt.detail
    assert job_dir.is_dir()
    entries = json.loads((runs / "queue.json").read_text(encoding="utf-8"))
    assert any(entry.get("queue_id") == receipt.submission_id for entry in entries)


def test_existing_queue_entry_reconciles_as_committed_receipt(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    runs = tmp_path / "runs"
    job_dir = runs / "idempotent"
    job_dir.mkdir()
    (job_dir / "idempotent.inp").write_text(_WATER_INP, encoding="utf-8")
    app = UploadApplication(
        settings=settings_from_config(str(config)),
        upload_policy=UploadPolicy(enabled=True),
    )

    first = app._submit_extracted_run_dir(job_dir, run_dir_kind="orca")
    repeated = app._submit_extracted_run_dir(job_dir, run_dir_kind="orca")

    assert first.committed is True
    assert repeated.committed is True
    assert repeated.submission_id == first.submission_id
    assert repeated.failure_reason == "submission_conflict"
    assert job_dir.is_dir()
