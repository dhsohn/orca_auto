"""Durable Discord upload application and queue/workflow commit coordination."""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from orca_auto.core.ingest import (
    UploadActionConsumedError,
    UploadActionExpiredError,
    UploadActionKind,
    UploadActionNotFoundError,
    UploadArchiveError,
    UploadBinding,
    UploadBindingMismatchError,
    UploadPolicy,
    UploadQuotaExceededError,
    UploadRejected,
    UploadReservation,
    UploadSession,
    UploadSessionError,
    UploadSessionStore,
    UploadState,
    UploadStateConflictError,
    UploadSweepResult,
    extract_archive,
    inspect_archive,
    validate_run_dir_name,
)
from orca_auto.core.messaging.interactive import (
    Actor,
    BotReply,
    CardAction,
    ConversationAddress,
    IncomingAction,
    IncomingUpload,
    InteractiveMessenger,
)
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.utils.lock import tmpfs_file_lock

from .._orca_stage_materialization import safe_name
from . import remote_admission
from .interaction_delivery import (
    acknowledge,
    check_provider,
    clear_origin_actions,
    invalid_action_text,
    send_action_reply,
)
from .replies import code, error_message, field_row, info_message, raw, reply_message, text
from .replies import line as reply_line
from .settings import BotSettings

LOGGER = logging.getLogger(__name__)

SubmissionKind = Literal["orca", "workflow", "unknown"]

_UPLOAD_STAGING_DIRNAME = ".uploads"
_UPLOAD_EXTRACT_DIRNAME = ".extract"
_UPLOAD_PUBLISH_LOCK_NAME = ".upload-publish.lock"
_UPLOAD_PUBLISH_MARKER = ".orca-auto-upload"
_UPLOAD_ACTION_TTL_SECONDS = 5 * 60.0
_UPLOAD_ACTION_PREFIX = "act_"


@dataclass(frozen=True)
class SubmissionReceipt:
    """Durable submission outcome at the queue/workflow persistence boundary."""

    committed: bool | None
    submission_id: str | None
    detail: str
    kind: SubmissionKind
    failure_reason: str = ""

    @property
    def cleanup_safe(self) -> bool:
        """Return whether the freshly extracted directory can be removed."""

        return self.committed is False and self.failure_reason != "submission_conflict"


@dataclass(frozen=True)
class RunSubmissionOutcome:
    """Operator reply and externally visible dispatch status for a run action."""

    status: str
    reply: BotReply
    receipt: SubmissionReceipt | None = None


class _UploadPublicationUncertain(RuntimeError):
    """A public name may have become visible even though rename raised."""

    def __init__(self, candidate: Path, detail: str) -> None:
        super().__init__(detail)
        self.candidate = candidate


@dataclass(frozen=True)
class _UploadRunDirSubmissionArgs:
    """Typed adapter request for the existing ORCA/workflow submission APIs."""

    path: str
    workflow_dir: str
    config: str | None
    workflow_root: str
    orca_auto_config: str | None
    priority: int = 10
    max_cores: int | None = None
    max_memory_gb: int | None = None
    force: bool = False
    json: bool = False


