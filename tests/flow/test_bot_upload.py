"""Application-level tests for the upload → confirm → submit flow."""

from __future__ import annotations

import shutil
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
from orca_auto.flow.bot import ActionRegistry, BotApplication, BotSettings

ADDRESS = ConversationAddress(provider="discord", channel_id="100")
ACTOR = Actor(user_id="42", label="chemist")


class FakeMessenger:
    provider = "discord"

    def __init__(self) -> None:
        self.replies: list[BotReply] = []
        self.acks: list[str] = []

    def send_reply(
        self, address: ConversationAddress, reply: BotReply, *, silent: bool = False
    ) -> SendResult:
        del address, silent
        self.replies.append(reply)
        return SendResult(sent=True, provider=self.provider, message_id=str(len(self.replies)))

    def edit_actions(
        self, address: ConversationAddress, message_id: str, actions: object
    ) -> SendResult:
        del address, actions
        return SendResult(sent=True, provider=self.provider, message_id=message_id)

    def acknowledge(self, action: IncomingAction, text: str) -> SendResult:
        del action
        self.acks.append(text)
        return SendResult(sent=True, provider=self.provider)


def _app(tmp_path: Path, *, enabled: bool = True) -> BotApplication:
    settings = BotSettings(
        workflow_root=str(tmp_path),
        crest_config=None,
        xtb_config=None,
        orca_config=None,
        orca_repo_root=None,
        runs_root=str(tmp_path),
    )
    return BotApplication(
        settings=settings,
        actions=ActionRegistry(),
        upload_policy=UploadPolicy(enabled=enabled),
    )


def _make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def _stage(app: BotApplication, archive: Path, filename: str) -> IncomingUpload:
    staged = app.stage_upload_path(filename)
    shutil.copy(archive, staged)
    return IncomingUpload(
        address=ADDRESS,
        actor=ACTOR,
        filename=filename,
        size=staged.stat().st_size,
        archive_path=str(staged),
    )


def _confirm_action_id(reply: BotReply) -> str:
    return reply.actions[0][0].action_id


def _dismiss_action_id(reply: BotReply) -> str:
    return reply.actions[0][1].action_id


def test_upload_sends_confirmation(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"mol42/job.inp": b"! r2scan-3c\n"})
    app = _app(tmp_path)
    messenger = FakeMessenger()

    status = app.dispatch_upload(_stage(app, archive, "mol42.zip"), messenger=messenger)

    assert status == "upload-confirmation-sent"
    reply = messenger.replies[-1]
    assert "Queue mol42?" in reply.text
    assert "orca" in reply.text
    assert len(reply.actions[0]) == 2


def test_upload_disabled_is_refused(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"job.inp": b"x"})
    app = _app(tmp_path, enabled=False)
    messenger = FakeMessenger()

    upload = _stage(app, archive, "mol42.zip")
    status = app.dispatch_upload(upload, messenger=messenger)

    assert status == "upload-disabled"
    assert "disabled" in messenger.replies[-1].text
    # Refused uploads leave nothing staged.
    assert not Path(upload.archive_path).exists()


def test_upload_rejects_bad_archive_and_cleans_up(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "evil.zip", {"../escape.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()

    upload = _stage(app, archive, "evil.zip")
    status = app.dispatch_upload(upload, messenger=messenger)

    assert status == "upload-rejected"
    assert "Rejected" in messenger.replies[-1].text
    assert not Path(upload.archive_path).exists()


def test_confirm_extracts_and_submits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _make_zip(
        tmp_path / "mol42.zip",
        {"mol42/job.inp": b"! r2scan-3c\n", "mol42/geo.xyz": b"1\n\nH 0 0 0\n"},
    )
    app = _app(tmp_path)
    messenger = FakeMessenger()

    submitted: list[Path] = []

    def _fake_submit(job_dir: Path) -> tuple[bool, str]:
        submitted.append(job_dir)
        return True, "ok"

    monkeypatch.setattr(app, "_submit_extracted_run_dir", _fake_submit)

    upload = _stage(app, archive, "mol42.zip")
    app.dispatch_upload(upload, messenger=messenger)
    confirm_id = _confirm_action_id(messenger.replies[-1])

    action = IncomingAction(
        address=ADDRESS, actor=ACTOR, action_id=confirm_id, ack_token="tok", message_id="1"
    )
    status = app.dispatch_action(action, messenger=messenger)

    assert status == "run-submitted"
    assert submitted == [tmp_path / "mol42"]
    assert (tmp_path / "mol42" / "job.inp").exists()
    assert "Queued mol42" in messenger.replies[-1].text
    # Staged archive is consumed.
    assert not Path(upload.archive_path).exists()


def test_confirm_cleans_up_extracted_dir_on_submission_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"mol42/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()

    monkeypatch.setattr(app, "_submit_extracted_run_dir", lambda job_dir: (False, "boom"))

    upload = _stage(app, archive, "mol42.zip")
    app.dispatch_upload(upload, messenger=messenger)
    confirm_id = _confirm_action_id(messenger.replies[-1])

    action = IncomingAction(
        address=ADDRESS, actor=ACTOR, action_id=confirm_id, ack_token="tok", message_id="1"
    )
    app.dispatch_action(action, messenger=messenger)

    assert "Submission failed" in messenger.replies[-1].text
    # The freshly-extracted run-dir must not be stranded in runs_root.
    assert not (tmp_path / "mol42").exists()
    assert not Path(upload.archive_path).exists()


def test_dismiss_discards_staged_archive(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"mol42/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()

    upload = _stage(app, archive, "mol42.zip")
    app.dispatch_upload(upload, messenger=messenger)
    dismiss_id = _dismiss_action_id(messenger.replies[-1])

    action = IncomingAction(
        address=ADDRESS, actor=ACTOR, action_id=dismiss_id, ack_token="tok", message_id="1"
    )
    status = app.dispatch_action(action, messenger=messenger)

    assert status == "run-dismissed"
    assert not Path(upload.archive_path).exists()
    assert not (tmp_path / "mol42").exists()


def test_confirm_after_staged_archive_gone(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"mol42/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()

    upload = _stage(app, archive, "mol42.zip")
    app.dispatch_upload(upload, messenger=messenger)
    confirm_id = _confirm_action_id(messenger.replies[-1])

    # Simulate the staging file vanishing before confirmation.
    Path(upload.archive_path).unlink()

    action = IncomingAction(
        address=ADDRESS, actor=ACTOR, action_id=confirm_id, ack_token="tok", message_id="1"
    )
    app.dispatch_action(action, messenger=messenger)
    assert "expired" in messenger.replies[-1].text.lower()


def test_run_command_usage_without_attachment(tmp_path: Path) -> None:
    from orca_auto.core.messaging.interactive import IncomingCommand

    app = _app(tmp_path)
    messenger = FakeMessenger()
    command = IncomingCommand(address=ADDRESS, actor=ACTOR, command="run", args="")

    status = app.dispatch_command(command, messenger=messenger)

    assert status == "run-usage"
    assert "Attach" in messenger.replies[-1].text
