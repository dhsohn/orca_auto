"""Application-level tests for the upload → confirm → submit flow."""

from __future__ import annotations

import json
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.ingest import UploadPolicy, UploadState
from orca_auto.core.messaging import render_discord_embed
from orca_auto.core.messaging.channel import SendResult
from orca_auto.core.messaging.interactive import (
    Actor,
    BotReply,
    ConversationAddress,
    IncomingAction,
    IncomingUpload,
)
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.flow.bot import ActionRegistry, BotApplication, BotSettings, remote_admission
from orca_auto.flow.bot.application import SubmissionReceipt

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


class FailingMessenger(FakeMessenger):
    def send_reply(
        self, address: ConversationAddress, reply: BotReply, *, silent: bool = False
    ) -> SendResult:
        del address, silent
        self.replies.append(reply)
        return SendResult(sent=False, error="transport failed", provider=self.provider)


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
    if app.upload_policy is None or not app.upload_policy.enabled:
        staged = app.stage_upload_path(filename)
        shutil.copy(archive, staged)
        return IncomingUpload(
            address=ADDRESS,
            actor=ACTOR,
            filename=filename,
            size=staged.stat().st_size,
            archive_path=str(staged),
        )
    attachment_id = f"attachment:{filename}"
    reservation = app.reserve_upload(
        address=ADDRESS,
        actor=ACTOR,
        message_id=f"message:{filename}",
        attachment_ids=(attachment_id,),
        expected_bytes=archive.stat().st_size,
    )
    shutil.copy(archive, reservation.session.archive_path)
    session = app.finalize_upload(reservation.session.upload_id)
    return IncomingUpload(
        address=ADDRESS,
        actor=ACTOR,
        filename=filename,
        size=session.actual_bytes or 0,
        archive_path=str(session.archive_path),
        message_id=session.message_id,
        attachment_id=attachment_id,
        upload_id=session.upload_id,
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


def test_reservation_is_idempotent_before_download(tmp_path: Path) -> None:
    app = _app(tmp_path)

    first = app.reserve_upload(
        address=ADDRESS,
        actor=ACTOR,
        message_id="message-7",
        attachment_ids=("attachment-7",),
        expected_bytes=42,
    )
    retry = app.reserve_upload(
        address=ADDRESS,
        actor=ACTOR,
        message_id="message-7",
        attachment_ids=("attachment-7",),
        expected_bytes=42,
    )

    assert first.created is True
    assert retry.created is False
    assert retry.session.upload_id == first.session.upload_id
    app.abandon_upload(first.session.upload_id, "test complete")


def test_confirmation_delivery_failure_discards_durable_session(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"mol42/job.inp": b"x"})
    app = _app(tmp_path)
    upload = _stage(app, archive, "mol42.zip")

    status = app.dispatch_upload(upload, messenger=FailingMessenger())

    assert status == "upload-confirmation-sent-delivery-failed"
    assert app.upload_sessions is not None
    session = app.upload_sessions.get(upload.upload_id or "")
    assert session.state is UploadState.DISCARDED
    assert not session.archive_path.parent.exists()


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


def test_disabled_upload_never_unlinks_an_unowned_caller_path(tmp_path: Path) -> None:
    app = _app(tmp_path, enabled=False)
    messenger = FakeMessenger()
    unowned = tmp_path / "service-owned.json"
    unowned.write_text("important", encoding="utf-8")
    upload = IncomingUpload(
        address=ADDRESS,
        actor=ACTOR,
        filename="upload.zip",
        size=unowned.stat().st_size,
        archive_path=str(unowned),
    )

    assert app.dispatch_upload(upload, messenger=messenger) == "upload-disabled"
    assert unowned.read_text(encoding="utf-8") == "important"


def test_upload_rejects_runtime_reserved_published_name(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "queue.json.zip", {"job.inp": b"! r2scan-3c\n"})
    app = _app(tmp_path)
    messenger = FakeMessenger()

    status = app.dispatch_upload(_stage(app, archive, "queue.json.zip"), messenger=messenger)

    assert status == "upload-rejected"
    assert "reserved for runtime state" in messenger.replies[-1].text
    assert not (tmp_path / "queue.json").exists()


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

    def _fake_submit(job_dir: Path, *, run_dir_kind: str | None = None) -> SubmissionReceipt:
        submitted.append(job_dir)
        return SubmissionReceipt(True, "q-test", "", run_dir_kind or "unknown")  # type: ignore[arg-type]

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
    # The rich success reply keeps exposing the submission id operators track by.
    success_message = messenger.replies[-1].message
    assert success_message is not None
    success_embed = render_discord_embed(success_message)
    assert any(
        field["name"] == "ID" and "q-test" in field["value"] for field in success_embed["fields"]
    )
    # Staged archive is consumed.
    assert not Path(upload.archive_path).exists()


def test_confirmation_action_survives_application_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _make_zip(tmp_path / "restart.zip", {"restart/job.inp": b"x"})
    first_app = _app(tmp_path)
    messenger = FakeMessenger()
    upload = _stage(first_app, archive, "restart.zip")
    assert first_app.dispatch_upload(upload, messenger=messenger) == "upload-confirmation-sent"
    confirm_id = _confirm_action_id(messenger.replies[-1])

    restarted = _app(tmp_path)
    monkeypatch.setattr(
        restarted,
        "_submit_extracted_run_dir",
        lambda job_dir, **kwargs: SubmissionReceipt(True, "q-restart", "", "orca"),
    )
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=confirm_id,
        ack_token="restart-token",
        message_id="confirmation-message",
    )

    assert restarted.dispatch_action(action, messenger=messenger) == "run-submitted"
    assert restarted.upload_sessions is not None
    session = restarted.upload_sessions.get(upload.upload_id or "")
    assert session.state is UploadState.COMMITTED
    assert session.receipt is not None
    assert session.receipt.queue_id == "q-restart"
    assert (tmp_path / "restart" / "job.inp").is_file()
    assert restarted.dispatch_upload(upload, messenger=messenger) == "upload-already-submitted"