@dataclass
class UploadApplication:
    """Own the durable upload transaction independently from command routing."""

    settings: BotSettings
    upload_policy: UploadPolicy
    upload_sessions: UploadSessionStore | None = None

    def __post_init__(self) -> None:
        """Create the durable upload store from the trusted server policy."""

        if self.upload_sessions is not None:
            return
        runs_root = self._runs_root()
        if runs_root is None:
            return
        policy = self.upload_policy
        self.upload_sessions = UploadSessionStore(
            self._staging_dir(runs_root),
            max_staged_count=policy.max_staged_uploads,
            max_staged_bytes=policy.max_staged_bytes,
            max_staged_per_actor=policy.max_pending_per_actor,
            session_ttl_seconds=policy.staging_ttl_seconds,
            action_ttl_seconds=min(
                float(policy.staging_ttl_seconds),
                _UPLOAD_ACTION_TTL_SECONDS,
            ),
            processing_ttl_seconds=policy.staging_ttl_seconds,
            committed_retention_seconds=policy.committed_retention_seconds,
            sweep_on_startup=False,
        )
        self.sweep_upload_sessions()

    def _runs_root(self) -> Path | None:
        raw = (self.settings.runs_root or "").strip()
        return Path(raw).expanduser().resolve() if raw else None

    def _staging_dir(self, runs_root: Path) -> Path:
        return runs_root / _UPLOAD_STAGING_DIRNAME

    @staticmethod
    def _upload_binding(address: ConversationAddress, actor: Actor) -> UploadBinding:
        return UploadBinding(
            provider=address.provider,
            channel_id=address.channel_id,
            thread_id=address.thread_id,
            actor_id=actor.user_id,
        )

    def _require_upload_sessions(self) -> UploadSessionStore:
        if self.upload_sessions is None:
            raise RuntimeError("Upload staging is not configured.")
        return self.upload_sessions

    def reserve_upload(
        self,
        *,
        address: ConversationAddress,
        actor: Actor,
        message_id: str,
        attachment_ids: Sequence[str],
        expected_bytes: int,
    ) -> UploadReservation:
        """Reserve bounded durable staging before a provider downloads bytes."""

        policy = self.upload_policy
        if policy is None or not policy.enabled:
            raise RuntimeError("File uploads are disabled.")
        if expected_bytes > policy.max_archive_bytes:
            raise UploadQuotaExceededError(f"archive exceeds {policy.max_archive_bytes} bytes")
        return self._require_upload_sessions().reserve(
            self._upload_binding(address, actor),
            message_id=message_id,
            attachment_ids=attachment_ids,
            expected_bytes=expected_bytes,
        )

    def finalize_upload(self, upload_id: str) -> UploadSession:
        """Finalize provider-written bytes and enforce the archive byte ceiling."""

        store = self._require_upload_sessions()
        session = store.finalize_upload(upload_id)
        policy = self.upload_policy
        if (
            policy is not None
            and session.actual_bytes is not None
            and session.actual_bytes > policy.max_archive_bytes
        ):
            reason = f"archive exceeds {policy.max_archive_bytes} bytes"
            store.mark_failed(upload_id, reason=reason)
            raise UploadQuotaExceededError(reason)
        return session

    def abandon_upload(self, upload_id: str, reason: str = "upload abandoned") -> None:
        """Release a pre-processing reservation without risking published work."""

        store = self.upload_sessions
        if store is None:
            return
        try:
            session = store.get(upload_id)
            if session.state in {
                UploadState.RECEIVING,
                UploadState.VERIFIED,
                UploadState.AWAITING_CONFIRM,
            }:
                store.mark_discarded(upload_id, reason=reason)
            elif session.state in {UploadState.PROCESSING, UploadState.PUBLISHED}:
                store.mark_ambiguous(upload_id, reason=reason)
        except Exception:  # noqa: BLE001 - best-effort cleanup at a durability boundary
            LOGGER.debug("upload_abandon_raced: %s", upload_id)

    def sweep_upload_sessions(self) -> UploadSweepResult:
        """Sweep staging and reconcile publication/commit crash windows."""

        store = self._require_upload_sessions()
        runs_root = self._runs_root()
        if runs_root is None or not runs_root.is_dir():
            return store.sweep()

        for marker in runs_root.glob(f"*/{_UPLOAD_PUBLISH_MARKER}"):
            session: UploadSession | None = None
            try:
                published_dir = marker.parent
                if (
                    published_dir.is_symlink()
                    or marker.is_symlink()
                    or not marker.is_file()
                    or published_dir.resolve().parent != runs_root.resolve()
                ):
                    continue
                upload_id = marker.read_text(encoding="ascii").strip()
                session = store.get(upload_id)
                if session.published_path is None and session.state in {
                    UploadState.PROCESSING,
                    UploadState.AMBIGUOUS,
                }:
                    session = store.mark_published(
                        upload_id,
                        published_path=published_dir,
                    )
                if (
                    session.published_path is None
                    or session.published_path.resolve() != published_dir.resolve()
                ):
                    raise ValueError("upload marker path does not match the durable published path")
                if session.state is UploadState.COMMITTED:
                    _safe_unlink(marker)
                    continue
                if session.state is UploadState.FAILED:
                    if not self._remove_owned_published_upload(
                        published_dir,
                        session.upload_id,
                    ):
                        raise OSError("failed upload publication cleanup did not complete")
                    continue
                if session.state not in {UploadState.PUBLISHED, UploadState.AMBIGUOUS}:
                    continue

                engine = str(session.verification.get("engine") or "")
                if engine == "workflow" or (published_dir / "workflow.json").is_file():
                    committed, workflow_id = self._workflow_commit(published_dir)
                    if committed is True and workflow_id:
                        store.mark_committed(upload_id, workflow_id=workflow_id)
                        _safe_unlink(marker)
                        continue
                    reason = "published workflow has no durable workflow identity"
                else:
                    entries = self._orca_entries_for_run_dir(published_dir, runs_root)
                    if entries:
                        store.mark_committed(upload_id, queue_id=next(reversed(entries)))
                        _safe_unlink(marker)
                        continue
                    reason = "published ORCA run has no durable queue identity"
                if session.state is UploadState.PUBLISHED:
                    store.mark_ambiguous(upload_id, reason=reason)
            except Exception:  # noqa: BLE001 - marker means deletion is never safe here
                # An ownership marker is evidence that deletion is unsafe. Keep
                # both it and the run-dir for a future sweep/manual reconciliation.
                LOGGER.warning("upload_publish_reconcile_failed: %s", marker, exc_info=True)
                if session is not None and session.state in {
                    UploadState.PROCESSING,
                    UploadState.PUBLISHED,
                }:
                    self._mark_upload_ambiguous(
                        session.upload_id,
                        "published upload reconciliation failed",
                    )
        # Reconcile marker-backed publications before terminal retention can
        # prune their records. This closes a long-downtime crash window after
        # mark_committed but before the marker unlink.
        return store.sweep()

    def _incoming_upload_session(self, upload: IncomingUpload) -> UploadSession:
        store = self._require_upload_sessions()
        if upload.upload_id is None:
            raise UploadBindingMismatchError("durable upload identity is required")
        session = store.get(upload.upload_id)
        if session.binding != self._upload_binding(upload.address, upload.actor):
            raise UploadBindingMismatchError("upload identity binding does not match")
        if upload.message_id is None or session.message_id != upload.message_id:
            raise UploadBindingMismatchError("upload message binding does not match")
        if upload.attachment_id is None or upload.attachment_id not in session.attachment_ids:
            raise UploadBindingMismatchError("upload attachment binding does not match")
        try:
            supplied_path = Path(upload.archive_path).expanduser().resolve()
        except OSError as exc:
            raise UploadArchiveError("staged archive is missing") from exc
        if supplied_path != session.archive_path:
            raise UploadBindingMismatchError("upload archive path is not store-owned")
        if session.state is UploadState.RECEIVING and session.actual_bytes is None:
            raise UploadArchiveError("upload must be finalized before dispatch")
        return session

    @staticmethod
    def _upload_confirmation_reply(
        session: UploadSession, confirm_id: str, dismiss_id: str
    ) -> BotReply:
        metadata = session.verification
        job_name = str(metadata.get("job_name") or "upload")
        engine = str(metadata.get("engine") or "unknown")
        raw_entries = metadata.get("entry_count")
        raw_total = metadata.get("total_uncompressed")
        entries = raw_entries if type(raw_entries) is int else 0
        total = raw_total if type(raw_total) is int else 0
        selected = str(metadata.get("selected_entry") or "")
        mib = total / (1024 * 1024)
        entry_text = f", entry: {selected}" if selected else ""
        return BotReply(
            f"Queue {job_name}? ({engine}, {entries} files, {mib:.1f} MiB{entry_text})",
            actions=(
                (
                    CardAction(confirm_id, "Queue it"),
                    CardAction(dismiss_id, "Discard"),
                ),
            ),
            message=reply_message(
                "Queue this run-dir?",
                field_row("Job", code(job_name), inline=True),
                field_row("Engine", code(engine), inline=True),
                field_row("Contents", raw(f"{entries} files · {mib:.1f} MiB"), inline=True),
                *([field_row("Entry", code(selected))] if selected else []),
            ),
        )

    def dispatch_upload(
        self,
        upload: IncomingUpload,
        *,
        messenger: InteractiveMessenger,
    ) -> str:
        """Validate an uploaded archive and ask the operator to confirm the queue."""

        check_provider(upload.address, messenger)
        policy = self.upload_policy
        if policy is None or not policy.enabled:
            status, reply = (
                "upload-disabled",
                BotReply(
                    "File uploads are disabled.",
                    message=error_message("Uploads disabled", "File uploads are disabled."),
                ),
            )
        elif self.upload_sessions is None:
            status, reply = (
                "upload-misconfigured",
                BotReply(
                    "Upload staging is not configured.",
                    message=error_message(
                        "Uploads not configured", "Upload staging is not configured."
                    ),
                ),
            )
        else:
            try:
                session = self._incoming_upload_session(upload)
                if session.state is UploadState.RECEIVING:
                    session = self.upload_sessions.verify_archive(session.upload_id)
                    report = inspect_archive(session.archive_path, policy)
                    job_name = safe_name(
                        report.suggested_name or Path(upload.filename).stem,
                        fallback="upload",
                    )
                    validate_run_dir_name(job_name)
                    session = self.upload_sessions.mark_verified(
                        session.upload_id,
                        verification={
                            "filename": upload.filename,
                            "job_name": job_name,
                            "engine": report.engine_hint,
                            "entry_count": report.entry_count,
                            "total_uncompressed": report.total_uncompressed,
                            "selected_entry": report.selected_entry,
                        },
                    )
                if session.state in {UploadState.VERIFIED, UploadState.AWAITING_CONFIRM}:
                    action_set = self.upload_sessions.await_confirmation(session.upload_id)
                    status = "upload-confirmation-sent"
                    reply = self._upload_confirmation_reply(
                        action_set.session,
                        action_set.confirm_action_id,
                        action_set.dismiss_action_id,
                    )
                elif session.state is UploadState.COMMITTED:
                    status = "upload-already-submitted"
                    reply = BotReply(
                        "This upload was already submitted.",
                        message=info_message(
                            "Already submitted", "This upload was already submitted."
                        ),
                    )
                elif session.state in {
                    UploadState.PROCESSING,
                    UploadState.PUBLISHED,
                    UploadState.AMBIGUOUS,
                }:
                    status = "upload-already-processing"
                    reply = BotReply(
                        "This upload is already being processed; check the list command.",
                        message=info_message(
                            "Already processing",
                            "This upload is already being processed; check the list command.",
                        ),
                    )
                else:
                    status = "upload-unavailable"
                    reply = BotReply(
                        "This upload is no longer available.",
                        message=error_message(
                            "Upload unavailable", "This upload is no longer available."
                        ),
                    )
            except UploadRejected as exc:
                if "session" in locals():
                    self.abandon_upload(session.upload_id, f"archive rejected: {exc.reason}")
                status, reply = (
                    "upload-rejected",
                    BotReply(
                        f"Rejected: {exc.reason}",
                        message=error_message("Upload rejected", str(exc.reason)),
                    ),
                )
            except (
                UploadArchiveError,
                UploadBindingMismatchError,
                UploadSessionError,
                OSError,
            ) as exc:
                LOGGER.warning("upload_confirmation_failed: %s", exc)
                if "session" in locals() and not isinstance(
                    exc,
                    UploadBindingMismatchError,
                ):
                    self.abandon_upload(session.upload_id, str(exc))
                status, reply = (
                    "upload-rejected",
                    BotReply(
                        f"Rejected: {exc}", message=error_message("Upload rejected", str(exc))
                    ),
                )

        try:
            result = messenger.send_reply(upload.address, reply)
        except Exception:  # noqa: BLE001 - transport failure must release staging
            LOGGER.warning("upload_confirmation_delivery_failed", exc_info=True)
            result = None
        if result is None or not result.sent:
            if status == "upload-confirmation-sent" and "session" in locals():
                self.abandon_upload(session.upload_id, "confirmation delivery failed")
            return f"{status}-delivery-failed"
        return status

    @staticmethod
    def _uploaded_run_dir_kind(job_dir: Path) -> Literal["orca", "workflow"]:
        """Validate the remote-ingress trust boundary and classify a run-dir."""

        # Persisted workflow state is a trusted local runtime artifact, never an
        # acceptable remote submission manifest. Allowing it would turn upload
        # into an implicit workflow restart API.
        if (job_dir / "workflow.json").is_file():
            raise ValueError("uploaded workflow.json state is not accepted")

        flow_manifest = job_dir / "flow.yaml"
        if flow_manifest.is_file():
            manifest = remote_admission.uploaded_flow_manifest(job_dir)
            forbidden: list[str] = []
            if "allow_external_inputs" in manifest:
                forbidden.append("allow_external_inputs")
            if "workflow_root" in manifest:
                forbidden.append("workflow_root")
            workflow = manifest.get("workflow")
            if isinstance(workflow, dict) and "root" in workflow:
                forbidden.append("workflow.root")
            if forbidden:
                fields = ", ".join(forbidden)
                raise ValueError(f"uploaded flow.yaml may not set server-owned fields: {fields}")
            return "workflow"

        if any(candidate.is_file() for candidate in job_dir.glob("*.inp")):
            return "orca"
        raise ValueError(
            "could not infer an upload entrypoint; expected root flow.yaml or a root *.inp"
        )

    def _atomic_publish_upload(
        self,
        extracted_dir: Path,
        *,
        job_name: str,
        upload_id: str,
    ) -> tuple[Path, bool]:
        """Atomically rename a private extraction into a locked final run-dir."""

        runs_root = self._runs_root()
        if runs_root is None:
            raise RuntimeError("runs_root is not configured")
        resolved_root = runs_root.resolve()
        resolved_extracted = extracted_dir.resolve()
        store = self._require_upload_sessions()
        session_dir = store.archive_path(upload_id).parent.resolve()
        if session_dir not in resolved_extracted.parents:
            raise ValueError("private extraction escaped its upload session")

        marker = resolved_extracted / _UPLOAD_PUBLISH_MARKER
        if marker.exists() or marker.is_symlink():
            raise ValueError("private extraction contains a reserved publish marker")
        with marker.open("x", encoding="ascii") as handle:
            handle.write(f"{upload_id}\n")
            handle.flush()
            os.fsync(handle.fileno())
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        extracted_fd = os.open(resolved_extracted, flags)
        try:
            os.fsync(extracted_fd)
        finally:
            os.close(extracted_fd)

        lock_timeout = store.lock_timeout_seconds
        with tmpfs_file_lock(
            resolved_root / _UPLOAD_PUBLISH_LOCK_NAME,
            timeout_seconds=lock_timeout,
        ):
            for suffix in range(1, 1000):
                name = job_name if suffix == 1 else f"{job_name}-{suffix}"
                candidate = resolved_root / name
                try:
                    candidate.mkdir()
                except FileExistsError:
                    continue
                try:
                    # Replace only the empty directory reserved by this lock
                    # holder; an existing user run-dir is never a rename target.
                    _replace_directory(resolved_extracted, candidate)
                except Exception as exc:
                    # POSIX rename is atomic, but wrappers/filesystems can report
                    # an error after the namespace change became visible. An
                    # exact ownership marker proves that publication may have
                    # succeeded; never downgrade that outcome to FAILED.
                    if self._owned_published_upload(candidate, upload_id) is not None:
                        LOGGER.warning(
                            "upload_publish_rename_reported_after_success: %s",
                            candidate,
                        )
                        return candidate, False
                    try:
                        candidate.rmdir()
                    except OSError:
                        raise _UploadPublicationUncertain(
                            candidate,
                            "publication rename outcome could not be proven",
                        ) from exc
                    raise
                durable = True
                try:
                    root_fd = os.open(resolved_root, flags)
                    try:
                        os.fsync(root_fd)
                    finally:
                        os.close(root_fd)
                except OSError:
                    durable = False
                    LOGGER.warning("upload_publish_root_fsync_failed: %s", resolved_root)
                return candidate, durable
        raise RuntimeError("too many run-dirs with the same name")

    def _owned_published_upload(self, path: Path, upload_id: str) -> Path | None:
        """Return an exact marker-proven direct runs-root child, if present."""

        runs_root = self._runs_root()
        if runs_root is None:
            return None
        try:
            if path.is_symlink():
                return None
            resolved = path.resolve(strict=True)
            marker = resolved / _UPLOAD_PUBLISH_MARKER
            if (
                resolved.parent != runs_root.resolve()
                or not resolved.is_dir()
                or marker.is_symlink()
                or not marker.is_file()
                or marker.read_text(encoding="ascii").strip() != upload_id
            ):
                return None
            return resolved
        except (OSError, UnicodeError):
            return None

    def _remove_owned_published_upload(self, path: Path, upload_id: str) -> bool:
        """Remove a marker-proven upload while keeping retry evidence until last."""

        resolved = self._owned_published_upload(path, upload_id)
        if resolved is None:
            return False
        marker = resolved / _UPLOAD_PUBLISH_MARKER
        try:
            # A partial recursive cleanup must leave the ownership marker for a
            # startup retry. Delete the marker only after every other child is
            # gone, and recreate it if the final rmdir cannot complete.
            for child in resolved.iterdir():
                if child == marker:
                    continue
                if child.is_symlink() or not child.is_dir():
                    child.unlink()
                else:
                    shutil.rmtree(child)
            marker.unlink()
            try:
                resolved.rmdir()
            except OSError:
                with marker.open("x", encoding="ascii") as handle:
                    handle.write(f"{upload_id}\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                raise
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            root_fd = os.open(resolved.parent, flags)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            return True
        except (OSError, UnicodeError):
            LOGGER.warning("upload_published_cleanup_failed: %s", path)
            return False

    @staticmethod
    def _submission_outcome(job_dir: Path, receipt: SubmissionReceipt) -> RunSubmissionOutcome:
        if receipt.cleanup_safe:
            note = f"Submission failed for {job_dir.name}. See the bot log."
            return RunSubmissionOutcome(
                "run-submission-failed",
                BotReply(note, message=error_message("Submission failed", note)),
                receipt,
            )
        if receipt.committed is False:
            note = (
                f"{job_dir.name} is already owned by an existing submission; "
                "its files were preserved. Check the list command before retrying."
            )
            return RunSubmissionOutcome(
                "run-submission-conflict",
                BotReply(note, message=error_message("Already submitted", note)),
                receipt,
            )
        if receipt.committed is None:
            note = (
                f"Submission outcome is uncertain for {job_dir.name}; its files were "
                "preserved. Check the list command and bot log before retrying."
            )
            return RunSubmissionOutcome(
                "run-submission-uncertain",
                BotReply(note, message=error_message("Submission uncertain", note)),
                receipt,
            )

        identifier = f" (id: {receipt.submission_id})" if receipt.submission_id else ""
        if receipt.failure_reason == "submission_conflict":
            note = (
                f"{job_dir.name} is already queued{identifier}; its files were preserved. "
                "Track it with the list command."
            )
            return RunSubmissionOutcome(
                "run-already-submitted",
                BotReply(note, message=info_message("Already queued", note)),
                receipt,
            )
        if receipt.detail:
            note = (
                f"Queued {job_dir.name}{identifier}, but a post-submission step failed. "
                "The committed run was preserved; check the bot log."
            )
            return RunSubmissionOutcome(
                "run-submitted-with-warning",
                BotReply(
                    note,
                    message=reply_message(
                        "Queued with a warning", reply_line(text(note)), severity="warning"
                    ),
                ),
                receipt,
            )
        submission_fields = [field_row("Run-dir", code(job_dir.name), inline=True)]
        if receipt.submission_id:
            submission_fields.append(field_row("ID", code(receipt.submission_id), inline=True))
        return RunSubmissionOutcome(
            "run-submitted",
            BotReply(
                f"Queued {job_dir.name}{identifier}. Track it with the list command.",
                message=reply_message(
                    "Queued",
                    *submission_fields,
                    reply_line(text("Track it with the list command.")),
                    severity="success",
                ),
            ),
            receipt,
        )

    def _mark_upload_ambiguous(self, upload_id: str, reason: str) -> None:
        try:
            self._require_upload_sessions().mark_ambiguous(upload_id, reason=reason)
        except Exception:  # noqa: BLE001 - published work must remain preserved
            LOGGER.warning("upload_ambiguous_state_failed: %s", upload_id, exc_info=True)

    def _run_submit_session(self, session: UploadSession) -> RunSubmissionOutcome:
        """Extract privately, publish atomically, then persist the commit outcome."""

        policy = self.upload_policy
        runs_root = self._runs_root()
        store = self.upload_sessions
        if policy is None or not policy.enabled or runs_root is None or store is None:
            try:
                if store is not None:
                    store.mark_failed(session.upload_id, reason="uploads are disabled")
            except Exception:  # noqa: BLE001 - no downstream commit was attempted
                LOGGER.warning("upload_disabled_state_failed", exc_info=True)
            return RunSubmissionOutcome(
                "run-disabled",
                BotReply(
                    "File uploads are disabled.",
                    message=error_message("Uploads disabled", "File uploads are disabled."),
                ),
            )

        extract_root = session.archive_path.parent / _UPLOAD_EXTRACT_DIRNAME
        extracted_dir: Path | None = None
        try:
            session = store.verify_archive(session.upload_id)
        except UploadArchiveError as exc:
            try:
                store.mark_failed(session.upload_id, reason=str(exc))
            except Exception:  # noqa: BLE001 - no downstream commit was attempted
                LOGGER.warning("upload_expired_state_failed", exc_info=True)
            return RunSubmissionOutcome(
                "run-expired",
                BotReply(
                    "Upload expired or changed before it could be processed.",
                    message=error_message(
                        "Upload expired",
                        "Upload expired or changed before it could be processed.",
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - stable durability boundary
            LOGGER.warning("upload_archive_identity_check_failed", exc_info=True)
            return RunSubmissionOutcome(
                "run-unavailable",
                BotReply(str(exc), message=error_message("Upload unavailable", str(exc))),
            )

        try:
            if extract_root.exists() or extract_root.is_symlink():
                raise UploadRejected("private extraction already exists")
            job_name = safe_name(
                str(session.verification.get("job_name") or "upload"),
                fallback="upload",
            )
            extracted_dir = extract_archive(
                session.archive_path,
                extract_root,
                job_name,
                policy,
                expected_size=session.actual_bytes,
                expected_sha256=session.sha256,
            )
            run_dir_kind = self._uploaded_run_dir_kind(extracted_dir)
            expected_kind = str(session.verification.get("engine") or "")
            if expected_kind and run_dir_kind != expected_kind:
                raise ValueError("archive entrypoint changed after confirmation")
        except (OSError, UploadRejected, ValueError) as exc:
            reason = exc.reason if isinstance(exc, UploadRejected) else str(exc)
            try:
                store.mark_failed(session.upload_id, reason=reason)
            except Exception:  # noqa: BLE001 - no downstream commit was attempted
                LOGGER.warning("upload_rejected_state_failed", exc_info=True)
            return RunSubmissionOutcome(
                "run-rejected",
                BotReply(f"Rejected: {reason}", message=error_message("Upload rejected", reason)),
            )

        try:
            published_dir, publication_durable = self._atomic_publish_upload(
                extracted_dir,
                job_name=job_name,
                upload_id=session.upload_id,
            )
        except _UploadPublicationUncertain as exc:
            reason = self._exception_detail(exc)
            self._mark_upload_ambiguous(session.upload_id, reason)
            note = (
                "Publication outcome is uncertain; any visible files were preserved. "
                "Check the bot log before retrying."
            )
            return RunSubmissionOutcome(
                "run-submission-uncertain",
                BotReply(note, message=error_message("Submission uncertain", note)),
            )
        except Exception as exc:  # noqa: BLE001 - no public namespace change was observed
            reason = self._exception_detail(exc)
            try:
                store.mark_failed(session.upload_id, reason=reason)
            except Exception:  # noqa: BLE001 - no downstream commit was attempted
                LOGGER.warning("upload_publish_failure_state_failed", exc_info=True)
            return RunSubmissionOutcome(
                "run-publish-failed",
                BotReply(
                    "Could not safely publish the upload. See the bot log.",
                    message=error_message(
                        "Publish failed", "Could not safely publish the upload. See the bot log."
                    ),
                ),
            )

        try:
            store.mark_published(session.upload_id, published_path=published_dir)
        except Exception as exc:  # noqa: BLE001 - publication exists; never delete it
            reason = f"published before state persistence: {self._exception_detail(exc)}"
            self._mark_upload_ambiguous(session.upload_id, reason)
            note = (
                f"Publication outcome is uncertain for {published_dir.name}; its files "
                "were preserved. Check the bot log before retrying."
            )
            return RunSubmissionOutcome(
                "run-submission-uncertain",
                BotReply(note, message=error_message("Submission uncertain", note)),
            )

        if not publication_durable:
            reason = "published run-dir could not be durably synced"
            self._mark_upload_ambiguous(session.upload_id, reason)
            note = (
                f"Publication durability is uncertain for {published_dir.name}; its files "
                "were preserved and were not submitted. Check the bot log before retrying."
            )
            return RunSubmissionOutcome(
                "run-submission-uncertain",
                BotReply(note, message=error_message("Submission uncertain", note)),
            )

        try:
            receipt = self._submit_extracted_run_dir(
                published_dir,
                run_dir_kind=run_dir_kind,
            )
        except Exception as exc:  # noqa: BLE001 - enforce structured ambiguous outcome
            receipt = SubmissionReceipt(
                None,
                None,
                self._exception_detail(exc),
                run_dir_kind,
            )

        if receipt.committed is True and receipt.submission_id:
            try:
                if receipt.kind == "workflow":
                    store.mark_committed(
                        session.upload_id,
                        workflow_id=receipt.submission_id,
                    )
                else:
                    store.mark_committed(
                        session.upload_id,
                        queue_id=receipt.submission_id,
                    )
                _safe_unlink(published_dir / _UPLOAD_PUBLISH_MARKER)
            except Exception as exc:  # noqa: BLE001 - commit may already be durable
                self._mark_upload_ambiguous(
                    session.upload_id,
                    f"commit receipt persistence failed: {self._exception_detail(exc)}",
                )
                note = (
                    f"Submission {receipt.submission_id} reached the downstream system, "
                    "but its local receipt could not be persisted. The run directory and "
                    "reconciliation marker were preserved."
                )
                return RunSubmissionOutcome(
                    "run-submission-uncertain",
                    BotReply(note, message=error_message("Submission uncertain", note)),
                    receipt,
                )
        elif receipt.cleanup_safe:
            reason = receipt.detail or "known pre-commit submission failure"
            try:
                # Persist cleanup intent before deleting a public directory. A
                # crash after this transition is recovered by the FAILED-marker
                # sweep above; a failed state write never authorizes deletion.
                store.mark_failed(session.upload_id, reason=reason)
            except Exception:  # noqa: BLE001 - preserve ownership evidence on failure
                LOGGER.warning("upload_failure_state_failed", exc_info=True)
                self._mark_upload_ambiguous(
                    session.upload_id,
                    f"{reason}; failure state persistence failed",
                )
            else:
                if not self._remove_owned_published_upload(
                    published_dir,
                    session.upload_id,
                ):
                    LOGGER.warning(
                        "upload_failed_publication_cleanup_pending: %s",
                        published_dir,
                    )
        else:
            reason = receipt.detail or "downstream commit outcome is ambiguous"
            self._mark_upload_ambiguous(session.upload_id, reason)

        return self._submission_outcome(published_dir, receipt)

    def _workflow_commit(self, job_dir: Path) -> tuple[bool | None, str | None]:
        """Locate the durable workflow created inside one published run-dir.

        Workflow workspaces are generation directories minted inside the
        submitted directory itself (mirroring standalone ORCA executions),
        so the commit evidence is a generation child carrying workflow.json.
        The legacy in-place layout (workflow.json directly in job_dir) is
        still honored for historical workspaces. Returns (True, id) on
        exactly one clean match, (False, None) when absence is provable,
        and (None, None) when the commit outcome cannot be classified
        safely.
        """

        if (job_dir / "workflow.json").exists():
            in_place = self._read_workflow_identity(job_dir)
            return (True, in_place) if in_place else (None, None)
        try:
            children = sorted(job_dir.iterdir())
        except OSError:
            return None, None
        matches: list[str] = []
        unreadable = False
        for child in children:
            if not child.is_dir() or child.is_symlink():
                continue
            if not is_visible_generation_name(child.name):
                continue
            if not (child / "workflow.json").exists():
                continue
            workflow_id = self._read_workflow_identity(child)
            if workflow_id is None:
                unreadable = True
                LOGGER.warning(
                    "workflow_commit_probe_unreadable_payload: %s", child / "workflow.json"
                )
                continue
            matches.append(workflow_id)
        if len(matches) == 1:
            return True, matches[0]
        if matches:
            return None, None
        return (None, None) if unreadable else (False, None)

    @staticmethod
    def _read_workflow_identity(workspace_dir: Path) -> str | None:
        """Return the workflow id of an in-place workspace, if one exists."""

        workflow_file = workspace_dir / "workflow.json"
        if not workflow_file.exists():
            return None
        try:
            payload = json.loads(workflow_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return str(payload.get("workflow_id") or "").strip() or None

    @staticmethod
    def _orca_entries_for_run_dir(job_dir: Path, runs_root: Path) -> dict[str, Any]:
        """Return durable queue entries for one run directory, keyed by queue id."""

        from orca_auto.orca.queue import adapter as queue_adapter

        resolved = str(job_dir.expanduser().resolve())
        entries: dict[str, Any] = {}
        for entry in queue_adapter.list_queue(runs_root):
            if queue_adapter.queue_entry_reaction_dir(entry) != resolved:
                continue
            queue_id = queue_adapter.queue_entry_id(entry)
            if queue_id:
                entries[queue_id] = entry
        return entries

    @staticmethod
    def _exception_detail(exc: Exception) -> str:
        message = str(exc).strip()
        return f"{type(exc).__name__}: {message}" if message else type(exc).__name__

    def _submit_orca_run_dir(
        self,
        job_dir: Path,
        runs_root: Path,
        args: _UploadRunDirSubmissionArgs,
    ) -> SubmissionReceipt:
        from orca_auto.orca.queue import adapter as queue_adapter
        from orca_auto.orca.submission import submit_reaction_dir_to_queue

        try:
            before_ids: set[str] | None = set(self._orca_entries_for_run_dir(job_dir, runs_root))
        except Exception:  # noqa: BLE001 - a failed audit makes cleanup unsafe
            before_ids = None
            LOGGER.warning("upload_submit_queue_snapshot_failed", exc_info=True)

        try:
            submission = submit_reaction_dir_to_queue(args)
        except Exception as exc:  # noqa: BLE001 - inspect persistence before classifying
            detail = self._exception_detail(exc)
            LOGGER.warning("upload_submit_orca_failed: %s", detail, exc_info=True)
            try:
                after = self._orca_entries_for_run_dir(job_dir, runs_root)
            except Exception:  # noqa: BLE001 - commit outcome is indeterminate
                LOGGER.warning("upload_submit_queue_reconcile_failed", exc_info=True)
                return SubmissionReceipt(None, None, detail, "orca")
            if before_ids is not None:
                new_ids = [queue_id for queue_id in after if queue_id not in before_ids]
                if new_ids:
                    queue_id = new_ids[-1]
                    return SubmissionReceipt(True, queue_id, detail, "orca")
            # An unexpected exception has no reliable pre/post-commit contract.
            # Preserve the directory even when a queue entry cannot be found.
            return SubmissionReceipt(None, None, detail, "orca")

        if submission.status != "submitted":
            detail = str(submission.stderr or submission.reason or "submission failed").strip()
            LOGGER.warning("upload_submit_orca_rejected: %s", detail)
            failure_reason = str(submission.reason or "").strip()
            try:
                existing = self._orca_entries_for_run_dir(job_dir, runs_root)
            except Exception:  # noqa: BLE001 - every failed result needs commit reconciliation
                LOGGER.warning("upload_submit_failure_reconcile_failed", exc_info=True)
                return SubmissionReceipt(
                    None,
                    None,
                    detail,
                    "orca",
                    failure_reason=failure_reason,
                )
            if existing:
                queue_id = next(reversed(existing))
                return SubmissionReceipt(
                    True,
                    queue_id,
                    detail,
                    "orca",
                    failure_reason=failure_reason,
                )
            if failure_reason == "submission_conflict":
                # A conflict can be backed by state outside the active queue;
                # absence from one index is not proof that deletion is safe.
                return SubmissionReceipt(
                    None,
                    None,
                    detail,
                    "orca",
                    failure_reason=failure_reason,
                )
            if failure_reason == "invalid_submission_target":
                # Context resolution fails before conflict checks or enqueue.
                return SubmissionReceipt(
                    False,
                    None,
                    detail,
                    "orca",
                    failure_reason=failure_reason,
                )
            # ``invalid_submission_input`` also covers ValueError raised by
            # post-enqueue notification. An empty active-queue scan cannot prove
            # pre-commit failure if that entry was concurrently cleared.
            return SubmissionReceipt(
                None,
                None,
                detail,
                "orca",
                failure_reason=failure_reason,
            )

        queued = submission.queued_result
        if queued is None or getattr(queued, "entry", None) is None:
            detail = "ORCA submission returned no durable queue entry"
            LOGGER.warning("upload_submit_orca_indeterminate: %s", detail)
            return SubmissionReceipt(None, None, detail, "orca")
        queue_id = queue_adapter.queue_entry_id(queued.entry)
        if not queue_id:
            detail = "ORCA submission returned a queue entry without an id"
            LOGGER.warning("upload_submit_orca_indeterminate: %s", detail)
            return SubmissionReceipt(None, None, detail, "orca")
        worker_info = getattr(queued, "worker_info", None)
        warning = str(getattr(worker_info, "detail", None) or "").strip()
        return SubmissionReceipt(True, queue_id, warning, "orca")

    def _submit_workflow_run_dir(
        self,
        job_dir: Path,
        args: _UploadRunDirSubmissionArgs,
    ) -> SubmissionReceipt:
        from orca_auto.flow.cli.run_dir import _create_run_dir_workflow

        try:
            payload = _create_run_dir_workflow(args, job_dir)
        except Exception as exc:  # noqa: BLE001 - inspect persistence before classifying
            detail = self._exception_detail(exc)
            LOGGER.warning("upload_submit_workflow_failed: %s", detail, exc_info=True)
            committed, workflow_id = self._workflow_commit(job_dir)
            return SubmissionReceipt(committed, workflow_id, detail, "workflow")

        workflow_id = str(payload.get("workflow_id") or "").strip()
        if not workflow_id:
            detail = "workflow creation returned no workflow_id"
            LOGGER.warning("upload_submit_workflow_indeterminate: %s", detail)
            return SubmissionReceipt(None, None, detail, "workflow")
        return SubmissionReceipt(True, workflow_id, "", "workflow")

    def _submit_extracted_run_dir(
        self,
        job_dir: Path,
        *,
        run_dir_kind: Literal["orca", "workflow"] | None = None,
    ) -> SubmissionReceipt:
        """Submit an upload through direct APIs and report the commit boundary.

        Server-owned ``runs_root`` is always passed explicitly as workflow root;
        no workflow path or external-input authority is inherited from uploaded
        data. The direct APIs return durable identifiers without routing output
        through process-global stdout.
        """

        runs_root = self._runs_root()
        if runs_root is None:
            return SubmissionReceipt(False, None, "runs_root is not configured", "unknown")
        try:
            kind = run_dir_kind or self._uploaded_run_dir_kind(job_dir)
        except ValueError as exc:
            return SubmissionReceipt(False, None, str(exc), "unknown")

        try:
            max_cores, max_memory_gb = remote_admission.trusted_upload_resource_limits(
                self.settings.orca_config
            )
            remote_atom_count = remote_admission.validate_remote_xyz_atom_limits(job_dir)
            if kind == "workflow":
                remote_admission.validate_workflow_resource_limits(
                    job_dir,
                    max_cores=max_cores,
                    max_memory_gb=max_memory_gb,
                )
                remote_admission.apply_remote_workflow_crest_policy(
                    job_dir,
                    atom_count=remote_atom_count,
                )
            else:
                remote_admission.validate_orca_resource_limits(
                    job_dir,
                    max_cores=max_cores,
                    max_memory_gb=max_memory_gb,
                )
        except (OSError, ValueError) as exc:
            detail = str(exc).strip() or type(exc).__name__
            LOGGER.warning("upload_submit_resource_rejected: %s", detail)
            return SubmissionReceipt(False, None, detail, kind)

        args = _UploadRunDirSubmissionArgs(
            path=str(job_dir),
            workflow_dir=str(job_dir),
            config=self.settings.orca_config,
            workflow_root=str(runs_root),
            orca_auto_config=self.settings.orca_config,
            max_cores=max_cores if kind == "workflow" else None,
            max_memory_gb=max_memory_gb if kind == "workflow" else None,
        )
        if kind == "workflow":
            return self._submit_workflow_run_dir(job_dir, args)
        return self._submit_orca_run_dir(job_dir, runs_root, args)

    def command_available(self, provider: str) -> bool:
        policy = self.upload_policy
        return (
            provider == "discord"
            and policy is not None
            and policy.enabled
            and self.upload_sessions is not None
        )

    @property
    def enabled(self) -> bool:
        return self.upload_policy.enabled

    @property
    def configured(self) -> bool:
        return self.upload_sessions is not None

    def dispatch_action(
        self,
        incoming: IncomingAction,
        messenger: InteractiveMessenger,
    ) -> str | None:
        """Consume a durable upload action, or return ``None`` when it is not one."""

        check_provider(incoming.address, messenger)

        store = self.upload_sessions
        if store is None or not incoming.action_id.startswith(_UPLOAD_ACTION_PREFIX):
            return None
        try:
            consumed = store.consume_action(
                incoming.action_id,
                binding=self._upload_binding(incoming.address, incoming.actor),
            )
        except UploadActionNotFoundError:
            return None
        except UploadBindingMismatchError:
            status = "wrong_binding"
        except UploadActionExpiredError:
            status = "expired"
        except (UploadActionConsumedError, UploadStateConflictError):
            status = "consumed"
        except Exception:  # noqa: BLE001 - stable interaction boundary
            LOGGER.warning("upload_action_consume_failed", exc_info=True)
            status = "unavailable"
        else:
            clear_origin_actions(incoming, messenger)
            if consumed.action.kind is UploadActionKind.DISMISS:
                acknowledge(incoming, "Upload discarded.", messenger)
                send_action_reply(
                    incoming,
                    BotReply(
                        "Upload discarded.",
                        message=info_message("Upload discarded", "The upload was discarded."),
                    ),
                    messenger,
                )
                return "run-dismissed"

            acknowledge(incoming, "Submitting…", messenger)
            outcome = self._run_submit_session(consumed.session)
            send_action_reply(incoming, outcome.reply, messenger)
            return outcome.status

        text = invalid_action_text(status)
        acknowledge(incoming, text, messenger)
        send_action_reply(
            incoming,
            BotReply(text, message=error_message("Action unavailable", text)),
            messenger,
        )
        return status


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.debug("upload_unlink_skip: %s", path.name)


def _replace_directory(source: Path, destination: Path) -> None:
    """Small crash-injection seam around the atomic publication primitive."""

    os.replace(source, destination)


__all__ = [
    "RunSubmissionOutcome",
    "SubmissionReceipt",
    "UploadApplication",
]