def test_two_same_named_uploads_publish_without_deleting_each_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_archive = _make_zip(tmp_path / "first.zip", {"same/job.inp": b"first"})
    second_archive = _make_zip(tmp_path / "second.zip", {"same/job.inp": b"second"})
    app = _app(tmp_path)
    messenger = FakeMessenger()
    first = _stage(app, first_archive, "first.zip")
    second = _stage(app, second_archive, "second.zip")
    app.dispatch_upload(first, messenger=messenger)
    first_action = _confirm_action_id(messenger.replies[-1])
    app.dispatch_upload(second, messenger=messenger)
    second_action = _confirm_action_id(messenger.replies[-1])

    monkeypatch.setattr(
        app,
        "_submit_extracted_run_dir",
        lambda job_dir, **kwargs: SubmissionReceipt(True, f"q-{job_dir.name}", "", "orca"),
    )

    def submit(action_id: str) -> str:
        return app.dispatch_action(
            IncomingAction(
                address=ADDRESS,
                actor=ACTOR,
                action_id=action_id,
                ack_token=action_id,
            ),
            messenger=messenger,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, (first_action, second_action)))

    assert statuses == ["run-submitted", "run-submitted"]
    contents = {
        (tmp_path / "same" / "job.inp").read_text(),
        (tmp_path / "same-2" / "job.inp").read_text(),
    }
    assert contents == {"first", "second"}


def test_confirm_cleans_up_extracted_dir_on_submission_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"mol42/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()

    monkeypatch.setattr(
        app,
        "_submit_extracted_run_dir",
        lambda job_dir, **kwargs: SubmissionReceipt(False, None, "boom", "orca"),
    )

    upload = _stage(app, archive, "mol42.zip")
    app.dispatch_upload(upload, messenger=messenger)
    confirm_id = _confirm_action_id(messenger.replies[-1])

    action = IncomingAction(
        address=ADDRESS, actor=ACTOR, action_id=confirm_id, ack_token="tok", message_id="1"
    )
    status = app.dispatch_action(action, messenger=messenger)

    assert status == "run-submission-failed"
    assert "Submission failed" in messenger.replies[-1].text
    # The freshly-extracted run-dir must not be stranded in runs_root.
    assert not (tmp_path / "mol42").exists()
    assert not Path(upload.archive_path).exists()
    assert app.upload_sessions is not None
    assert app.upload_sessions.get(upload.upload_id or "").state is UploadState.FAILED


def test_confirm_preserves_committed_dir_and_reports_postcommit_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"mol42/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()
    monkeypatch.setattr(
        app,
        "_submit_extracted_run_dir",
        lambda job_dir, **kwargs: SubmissionReceipt(
            True, "q-committed", "notification failed", "orca"
        ),
    )

    upload = _stage(app, archive, "mol42.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
        message_id="1",
    )

    status = app.dispatch_action(action, messenger=messenger)

    assert status == "run-submitted-with-warning"
    assert "q-committed" in messenger.replies[-1].text
    assert (tmp_path / "mol42" / "job.inp").is_file()


def test_commit_receipt_persistence_error_is_uncertain_and_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"mol42/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()
    monkeypatch.setattr(
        app,
        "_submit_extracted_run_dir",
        lambda job_dir, **kwargs: SubmissionReceipt(True, "q-durable", "", "orca"),
    )
    assert app.upload_sessions is not None
    monkeypatch.setattr(
        app.upload_sessions,
        "mark_committed",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    upload = _stage(app, archive, "mol42.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
    )

    assert app.dispatch_action(action, messenger=messenger) == "run-submission-uncertain"
    session = app.upload_sessions.get(upload.upload_id or "")
    assert session.state is UploadState.AMBIGUOUS
    assert (tmp_path / "mol42" / "job.inp").is_file()
    assert (tmp_path / "mol42" / ".orca-auto-upload").is_file()


def test_nondurable_publish_is_preserved_and_never_submitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"mol42/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()
    original_publish = app._atomic_publish_upload

    def publish_without_durable_root_sync(
        extracted_dir: Path,
        *,
        job_name: str,
        upload_id: str,
    ) -> tuple[Path, bool]:
        published, _ = original_publish(
            extracted_dir,
            job_name=job_name,
            upload_id=upload_id,
        )
        return published, False

    monkeypatch.setattr(app, "_atomic_publish_upload", publish_without_durable_root_sync)
    monkeypatch.setattr(
        app,
        "_submit_extracted_run_dir",
        lambda *args, **kwargs: pytest.fail("a nondurable publication must not be submitted"),
    )

    upload = _stage(app, archive, "mol42.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
    )

    assert app.dispatch_action(action, messenger=messenger) == "run-submission-uncertain"
    assert app.upload_sessions is not None
    session = app.upload_sessions.get(upload.upload_id or "")
    assert session.state is UploadState.AMBIGUOUS
    assert (tmp_path / "mol42" / "job.inp").is_file()
    assert (tmp_path / "mol42" / ".orca-auto-upload").is_file()


def test_publish_rename_success_then_error_is_never_downgraded_to_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.flow.bot import application as application_module

    archive = _make_zip(tmp_path / "rename_race.zip", {"rename_race/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()

    def replace_then_raise(source: Path, destination: Path) -> None:
        application_module.os.replace(source, destination)
        raise OSError("late filesystem error")

    monkeypatch.setattr(application_module, "_replace_directory", replace_then_raise)
    monkeypatch.setattr(
        app,
        "_submit_extracted_run_dir",
        lambda *args, **kwargs: pytest.fail("an uncertain publication must not be submitted"),
    )

    upload = _stage(app, archive, "rename_race.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
    )

    assert app.dispatch_action(action, messenger=messenger) == "run-submission-uncertain"
    assert app.upload_sessions is not None
    session = app.upload_sessions.get(upload.upload_id or "")
    assert session.state is UploadState.AMBIGUOUS
    assert session.published_path == (tmp_path / "rename_race").resolve()
    assert (tmp_path / "rename_race" / ".orca-auto-upload").is_file()

    restarted = _app(tmp_path)
    assert restarted.upload_sessions is not None
    assert restarted.upload_sessions.get(upload.upload_id or "").state is UploadState.AMBIGUOUS
    assert (tmp_path / "rename_race" / "job.inp").is_file()


def test_failed_publish_cleanup_is_state_first_and_retried_on_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"mol42/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()
    monkeypatch.setattr(
        app,
        "_submit_extracted_run_dir",
        lambda job_dir, **kwargs: SubmissionReceipt(False, None, "rejected", "orca"),
    )
    monkeypatch.setattr(app, "_remove_owned_published_upload", lambda *args: False)

    upload = _stage(app, archive, "mol42.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
    )

    assert app.dispatch_action(action, messenger=messenger) == "run-submission-failed"
    assert app.upload_sessions is not None
    session = app.upload_sessions.get(upload.upload_id or "")
    assert session.state is UploadState.FAILED
    assert session.published_path == (tmp_path / "mol42").resolve()
    assert (tmp_path / "mol42" / ".orca-auto-upload").is_file()

    restarted = _app(tmp_path)
    assert restarted.upload_sessions is not None
    assert restarted.upload_sessions.get(upload.upload_id or "").state is UploadState.FAILED
    assert not (tmp_path / "mol42").exists()


def test_failed_state_persistence_error_never_authorizes_public_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"mol42/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()
    monkeypatch.setattr(
        app,
        "_submit_extracted_run_dir",
        lambda job_dir, **kwargs: SubmissionReceipt(False, None, "rejected", "orca"),
    )

    upload = _stage(app, archive, "mol42.zip")
    app.dispatch_upload(upload, messenger=messenger)
    assert app.upload_sessions is not None
    monkeypatch.setattr(
        app.upload_sessions,
        "mark_failed",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("state disk unavailable")),
    )
    monkeypatch.setattr(
        app,
        "_remove_owned_published_upload",
        lambda *args, **kwargs: pytest.fail("cleanup requires durable FAILED state"),
    )
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
    )

    assert app.dispatch_action(action, messenger=messenger) == "run-submission-failed"
    session = app.upload_sessions.get(upload.upload_id or "")
    assert session.state is UploadState.AMBIGUOUS
    assert (tmp_path / "mol42" / "job.inp").is_file()
    assert (tmp_path / "mol42" / ".orca-auto-upload").is_file()


def test_confirm_preserves_dir_when_commit_outcome_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _make_zip(tmp_path / "mol42.zip", {"mol42/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()
    monkeypatch.setattr(
        app,
        "_submit_extracted_run_dir",
        lambda job_dir, **kwargs: SubmissionReceipt(None, None, "transport failed", "orca"),
    )

    upload = _stage(app, archive, "mol42.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
        message_id="1",
    )

    status = app.dispatch_action(action, messenger=messenger)

    assert status == "run-submission-uncertain"
    assert (tmp_path / "mol42" / "job.inp").is_file()
    assert app.upload_sessions is not None
    session = app.upload_sessions.get(upload.upload_id or "")
    assert session.state is UploadState.AMBIGUOUS
    assert session.published_path == (tmp_path / "mol42").resolve()


def test_startup_sweep_recovers_publish_before_state_persistence(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "crash.zip", {"crash/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()
    upload = _stage(app, archive, "crash.zip")
    app.dispatch_upload(upload, messenger=messenger)
    confirm_id = _confirm_action_id(messenger.replies[-1])
    assert app.upload_sessions is not None
    app.upload_sessions.consume_action(
        confirm_id,
        binding=app._upload_binding(ADDRESS, ACTOR),
    )

    published = tmp_path / "crash"
    published.mkdir()
    (published / "job.inp").write_text("x", encoding="utf-8")
    (published / ".orca-auto-upload").write_text(
        f"{upload.upload_id}\n",
        encoding="ascii",
    )

    restarted = _app(tmp_path)
    assert restarted.upload_sessions is not None
    recovered = restarted.upload_sessions.get(upload.upload_id or "")
    assert recovered.state is UploadState.AMBIGUOUS
    assert recovered.published_path == published.resolve()
    assert published.is_dir()


def test_startup_sweep_recovers_commit_before_receipt_persistence(tmp_path: Path) -> None:
    archive = _make_zip(
        tmp_path / "flow_crash.zip",
        {
            "flow_crash/flow.yaml": b"workflow_type: conformer_screening\n",
            "flow_crash/input.xyz": b"1\n\nH 0 0 0\n",
        },
    )
    app = _app(tmp_path)
    messenger = FakeMessenger()
    upload = _stage(app, archive, "flow_crash.zip")
    app.dispatch_upload(upload, messenger=messenger)
    confirm_id = _confirm_action_id(messenger.replies[-1])
    assert app.upload_sessions is not None
    app.upload_sessions.consume_action(
        confirm_id,
        binding=app._upload_binding(ADDRESS, ACTOR),
    )

    published = tmp_path / "flow_crash"
    published.mkdir()
    (published / "flow.yaml").write_text(
        "workflow_type: conformer_screening\n",
        encoding="utf-8",
    )
    (published / "workflow.json").write_text(
        '{"workflow_id": "wf-crash"}',
        encoding="utf-8",
    )
    (published / ".orca-auto-upload").write_text(
        f"{upload.upload_id}\n",
        encoding="ascii",
    )

    restarted = _app(tmp_path)
    assert restarted.upload_sessions is not None
    recovered = restarted.upload_sessions.get(upload.upload_id or "")
    assert recovered.state is UploadState.COMMITTED
    assert recovered.receipt is not None
    assert recovered.receipt.workflow_id == "wf-crash"
    assert published.is_dir()
    assert not (published / ".orca-auto-upload").exists()


def test_sweep_rejects_marker_for_a_different_recorded_publish_path(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "bound.zip", {"bound/job.inp": b"x"})
    app = _app(tmp_path)
    messenger = FakeMessenger()
    upload = _stage(app, archive, "bound.zip")
    app.dispatch_upload(upload, messenger=messenger)
    confirm_id = _confirm_action_id(messenger.replies[-1])
    assert app.upload_sessions is not None
    app.upload_sessions.consume_action(
        confirm_id,
        binding=app._upload_binding(ADDRESS, ACTOR),
    )

    recorded = tmp_path / "recorded"
    recorded.mkdir()
    (recorded / "job.inp").write_text("x", encoding="utf-8")
    app.upload_sessions.mark_published(
        upload.upload_id or "",
        published_path=recorded,
    )

    tampered = tmp_path / "tampered"
    tampered.mkdir()
    (tampered / "workflow.json").write_text(
        '{"workflow_id": "wrong-workflow"}',
        encoding="utf-8",
    )
    (tampered / ".orca-auto-upload").write_text(
        f"{upload.upload_id}\n",
        encoding="ascii",
    )

    app.sweep_upload_sessions()

    session = app.upload_sessions.get(upload.upload_id or "")
    assert session.state is UploadState.AMBIGUOUS
    assert session.published_path == recorded.resolve()
    assert session.receipt is None
    assert (tampered / ".orca-auto-upload").is_file()


@pytest.mark.parametrize(
    "forbidden_manifest",
    [
        "workflow_root: /tmp/escape\n",
        "workflow:\n  root: /tmp/escape\n",
        "allow_external_inputs: true\n",
    ],
)
def test_confirm_rejects_server_owned_workflow_fields(
    tmp_path: Path,
    forbidden_manifest: str,
) -> None:
    archive = _make_zip(
        tmp_path / "untrusted.zip",
        {
            "untrusted/flow.yaml": (
                "workflow_type: conformer_screening\n" + forbidden_manifest
            ).encode(),
            "untrusted/input.xyz": b"1\n\nH 0 0 0\n",
        },
    )
    app = _app(tmp_path)
    messenger = FakeMessenger()

    upload = _stage(app, archive, "untrusted.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
        message_id="1",
    )

    status = app.dispatch_action(action, messenger=messenger)

    assert status == "run-rejected"
    assert "server-owned fields" in messenger.replies[-1].text
    assert not (tmp_path / "untrusted").exists()


def test_confirm_rejects_remote_crest_solvent_shell_token(tmp_path: Path) -> None:
    archive = _make_zip(
        tmp_path / "solvent_injection.zip",
        {
            "solvent_injection/flow.yaml": (
                b"workflow_type: conformer_screening\n"
                b"crest:\n"
                b"  solvent_model: gbsa\n"
                b'  solvent: "water;touch"\n'
            ),
            "solvent_injection/input.xyz": b"2\nH2\nH 0 0 0\nH 0 0 0.7\n",
        },
    )
    app = _app(tmp_path)
    messenger = FakeMessenger()
    upload = _stage(app, archive, "solvent_injection.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
        message_id="1",
    )

    status = app.dispatch_action(action, messenger=messenger)

    assert status == "run-submission-failed"
    assert not (tmp_path / "solvent_injection").exists()


def test_confirm_rejects_remote_xtb_solvent_shell_token(tmp_path: Path) -> None:
    archive = _make_zip(
        tmp_path / "xtb_solvent_injection.zip",
        {
            "xtb_solvent_injection/flow.yaml": (
                b"workflow_type: reaction_ts_search\n"
                b"xtb:\n"
                b"  solvent_model: gbsa\n"
                b'  solvent: "water;touch"\n'
            ),
            "xtb_solvent_injection/reactant.xyz": b"2\nH2\nH 0 0 0\nH 0 0 0.7\n",
            "xtb_solvent_injection/product.xyz": b"2\nH2\nH 0 0 0\nH 0 0 0.8\n",
        },
    )
    app = _app(tmp_path)
    messenger = FakeMessenger()
    upload = _stage(app, archive, "xtb_solvent_injection.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
        message_id="1",
    )

    status = app.dispatch_action(action, messenger=messenger)

    assert status == "run-submission-failed"
    assert not (tmp_path / "xtb_solvent_injection").exists()


def test_confirm_submits_workflow_upload_through_real_creation_path(tmp_path: Path) -> None:
    """Regression: uploads publish directly under runs_root, so the real
    (unmocked) workflow creation path must mint a fresh prefixed workspace
    instead of colliding with the published run-dir."""

    archive = _make_zip(
        tmp_path / "conf_case.zip",
        {
            "conf_case/flow.yaml": b"workflow_type: conformer_screening\n",
            "conf_case/input.xyz": b"2\nH2\nH 0 0 0\nH 0 0 0.7\n",
        },
    )
    app = _app(tmp_path)
    messenger = FakeMessenger()
    upload = _stage(app, archive, "conf_case.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
        message_id="1",
    )

    status = app.dispatch_action(action, messenger=messenger)

    assert status == "run-submitted"
    published_dir = tmp_path / "conf_case"
    generations = [
        item
        for item in published_dir.iterdir()
        if item.is_dir() and is_visible_generation_name(item.name)
    ]
    assert len(generations) == 1
    workspace_dir = generations[0]
    assert (workspace_dir / "workflow.json").is_file()
    assert workspace_dir.name in messenger.replies[-1].text


def test_confirm_rejects_standalone_orca_resources_above_server_cap(tmp_path: Path) -> None:
    archive = _make_zip(
        tmp_path / "oversized.zip",
        {"oversized/job.inp": b"! r2scan-3c PAL999\n%maxcore 999999\n"},
    )
    app = _app(tmp_path)
    messenger = FakeMessenger()

    upload = _stage(app, archive, "oversized.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
        message_id="1",
    )

    status = app.dispatch_action(action, messenger=messenger)

    assert status == "run-submission-failed"
    assert not (tmp_path / "oversized").exists()


@pytest.mark.parametrize(
    "directive",
    [
        "% maxcore 999999",
        "% pal nprocs 999 end",
        "# hidden # % maxcore 999999",
        "# hidden # ! PAL999",
    ],
)
def test_standalone_orca_resource_caps_cover_spaced_percent_syntax(
    tmp_path: Path,
    directive: str,
) -> None:
    job_dir = tmp_path / "spaced_resource"
    job_dir.mkdir()
    (job_dir / "job.inp").write_text(
        f"! r2scan-3c\n{directive}\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="server limit"):
        remote_admission.validate_orca_resource_limits(
            job_dir,
            max_cores=4,
            max_memory_gb=8,
        )


@pytest.mark.parametrize(
    "input_text",
    [
        "! r2scan-3c\n* xyzfile 0 1 ../../secret.xyz\n",
        '! r2scan-3c\n%moinp "/etc/passwd"\n* xyz 0 1\nH 0 0 0\n*\n',
        '! r2scan-3c\n%pointcharges "../outside.pc"\n* xyz 0 1\nH 0 0 0\n*\n',
        ('! r2scan-3c\n%geom\n  InHessName "../outside.hess"\nend\n* xyz 0 1\nH 0 0 0\n*\n'),
        '! r2scan-3c\n%base "../../overwrite"\n* xyz 0 1\nH 0 0 0\n*\n',
        '! r2scan-3c\n%unknown "/tmp/external.dat"\n* xyz 0 1\nH 0 0 0\n*\n',
        '! r2scan-3c\n%moinp "..\\outside.gbw"\n* xyz 0 1\nH 0 0 0\n*\n',
        '! r2scan-3c\n% pointcharges "missing.pc"\n* xyz 0 1\nH 0 0 0\n*\n',
        '! r2scan-3c\n% base "job;id"\n* xyz 0 1\nH 0 0 0\n*\n',
        '! r2scan-3c\n% base "job$(id)"\n* xyz 0 1\nH 0 0 0\n*\n',
    ],
    ids=[
        "xyzfile-traversal",
        "absolute-moinp",
        "pointcharges-traversal",
        "inhess-traversal",
        "base-output-traversal",
        "unknown-absolute-reference",
        "backslash-traversal",
        "spaced-pointcharges-missing",
        "spaced-base-shell-separator",
        "spaced-base-command-substitution",
    ],
)
def test_confirm_rejects_external_orca_file_references(
    tmp_path: Path,
    input_text: str,
) -> None:
    archive = _make_zip(
        tmp_path / "external_refs.zip",
        {"external_refs/job.inp": input_text.encode()},
    )
    app = _app(tmp_path)
    messenger = FakeMessenger()

    upload = _stage(app, archive, "external_refs.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
        message_id="1",
    )

    assert app.dispatch_action(action, messenger=messenger) == "run-submission-failed"
    assert not (tmp_path / "external_refs").exists()


@pytest.mark.parametrize(
    "input_text",
    [
        '%compound\n  SYS_CMD "touch /tmp/owned"\nend\n',
        '%compound "payload.txt"\n',
        ('! ExtOpt\n%method\n  ProgExt "/bin/sh"\n  Ext_Params "-c id"\nend\n'),
        "!ExtOpt\n* xyz 0 1\nH 0 0 0\n*\n",
        '%method\n  ProgSCF "sh"\nend\n',
        '%xtb\n  XTBINPUTSTRING "--input arbitrary"\nend\n',
        "%md\n  Run 999999999\nend\n",
        "% md\n  Run 999999999\nend\n",
        "! MD\n* xyz 0 1\nH 0 0 0\n*\n",
        "# hidden # ! MD\n* xyz 0 1\nH 0 0 0\n*\n",
        "# hidden # !Compound\n* xyz 0 1\nH 0 0 0\n*\n",
        "! r2scan-3c\n$new_job\n! r2scan-3c\n",
        "! r2scan-3c GCP(FILE)\n",
        '%eda\n  Frag1_MethodFile "nested.txt"\nend\n',
        '%qmmm\n  QM2CustomFile "nested.txt"\nend\n',
    ],
    ids=[
        "compound-system-command",
        "compound-include",
        "external-optimizer",
        "external-optimizer-no-space",
        "program-override",
        "external-xtb-options",
        "molecular-dynamics-block",
        "spaced-molecular-dynamics-block",
        "molecular-dynamics-route",
        "closed-comment-molecular-dynamics-route",
        "closed-comment-compound-route",
        "multiple-jobs",
        "external-gcp-parameters",
        "eda-input-include",
        "qmmm-input-include",
    ],
)
def test_confirm_rejects_remote_orca_execution_and_include_features(
    tmp_path: Path,
    input_text: str,
) -> None:
    archive = _make_zip(
        tmp_path / "unsafe_orca.zip",
        {
            "unsafe_orca/job.inp": input_text.encode(),
            "unsafe_orca/payload.txt": b'SYS_CMD "id"\n',
            "unsafe_orca/nested.txt": b"! ExtOpt\n",
        },
    )
    app = _app(tmp_path)
    messenger = FakeMessenger()

    upload = _stage(app, archive, "unsafe_orca.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
        message_id="1",
    )

    assert app.dispatch_action(action, messenger=messenger) == "run-submission-failed"
    assert not (tmp_path / "unsafe_orca").exists()


def test_standalone_orca_allows_existing_nested_file_references(tmp_path: Path) -> None:
    job_dir = tmp_path / "contained"
    assets = job_dir / "assets"
    assets.mkdir(parents=True)
    for filename in ("progress.gbw", "progress.hess"):
        (assets / filename).write_text("contained\n", encoding="utf-8")
    (assets / "progress.xyz").write_text("1\ncontained\nH 0 0 0\n", encoding="utf-8")
    (job_dir / "job.inp").write_text(
        "\n".join(
            (
                "! r2scan-3c PAL2",
                "%moinp assets/progress.gbw",
                '% base "result"',
                "%geom",
                "  InHessName assets/progress.hess",
                "end",
                "* xyzfile 0 1 assets/progress.xyz",
            )
        ),
        encoding="utf-8",
    )

    remote_admission.validate_orca_resource_limits(
        job_dir,
        max_cores=4,
        max_memory_gb=8,
    )


def test_standalone_orca_allows_builtin_gcpmethod(tmp_path: Path) -> None:
    job_dir = tmp_path / "builtin-gcp"
    job_dir.mkdir()
    (job_dir / "job.inp").write_text(
        '! SP\n%method GCPMETHOD "dft/svp" end\n* xyz 0 1\nH 0 0 0\n*\n',
        encoding="utf-8",
    )

    remote_admission.validate_orca_resource_limits(
        job_dir,
        max_cores=4,
        max_memory_gb=8,
    )


@pytest.mark.parametrize(
    ("failure_reason", "expected_commit"),
    [
        ("invalid_submission_input", None),
        ("unexpected_future_reason", None),
        ("invalid_submission_target", False),
    ],
)
def test_empty_queue_reconciliation_is_cleanup_safe_only_for_precommit_target_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_reason: str,
    expected_commit: bool | None,
) -> None:
    from orca_auto.orca.commands import run_inp
    from orca_auto.orca.commands.run_inp_submission import DirectQueueSubmission

    job_dir = tmp_path / "submission_boundary"
    job_dir.mkdir()
    (job_dir / "job.inp").write_text(
        "! r2scan-3c\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )
    app = _app(tmp_path)
    monkeypatch.setattr(
        run_inp,
        "submit_reaction_dir_to_queue",
        lambda args: DirectQueueSubmission(
            status="failed",
            reason=failure_reason,
            stderr="submission returned failure",
        ),
    )
    monkeypatch.setattr(app, "_orca_entries_for_run_dir", lambda *args: {})

    receipt = app._submit_extracted_run_dir(job_dir, run_dir_kind="orca")

    assert receipt.committed is expected_commit
    assert receipt.failure_reason == failure_reason


def test_confirm_rejects_workflow_resources_above_server_cap(tmp_path: Path) -> None:
    archive = _make_zip(
        tmp_path / "oversized_flow.zip",
        {
            "oversized_flow/flow.yaml": (
                b"workflow_type: conformer_screening\nresources:\n  max_cores: 999\n"
            ),
            "oversized_flow/input.xyz": b"1\n\nH 0 0 0\n",
        },
    )
    app = _app(tmp_path)
    messenger = FakeMessenger()

    upload = _stage(app, archive, "oversized_flow.zip")
    app.dispatch_upload(upload, messenger=messenger)
    action = IncomingAction(
        address=ADDRESS,
        actor=ACTOR,
        action_id=_confirm_action_id(messenger.replies[-1]),
        ack_token="tok",
        message_id="1",
    )

    status = app.dispatch_action(action, messenger=messenger)

    assert status == "run-submission-failed"
    assert not (tmp_path / "oversized_flow").exists()


def test_uploaded_workflow_cannot_override_remote_atom_cap(tmp_path: Path) -> None:
    job_dir = tmp_path / "remote_atom_cap"
    job_dir.mkdir()
    atom_count = 201
    (job_dir / "flow.yaml").write_text(
        "workflow_type: conformer_screening\nmax_atoms: 999999\n",
        encoding="utf-8",
    )
    (job_dir / "input.xyz").write_text(
        f"{atom_count}\nremote molecule\n" + "H 0 0 0\n" * atom_count,
        encoding="utf-8",
    )
    app = _app(tmp_path)

    receipt = app._submit_extracted_run_dir(job_dir, run_dir_kind="workflow")

    assert receipt.committed is False
    assert "atom-count limit of 200" in receipt.detail
    assert not (job_dir / "workflow.json").exists()


def test_uploaded_orca_inline_geometry_uses_remote_atom_cap(tmp_path: Path) -> None:
    job_dir = tmp_path / "remote_orca_atom_cap"
    job_dir.mkdir()
    atom_count = 201
    (job_dir / "job.inp").write_text(
        "! SP\n* xyz 0 1\n" + "H 0 0 0\n" * atom_count + "*\n",
        encoding="utf-8",
    )
    app = _app(tmp_path)

    receipt = app._submit_extracted_run_dir(job_dir, run_dir_kind="orca")

    assert receipt.committed is False
    assert "remote atom-count limit of 200" in receipt.detail


@pytest.mark.parametrize(("atom_count", "accepted"), [(200, True), (201, False)])
def test_uploaded_xyz_remote_atom_cap_boundary(
    tmp_path: Path,
    atom_count: int,
    accepted: bool,
) -> None:
    job_dir = tmp_path / f"xyz-boundary-{atom_count}"
    job_dir.mkdir()
    (job_dir / "input.xyz").write_text(
        f"{atom_count}\nboundary\n" + "H 0 0 0\n" * atom_count,
        encoding="utf-8",
    )

    if accepted:
        assert remote_admission.validate_remote_xyz_atom_limits(job_dir) == atom_count
    else:
        with pytest.raises(ValueError, match="atom-count limit of 200"):
            remote_admission.validate_remote_xyz_atom_limits(job_dir)


@pytest.mark.parametrize(("atom_count", "accepted"), [(200, True), (201, False)])
def test_uploaded_orca_inline_remote_atom_cap_boundary(
    tmp_path: Path,
    atom_count: int,
    accepted: bool,
) -> None:
    inp_path = tmp_path / f"inline-{atom_count}.inp"
    lines = ["! SP", "* xyz 0 1", *("H 0 0 0" for _ in range(atom_count)), "*"]

    if accepted:
        remote_admission.validate_remote_orca_inline_atom_limits(inp_path, lines)
    else:
        with pytest.raises(ValueError, match="remote atom-count limit of 200"):
            remote_admission.validate_remote_orca_inline_atom_limits(inp_path, lines)


@pytest.mark.parametrize(
    "lines",
    [
        ["! Freq", "* int 0 1", *("H 0 0 0" for _ in range(201)), "*"],
        ["! SP", "* internal 0 1", "H 0 0 0", "*"],
        ["! SP", "* gzmtfile 0 1 geometry.gzmt"],
        [
            "! Freq",
            "%coords",
            "  CTyp xyz",
            "  Charge 0",
            "  Mult 1",
            "  coords",
            *("    H 0 0 0" for _ in range(201)),
            "  end",
            "end",
        ],
        ["# hidden # % coords", "  CTyp xyz", "end"],
        ["%compound", "end"],
        ["* xyzfile 0 1"],
    ],
)
def test_uploaded_orca_rejects_unbounded_geometry_formats(
    tmp_path: Path,
    lines: list[str],
) -> None:
    inp_path = tmp_path / "unsupported.inp"

    with pytest.raises(ValueError, match="unsupported|invalid"):
        remote_admission.validate_remote_orca_inline_atom_limits(inp_path, lines)


@pytest.mark.parametrize(
    "directive",
    [
        '%pointcharges "aux.dat"',
        'orcafffilename "aux.dat"',
        'neb_end_pdbfile "aux.dat"',
        'restart_allxyzfile "aux.dat"',
        'GTOName "aux.dat"',
        'ReadFragBasis "aux.dat"',
        'XTBParamFile "aux.dat"',
    ],
)
def test_uploaded_orca_rejects_remote_unbounded_auxiliary_formats(
    tmp_path: Path,
    directive: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    inp_path = job_dir / "job.inp"
    lines = ["! SP", directive, "* xyz 0 1", "H 0 0 0", "*"]
    (job_dir / "aux.dat").write_text("aux", encoding="utf-8")

    with pytest.raises(ValueError, match="remote-disabled"):
        remote_admission.validate_orca_file_references(job_dir, inp_path, lines)


def test_remote_workflow_policy_injects_server_owned_mdlen(tmp_path: Path) -> None:
    job_dir = tmp_path / "remote-md-policy"
    job_dir.mkdir()
    (job_dir / "flow.yaml").write_text(
        "workflow_type: conformer_screening\ncrest:\n  gfn: 2\n",
        encoding="utf-8",
    )

    remote_admission.apply_remote_workflow_crest_policy(job_dir, atom_count=50)

    manifest = remote_admission.uploaded_flow_manifest(job_dir)
    assert manifest["crest"] == {"gfn": 2, "mdlen": 5.0}


def test_remote_workflow_policy_rejects_non_json_yaml_scalar(tmp_path: Path) -> None:
    job_dir = tmp_path / "remote-non-json"
    job_dir.mkdir()
    flow_path = job_dir / "flow.yaml"
    original = "workflow_type: conformer_screening\nmetadata: 2026-01-01\n"
    flow_path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="JSON-compatible"):
        remote_admission.apply_remote_workflow_crest_policy(job_dir, atom_count=10)

    assert flow_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(("atom_count", "accepted"), [(107, True), (108, False)])
def test_remote_crest_atom_step_work_boundary(
    tmp_path: Path,
    atom_count: int,
    accepted: bool,
) -> None:
    job_dir = tmp_path / f"remote-work-{atom_count}"
    job_dir.mkdir()
    (job_dir / "flow.yaml").write_text(
        "workflow_type: conformer_screening\ncrest:\n  gfn: ff\n",
        encoding="utf-8",
    )

    if accepted:
        remote_admission.apply_remote_workflow_crest_policy(job_dir, atom_count=atom_count)
        assert remote_admission.uploaded_flow_manifest(job_dir)["crest"]["mdlen"] == 5.0
    else:
        with pytest.raises(ValueError, match="remote work-unit ceiling"):
            remote_admission.apply_remote_workflow_crest_policy(job_dir, atom_count=atom_count)


@pytest.mark.parametrize(
    "manifest",
    [
        "workflow_type: conformer_screening\nmax_orca_stages: 999\n",
        (
            "workflow_type: conformer_screening\n"
            "orca_route_line: |\n  ! r2scan-3c\n  %pal nprocs 999 end\n"
        ),
        "workflow_type: conformer_screening\norca_route_line: '! Opt PAL999'\n",
        ("workflow_type: scan_ts_search\nscan_coordinate: |\n  B 0 1 = 1.2, 3.0, 16\n  end\n"),
        "workflow_type: scan_ts_search\nscan_coordinate: 'B 0 1 = 1.2, 3.0, 999'\n",
        "workflow_type: conformer_screening\norca_route_line: '!ExtOpt'\n",
        "workflow_type: conformer_screening\norca_route_line: '! Compound'\n",
        "workflow_type: conformer_screening\norca_route_line: '! r2scan-3c MD'\n",
        "workflow_type: conformer_screening\nroute_line: '! r2scan-3c GCP(FILE)'\n",
        # interaction_energy.sp_route_line is a route-line key and MUST be scanned
        # for remote-disabled ORCA features (blocker: it was previously unvalidated).
        (
            "workflow_type: conformer_screening\n"
            "interaction_energy:\n  sp_route_line: '! r2scan-3c MD'\n"
        ),
        (
            "workflow_type: conformer_screening\n"
            "interaction_energy:\n  sp_route_line: '! r2scan-3c GCP(FILE)'\n"
        ),
        (
            "workflow_type: conformer_screening\n"
            "interaction_energy:\n"
            "  enabled: true\n"
            "  sp_route_line: '! HF TightOpt'\n"
            "  fragments:\n"
            "    - atom_indices: [0]\n"
            "    - atom_indices: [1]\n"
        ),
        (
            "workflow_type: conformer_screening\n"
            "interaction_energy:\n"
            "  enabled: true\n"
            "  priority: -1000000000\n"
            "  fragments:\n"
            "    - atom_indices: [0]\n"
            "    - atom_indices: [1]\n"
        ),
        # The fragment fan-out count is bounded remotely.
        "workflow_type: conformer_screening\ninteraction_energy:\n  max_fragments: 99\n",
        "workflow_type: conformer_screening\ncrest:\n  mdlen: 1000000000\n",
        "workflow_type: conformer_screening\ncrest:\n  len: 1\n",
        "workflow_type: conformer_screening\ncrest:\n  tstep: 0.001\n",
        "workflow_type: conformer_screening\ncrest:\n  mddump: 1\n",
        ("workflow_type: conformer_screening\ncrest: &cost\n  mdlen: 1000000000\nshared: *cost\n"),
    ],
)
def test_uploaded_workflow_rejects_injected_or_unbounded_generation(
    tmp_path: Path,
    manifest: str,
) -> None:
    job_dir = tmp_path / "remote_flow"
    job_dir.mkdir()
    (job_dir / "flow.yaml").write_text(manifest, encoding="utf-8")
    (job_dir / "input.xyz").write_text("1\n\nH 0 0 0\n", encoding="utf-8")
    app = _app(tmp_path)

    receipt = app._submit_extracted_run_dir(job_dir, run_dir_kind="workflow")

    assert receipt.committed is False
    assert receipt.detail
    assert not (job_dir / "workflow.json").exists()


def test_workflow_submission_forces_runs_root_and_trusted_resource_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.flow.cli import run_dir as workflow_run_dir

    job_dir = tmp_path / "flow_job"
    job_dir.mkdir()
    (job_dir / "flow.yaml").write_text(
        "workflow_type: conformer_screening\nmax_cores: 1\nmax_memory_gb: 2\n",
        encoding="utf-8",
    )
    (job_dir / "input.xyz").write_text("1\n\nH 0 0 0\n", encoding="utf-8")
    app = BotApplication(
        settings=BotSettings(
            workflow_root=str(tmp_path / "different_workflow_root"),
            crest_config=None,
            xtb_config=None,
            orca_config=None,
            orca_repo_root=None,
            runs_root=str(tmp_path),
        ),
        upload_policy=UploadPolicy(enabled=True),
    )
    captured: dict[str, object] = {}

    def _fake_create(args: Any, submitted_dir: Path) -> dict[str, object]:
        captured["workflow_root"] = args.workflow_root
        captured["max_cores"] = args.max_cores
        captured["max_memory_gb"] = args.max_memory_gb
        return {"workflow_id": submitted_dir.name}

    monkeypatch.setattr(workflow_run_dir, "_create_run_dir_workflow", _fake_create)

    receipt = app._submit_extracted_run_dir(job_dir, run_dir_kind="workflow")

    assert receipt == SubmissionReceipt(True, "flow_job", "", "workflow")
    assert captured == {
        "workflow_root": str(tmp_path.resolve()),
        "max_cores": 8,
        "max_memory_gb": 32,
    }


def _write_workspace_payload(
    workspace_dir: Path,
    workflow_id: str,
    input_xyz: Path,
    *,
    requested_at: str | None = None,
) -> None:
    from datetime import UTC, datetime

    workspace_dir.mkdir(parents=True)
    (workspace_dir / "workflow.json").write_text(
        json.dumps(
            {
                "workflow_id": workflow_id,
                "requested_at": requested_at or datetime.now(UTC).isoformat(),
                "metadata": {"source_inputs": [str(input_xyz)]},
            }
        ),
        encoding="utf-8",
    )


def _fresh_generation_names(count: int) -> list[str]:
    return [f"20260717-08000{index}-{index:08x}" for index in range(count)]


def _workflow_job_dir(tmp_path: Path) -> Path:
    job_dir = tmp_path / "flow_job"
    job_dir.mkdir()
    (job_dir / "flow.yaml").write_text(
        "workflow_type: conformer_screening\n",
        encoding="utf-8",
    )
    (job_dir / "input.xyz").write_text("1\n\nH 0 0 0\n", encoding="utf-8")
    return job_dir


def test_workflow_postcommit_exception_returns_committed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry-sync failure after the durable workflow.json write must be
    classified as committed. This drives the real factory so the probe is
    checked against the production payload shape."""

    import dataclasses

    from orca_auto.flow import orchestration as flow_orchestration

    job_dir = _workflow_job_dir(tmp_path)
    app = _app(tmp_path)
    real_deps = flow_orchestration._workflow_factory_deps()

    def _raise_registry_sync(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("registry sync failed")

    monkeypatch.setattr(
        flow_orchestration,
        "_workflow_factory_deps",
        lambda: dataclasses.replace(real_deps, sync_workflow_registry_fn=_raise_registry_sync),
    )

    receipt = app._submit_extracted_run_dir(job_dir, run_dir_kind="workflow")

    assert receipt.committed is True
    assert "registry sync failed" in receipt.detail
    generations = [
        item
        for item in job_dir.iterdir()
        if item.is_dir() and is_visible_generation_name(item.name)
    ]
    assert len(generations) == 1
    workspace_dir = generations[0]
    assert receipt.submission_id == workspace_dir.name
    assert (workspace_dir / "workflow.json").is_file()


def test_workflow_precommit_exception_returns_failed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.flow.cli import run_dir as workflow_run_dir

    job_dir = _workflow_job_dir(tmp_path)
    app = _app(tmp_path)

    def _raise_before_persist(args: Any, submitted_dir: Path) -> dict[str, object]:
        del args, submitted_dir
        raise RuntimeError("exploded before any durable write")

    monkeypatch.setattr(workflow_run_dir, "_create_run_dir_workflow", _raise_before_persist)

    receipt = app._submit_extracted_run_dir(job_dir, run_dir_kind="workflow")

    assert receipt.committed is False
    assert "exploded before any durable write" in receipt.detail


def test_workflow_exception_with_two_matching_workspaces_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.flow.cli import run_dir as workflow_run_dir

    job_dir = _workflow_job_dir(tmp_path)
    app = _app(tmp_path)

    def _raise_after_double_persist(args: Any, submitted_dir: Path) -> dict[str, object]:
        del args
        for workflow_id in _fresh_generation_names(2):
            _write_workspace_payload(
                submitted_dir / workflow_id,
                workflow_id,
                submitted_dir / "input.xyz",
            )
        raise RuntimeError("registry sync failed")

    monkeypatch.setattr(workflow_run_dir, "_create_run_dir_workflow", _raise_after_double_persist)

    receipt = app._submit_extracted_run_dir(job_dir, run_dir_kind="workflow")

    assert receipt.committed is None


def test_workflow_precommit_exception_ignores_unrelated_root_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace elsewhere under runs_root must never be claimed as this
    submission's commit: only a generation inside the published dir counts,
    so the pre-commit failure stays a definite failure."""

    from orca_auto.flow.cli import run_dir as workflow_run_dir

    job_dir = _workflow_job_dir(tmp_path)
    app = _app(tmp_path)
    _write_workspace_payload(
        tmp_path / _fresh_generation_names(1)[0],
        _fresh_generation_names(1)[0],
        job_dir / "input.xyz",
    )

    def _raise_before_persist(args: Any, submitted_dir: Path) -> dict[str, object]:
        del args, submitted_dir
        raise RuntimeError("exploded before any durable write")

    monkeypatch.setattr(workflow_run_dir, "_create_run_dir_workflow", _raise_before_persist)

    receipt = app._submit_extracted_run_dir(job_dir, run_dir_kind="workflow")

    assert receipt.committed is False
    assert "exploded before any durable write" in receipt.detail


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


def test_disabled_discord_help_does_not_advertise_upload_command(tmp_path: Path) -> None:
    from orca_auto.core.messaging.interactive import IncomingCommand

    app = _app(tmp_path, enabled=False)
    messenger = FakeMessenger()
    command = IncomingCommand(address=ADDRESS, actor=ACTOR, command="help")

    status = app.dispatch_command(command, messenger=messenger)

    assert status == "help-sent"
    assert "!run" not in messenger.replies[-1].text


def test_telegram_run_command_is_not_advertised_or_accepted(tmp_path: Path) -> None:
    from orca_auto.core.messaging.interactive import IncomingCommand

    app = _app(tmp_path)
    messenger = FakeMessenger()
    messenger.provider = "telegram"
    address = ConversationAddress(provider="telegram", channel_id="100")

    help_status = app.dispatch_command(
        IncomingCommand(address=address, actor=ACTOR, command="help"),
        messenger=messenger,
    )
    run_status = app.dispatch_command(
        IncomingCommand(address=address, actor=ACTOR, command="run"),
        messenger=messenger,
    )

    assert help_status == "help-sent"
    assert "/run" not in messenger.replies[-2].text
    assert run_status == "run-unavailable"
    assert "only through Discord" in messenger.replies[-1].text
