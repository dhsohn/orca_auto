"""Provider-neutral command and component dispatch for the orca_auto bot."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from orca_auto.activity_presenter import (
    QueueListPresentationDeps,
    QueueListPresentationRequest,
    queue_list_text_presentation,
)
from orca_auto.activity_rendering import queue_clear_lines, queue_list_text_lines
from orca_auto.activity_view import (
    activity_counter_config_path,
    count_global_active_simulations,
    filter_activity_items,
    queue_list_default_visible_items,
    queue_list_display_rows,
)
from orca_auto.core.activity_icons import activity_status_icon
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
    IncomingCommand,
    IncomingUpload,
    InteractiveMessenger,
)
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.statuses import QUEUE_ACTIVE_STATUSES
from orca_auto.core.utils.lock import file_lock

from .._orca_stage_materialization import safe_name
from ..activity import cancel_activity, clear_activities, list_activities
from . import remote_admission
from .action_registry import ActionKind, ActionRegistry, ActionStore, RegisteredAction
from .replies import (
    code,
    code_block,
    error_message,
    field_row,
    info_message,
    raw,
    reply_message,
    text,
)
from .replies import line as reply_line
from .settings import BotSettings

LOGGER = logging.getLogger(__name__)

SubmissionKind = Literal["orca", "workflow", "unknown"]

# Uploaded archives and their durable state live under this hidden directory.
# A fixed store-owned archive path and opaque upload id prevent provider paths
# from becoming authority at the application boundary.
_UPLOAD_STAGING_DIRNAME = ".uploads"
_UPLOAD_LEGACY_DIRNAME = ".legacy"
_UPLOAD_EXTRACT_DIRNAME = ".extract"
_UPLOAD_PUBLISH_LOCK_NAME = ".upload-publish.lock"
_UPLOAD_PUBLISH_MARKER = ".orca-auto-upload"
_UPLOAD_ACTION_TTL_SECONDS = 5 * 60.0
_UPLOAD_ACTION_PREFIX = "act_"
# Largest activity table (chars) rendered inside a Discord embed description
# (limit 4096, minus room for the code fence). Larger tables fall back to the
# paginated plain path so no rows are dropped.
_LIST_EMBED_MAX_CHARS = 3900


# Discord allows five action rows. Reserve the fifth row for refresh/clear so
# the same neutral card renders on every provider without adapter-side loss.
_MAX_LIST_CANCEL_BUTTONS = 4
_LIST_BUTTON_LABEL_WIDTH = 30
_CANCEL_BINDING_PREFIX = "v1:"
_CANCEL_VERSION_FIELDS = (
    "activity_id",
    "kind",
    "engine",
    "source",
    "submitted_at",
    "cancel_target",
)


@dataclass(frozen=True)
class BotApplicationDeps:
    """Injectable activity/presentation seams for deterministic application tests."""

    list_activities: Callable[..., dict[str, Any]] = list_activities
    clear_activities: Callable[..., dict[str, Any]] = clear_activities
    cancel_activity: Callable[..., dict[str, Any]] = cancel_activity
    filter_activity_items: Callable[..., list[dict[str, Any]]] = filter_activity_items
    queue_list_text_presentation: Callable[..., Any] = queue_list_text_presentation
    activity_counter_config_path: Callable[..., str | None] = activity_counter_config_path
    count_global_active_simulations: Callable[..., int] = count_global_active_simulations
    queue_list_default_visible_items: Callable[..., list[dict[str, Any]]] = (
        queue_list_default_visible_items
    )
    queue_list_display_rows: Callable[..., list[tuple[int, dict[str, Any]]]] = (
        queue_list_display_rows
    )
    queue_list_text_lines: Callable[..., list[str]] = queue_list_text_lines
    queue_clear_lines: Callable[[dict[str, Any]], list[str]] = queue_clear_lines
    status_icon: Callable[[str], str] = activity_status_icon


@dataclass(frozen=True)
class SubmissionReceipt:
    """Durable submission outcome at the queue/workflow persistence boundary.

    ``committed=None`` is deliberately distinct from a failed submission.  It
    means an unexpected error made the commit outcome impossible to prove, so
    callers must retain the run directory for reconciliation rather than risk
    deleting work that a queue worker may already own.
    """

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
class BotApplication:
    """Shared orca_auto bot application used by every native adapter."""

    settings: BotSettings
    actions: ActionStore = field(default_factory=ActionRegistry)
    deps: BotApplicationDeps = field(default_factory=BotApplicationDeps)
    upload_policy: UploadPolicy | None = None
    upload_sessions: UploadSessionStore | None = None

    def __post_init__(self) -> None:
        """Create the durable upload store from the trusted server policy."""

        if self.upload_sessions is not None:
            return
        policy = self.upload_policy
        runs_root = self._runs_root()
        if policy is None or runs_root is None:
            return
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

    def _activity_payload(
        self,
        *,
        child_job_engines: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        return self.deps.list_activities(
            workflow_root=self.settings.workflow_root,
            crest_config=self.settings.crest_config,
            xtb_config=self.settings.xtb_config,
            orca_config=self.settings.orca_config,
            child_job_engines=child_job_engines,
        )

    def _list_clear_text(self) -> str:
        payload = self.deps.clear_activities(
            workflow_root=self.settings.workflow_root,
            crest_config=self.settings.crest_config,
            xtb_config=self.settings.xtb_config,
            orca_config=self.settings.orca_config,
        )
        return "\n".join(self.deps.queue_clear_lines(payload))

    def _list_text(
        self,
        filter_status: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> str:
        payload = payload or self._activity_payload(
            child_job_engines=() if not filter_status else None,
        )
        rows = [item for item in payload.get("activities", []) if isinstance(item, dict)]
        if filter_status:
            rows = self.deps.filter_activity_items(rows, statuses=(filter_status,))
        presentation = self.deps.queue_list_text_presentation(
            payload,
            request=QueueListPresentationRequest(
                visible_items=rows,
                config_hints=(
                    self.settings.orca_config,
                    self.settings.crest_config,
                    self.settings.xtb_config,
                ),
                default_visible_items=not filter_status,
                show_workflow_context=True,
                visible_workflow_child_engines=("orca",) if not filter_status else None,
                include_id=False,
            ),
            deps=QueueListPresentationDeps(
                activity_counter_config_path=self.deps.activity_counter_config_path,
                count_global_active_simulations=self.deps.count_global_active_simulations,
                queue_list_default_visible_items=self.deps.queue_list_default_visible_items,
                queue_list_display_rows=self.deps.queue_list_display_rows,
                queue_list_text_lines=self.deps.queue_list_text_lines,
            ),
        )
        return "\n".join(presentation.lines)

    def _active_cancel_targets(
        self,
        *,
        payload: dict[str, Any] | None = None,
        default_visible: bool = True,
    ) -> list[dict[str, Any]]:
        payload = payload or self._activity_payload(child_job_engines=("orca",))
        items = [item for item in payload.get("activities", []) if isinstance(item, dict)]
        visible = self.deps.queue_list_default_visible_items(items) if default_visible else items
        return [
            item
            for item in visible
            if str(item.get("status", "")).strip().lower() in QUEUE_ACTIVE_STATUSES
        ]

    @staticmethod
    def _activity_version(item: dict[str, Any]) -> str:
        payload = [str(item.get(field_name) or "") for field_name in _CANCEL_VERSION_FIELDS]
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _cancel_binding(cls, item: dict[str, Any]) -> str:
        activity_id = str(item.get("activity_id") or "").strip()
        if not activity_id:
            raise ValueError("Activity has no immutable activity_id.")
        return _CANCEL_BINDING_PREFIX + json.dumps(
            {
                "activity_id": activity_id,
                "label": str(item.get("label") or activity_id).strip(),
                "version": cls._activity_version(item),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode_cancel_binding(binding: str) -> tuple[str, str, str]:
        if not binding.startswith(_CANCEL_BINDING_PREFIX):
            raise ValueError("Cancellation target binding is invalid.")
        try:
            payload = json.loads(binding.removeprefix(_CANCEL_BINDING_PREFIX))
        except (TypeError, ValueError):
            raise ValueError("Cancellation target binding is invalid.") from None
        if not isinstance(payload, dict):
            raise ValueError("Cancellation target binding is invalid.")
        activity_id = str(payload.get("activity_id") or "").strip()
        label = str(payload.get("label") or activity_id).strip()
        version = str(payload.get("version") or "").strip()
        if not activity_id or not version:
            raise ValueError("Cancellation target binding is invalid.")
        return activity_id, label, version

    def _activity_items(self) -> list[dict[str, Any]]:
        payload = self._activity_payload(child_job_engines=None)
        return [item for item in payload.get("activities", []) if isinstance(item, dict)]

    def _resolve_cancel_binding(self, target: str) -> str:
        normalized = target.strip()
        if not normalized:
            raise ValueError("Cancel target is empty.")
        items = self._activity_items()
        exact = [
            item
            for item in items
            if normalized
            in {
                str(item.get("activity_id") or "").strip(),
                str(item.get("cancel_target") or "").strip(),
            }
        ]
        if len(exact) > 1:
            raise ValueError(f"Ambiguous activity target: {normalized}.")
        matches = exact
        if not matches:
            matches = [
                item
                for item in items
                if normalized
                in {str(alias).strip() for alias in item.get("aliases", []) if str(alias).strip()}
            ]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous activity target: {normalized}.")
        if not matches:
            raise LookupError(f"Activity target not found: {normalized}")
        item = matches[0]
        status = str(item.get("status") or "").strip().lower()
        if status not in QUEUE_ACTIVE_STATUSES:
            raise ValueError(f"Activity is no longer cancellable: {normalized}")
        return self._cancel_binding(item)

    def _validated_cancel_target(self, binding: str) -> tuple[str, str]:
        activity_id, label, expected_version = self._decode_cancel_binding(binding)
        matches = [
            item
            for item in self._activity_items()
            if str(item.get("activity_id") or "").strip() == activity_id
        ]
        if len(matches) != 1:
            raise LookupError("Activity changed or no longer exists; cancellation was not sent.")
        item = matches[0]
        status = str(item.get("status") or "").strip().lower()
        if status not in QUEUE_ACTIVE_STATUSES or self._activity_version(item) != expected_version:
            raise LookupError("Activity changed or is no longer active; cancellation was not sent.")
        return activity_id, label

    @staticmethod
    def _list_button_label(item: dict[str, Any]) -> str:
        icon = activity_status_icon(str(item.get("status", "")))
        name = str(item.get("label") or item.get("activity_id") or "?").strip()
        if len(name) > _LIST_BUTTON_LABEL_WIDTH:
            name = name[: _LIST_BUTTON_LABEL_WIDTH - 1] + "…"
        return f"Cancel {icon} {name}"

    def _list_actions(
        self,
        *,
        address: ConversationAddress,
        actor: Actor,
        filter_status: str = "",
        payload: dict[str, Any] | None = None,
    ) -> tuple[tuple[CardAction, ...], ...]:
        active = self._active_cancel_targets(
            payload=payload,
            default_visible=not bool(filter_status),
        )
        if filter_status:
            active = self.deps.filter_activity_items(active, statuses=(filter_status,))
        active = active[:_MAX_LIST_CANCEL_BUTTONS]
        bindings = [self._cancel_binding(item) for item in active]
        specs: list[tuple[ActionKind, str]] = [("cancel_prompt", binding) for binding in bindings]
        specs.extend((("list_refresh", filter_status), ("list_clear", filter_status)))
        action_ids = self.actions.issue_group(specs, address=address, actor=actor)

        rows: list[tuple[CardAction, ...]] = []
        for item, action_id in zip(
            active,
            action_ids[: len(bindings)],
            strict=True,
        ):
            rows.append((CardAction(action_id, self._list_button_label(item)),))
        rows.append(
            (
                CardAction(action_ids[len(bindings)], "Refresh"),
                CardAction(action_ids[len(bindings) + 1], "Clear finished"),
            )
        )
        return tuple(rows)

    def _list_reply(
        self,
        *,
        address: ConversationAddress,
        actor: Actor,
        filter_status: str = "",
    ) -> BotReply:
        payload = self._activity_payload(
            child_job_engines=() if not filter_status else None,
        )
        table = self._list_text(filter_status, payload=payload)
        title = "Activities" if not filter_status else f"{filter_status.capitalize()} activities"
        # Keep the embed only when the table fits Discord's description limit;
        # for an oversized table fall back to the paginated plain path (message
        # omitted) so every row is delivered instead of being truncated.
        message = (
            reply_message(title, reply_line(code_block(table)))
            if len(table) <= _LIST_EMBED_MAX_CHARS
            else None
        )
        return BotReply(
            table,
            format="preformatted",
            actions=self._list_actions(
                address=address,
                actor=actor,
                filter_status=filter_status,
                payload=payload,
            ),
            message=message,
        )

    def _cancel_confirmation_reply(
        self,
        binding: str,
        *,
        address: ConversationAddress,
        actor: Actor,
    ) -> BotReply:
        activity_id, label, _version = self._decode_cancel_binding(binding)
        confirm_id, dismiss_id = self.actions.issue_group(
            (("cancel_confirm", binding), ("cancel_dismiss", binding)),
            address=address,
            actor=actor,
        )
        return BotReply(
            f"Cancel {label} ({activity_id})?",
            actions=(
                (
                    CardAction(confirm_id, "Yes, cancel"),
                    CardAction(dismiss_id, "Keep running"),
                ),
            ),
            message=reply_message(
                "Cancel this activity?",
                field_row("Activity", code(label), inline=True),
                field_row("ID", code(activity_id), inline=True),
                severity="warning",
            ),
        )

    def _cancel_result(self, binding: str) -> BotReply:
        try:
            target, _label = self._validated_cancel_target(binding)
            payload = self.deps.cancel_activity(
                target=target,
                workflow_root=self.settings.workflow_root,
                crest_config=self.settings.crest_config,
                xtb_config=self.settings.xtb_config,
                orca_config=self.settings.orca_config,
                orca_repo_root=self.settings.orca_repo_root,
            )
        except (LookupError, ValueError) as exc:
            return BotReply(str(exc), message=error_message("Cancellation failed", str(exc)))
        label = str(payload.get("label", payload.get("activity_id", target)))
        status = str(payload.get("status", "unknown"))
        return BotReply(
            f"{self.deps.status_icon(status)} {label}\nstatus: {status}",
            message=reply_message(
                "Cancellation requested",
                field_row("Activity", code(label), inline=True),
                field_row("Status", code(status), inline=True),
                severity="success",
            ),
        )

    # -- Upload → safe-extract → queue submission ------------------------------

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

    def stage_upload_path(self, filename: str) -> Path:
        """Return a compatibility-only path for legacy provider/test callers.

        Production adapters must use :meth:`reserve_upload` so quota is reserved
        before download.  A legacy file is adopted into a durable session before
        it can be inspected and never shares the store root with state files.
        """

        runs_root = self._runs_root()
        if runs_root is None:
            raise RuntimeError("runs_root is not configured; uploads are unavailable")
        staging = self._staging_dir(runs_root) / _UPLOAD_LEGACY_DIRNAME
        staging.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(8)
        safe = safe_name(Path(filename).name, fallback="upload")
        return staging / f"{token}__{safe}"

    def _discard_legacy_upload_path(self, archive_path: str) -> None:
        """Delete only a regular file issued by ``stage_upload_path``."""

        runs_root = self._runs_root()
        if runs_root is None:
            return
        source = Path(archive_path)
        legacy_root = (self._staging_dir(runs_root) / _UPLOAD_LEGACY_DIRNAME).resolve()
        try:
            if source.is_symlink():
                return
            resolved = source.resolve(strict=True)
            if resolved.parent == legacy_root and resolved.is_file():
                resolved.unlink()
        except OSError:
            LOGGER.debug("legacy_upload_cleanup_skip")

    def _adopt_legacy_upload(self, upload: IncomingUpload) -> UploadSession:
        runs_root = self._runs_root()
        if runs_root is None:
            raise RuntimeError("Upload staging is not configured.")
        source = Path(upload.archive_path)
        legacy_root = (self._staging_dir(runs_root) / _UPLOAD_LEGACY_DIRNAME).resolve()
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise UploadArchiveError("legacy staged archive is missing") from exc
        if resolved.parent != legacy_root or resolved.is_symlink() or not resolved.is_file():
            raise UploadArchiveError("legacy staged archive is outside its private staging area")

        reservation = self.reserve_upload(
            address=upload.address,
            actor=upload.actor,
            message_id=upload.message_id or f"legacy:{resolved.name}",
            attachment_ids=(upload.attachment_id or resolved.name,),
            expected_bytes=upload.size,
        )
        if not reservation.created:
            _safe_unlink(resolved)
            return reservation.session
        try:
            resolved.rename(reservation.session.archive_path)
            return self.finalize_upload(reservation.session.upload_id)
        except BaseException:
            self.abandon_upload(
                reservation.session.upload_id,
                "legacy upload adoption failed",
            )
            raise

    def _incoming_upload_session(self, upload: IncomingUpload) -> UploadSession:
        store = self._require_upload_sessions()
        legacy_adopted = upload.upload_id is None
        if upload.upload_id is None:
            session = self._adopt_legacy_upload(upload)
        else:
            session = store.get(upload.upload_id)
        if session.binding != self._upload_binding(upload.address, upload.actor):
            raise UploadBindingMismatchError("upload identity binding does not match")
        if upload.message_id is not None and session.message_id != upload.message_id:
            raise UploadBindingMismatchError("upload message binding does not match")
        if upload.attachment_id is not None and upload.attachment_id not in session.attachment_ids:
            raise UploadBindingMismatchError("upload attachment binding does not match")
        if not legacy_adopted:
            try:
                supplied_path = Path(upload.archive_path).expanduser().resolve()
            except OSError as exc:
                raise UploadArchiveError("staged archive is missing") from exc
            if supplied_path != session.archive_path:
                raise UploadBindingMismatchError("upload archive path is not store-owned")
        if session.state is UploadState.RECEIVING and session.actual_bytes is None:
            # Compatibility for callers that downloaded into a reservation but
            # did not yet adopt the explicit finalize API.
            session = self.finalize_upload(session.upload_id)
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

        self._check_provider(upload.address, messenger)
        policy = self.upload_policy
        if policy is None or not policy.enabled:
            if upload.upload_id is not None:
                self.abandon_upload(upload.upload_id, "uploads disabled")
            else:
                self._discard_legacy_upload_path(upload.archive_path)
            status, reply = (
                "upload-disabled",
                BotReply(
                    "File uploads are disabled.",
                    message=error_message("Uploads disabled", "File uploads are disabled."),
                ),
            )
        elif self.upload_sessions is None:
            if upload.upload_id is None:
                self._discard_legacy_upload_path(upload.archive_path)
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
        with file_lock(
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

    def _upload_command_available(self, provider: str) -> bool:
        policy = self.upload_policy
        return (
            provider == "discord"
            and policy is not None
            and policy.enabled
            and self.upload_sessions is not None
        )

    def _help_reply(self, prefix: str, *, provider: str) -> BotReply:
        commands = [
            (f"{prefix}list", "Show unified activities"),
            (f"{prefix}list clear", "Remove completed, failed, and cancelled entries"),
            (f"{prefix}list running", "Show running activities only"),
            (f"{prefix}list failed", "Show failed activities only"),
            (f"{prefix}cancel TARGET", "Ask to cancel a workflow or queued job"),
        ]
        if self._upload_command_available(provider):
            commands.append((f"{prefix}run", "Attach a .zip/.tar.gz run-dir to queue it"))
        commands.append((f"{prefix}help", "Show this help message"))
        fallback = "orca_auto bot commands\n\n" + "\n".join(
            f"{name} — {desc}" for name, desc in commands
        )
        message = reply_message(
            "Commands",
            *(reply_line(code(name), raw(f" — {desc}")) for name, desc in commands),
        )
        return BotReply(fallback, message=message)

    @staticmethod
    def _check_provider(
        address: ConversationAddress,
        messenger: InteractiveMessenger,
    ) -> None:
        if messenger.provider.strip().lower() != address.provider:
            raise ValueError(
                "interactive messenger provider does not match the incoming conversation"
            )

    @staticmethod
    def _command_name(command: IncomingCommand) -> str:
        return command.command.strip().lstrip("/!").split("@", 1)[0].lower()

    def dispatch_command(
        self,
        command: IncomingCommand,
        *,
        messenger: InteractiveMessenger,
    ) -> str:
        """Dispatch one normalized provider command and send its reply."""

        self._check_provider(command.address, messenger)
        name = self._command_name(command)
        args = command.args.strip()
        prefix = "!" if command.address.provider == "discord" else "/"
        status = "unknown-command"
        if name == "list":
            if args.lower() == "clear":
                clear_text = self._list_clear_text()
                reply = BotReply(
                    clear_text,
                    format="preformatted",
                    message=reply_message(
                        "Cleared", reply_line(code_block(clear_text)), severity="success"
                    ),
                )
                status = "list-cleared"
            else:
                reply = self._list_reply(
                    address=command.address,
                    actor=command.actor,
                    filter_status=args.lower(),
                )
                status = "list-sent"
        elif name == "cancel":
            if not args:
                reply = BotReply(
                    f"Usage: {prefix}cancel TARGET",
                    message=reply_message(
                        "Cancel a workflow or job",
                        reply_line(text("Usage: "), code(f"{prefix}cancel TARGET")),
                    ),
                )
                status = "cancel-usage"
            else:
                # Cancellation is never executed from the text command.  Even a
                # target too long for native callback data is represented by a
                # short opaque registry id and must be confirmed.
                try:
                    binding = self._resolve_cancel_binding(args)
                except (LookupError, ValueError) as exc:
                    reply = BotReply(
                        str(exc), message=error_message("Cancel target not found", str(exc))
                    )
                    status = "cancel-target-invalid"
                else:
                    reply = self._cancel_confirmation_reply(
                        binding,
                        address=command.address,
                        actor=command.actor,
                    )
                    status = "cancel-confirmation-sent"
        elif name == "run":
            if command.address.provider != "discord":
                reply = BotReply(
                    "File uploads are available only through Discord.",
                    message=error_message(
                        "Uploads unavailable", "File uploads are available only through Discord."
                    ),
                )
                status = "run-unavailable"
            elif self.upload_policy is None or not self.upload_policy.enabled:
                reply = BotReply(
                    "File uploads are disabled.",
                    message=error_message("Uploads disabled", "File uploads are disabled."),
                )
                status = "run-disabled"
            elif self.upload_sessions is None:
                reply = BotReply(
                    "Upload staging is not configured.",
                    message=error_message(
                        "Uploads not configured", "Upload staging is not configured."
                    ),
                )
                status = "run-misconfigured"
            else:
                reply = BotReply(
                    f"Attach a .zip or .tar.gz run-dir to {prefix}run to queue it.",
                    message=reply_message(
                        "Upload a run-dir",
                        reply_line(
                            raw("Attach a .zip or .tar.gz run-dir to "),
                            code(f"{prefix}run"),
                            raw(" to queue it."),
                        ),
                    ),
                )
                status = "run-usage"
        elif name in {"help", "start"}:
            reply = self._help_reply(prefix, provider=command.address.provider)
            status = "help-sent"
        else:
            reply = BotReply(
                f"Unknown command: {prefix}{name}\nType {prefix}help for available commands.",
                message=error_message(
                    "Unknown command",
                    f"Type {prefix}help for available commands.",
                ),
            )

        result = messenger.send_reply(command.address, reply)
        return status if result.sent else f"{status}-delivery-failed"

    @staticmethod
    def _invalid_action_text(status: str) -> str:
        return {
            "expired": "Action expired. Run the command again.",
            "wrong_address": "Action belongs to another conversation.",
            "wrong_actor": "Action belongs to another user.",
            "wrong_binding": "Action belongs to another conversation or user.",
        }.get(status, "Action is unavailable or was already used.")

    @staticmethod
    def _clear_origin_actions(
        action: IncomingAction,
        messenger: InteractiveMessenger,
    ) -> None:
        if action.message_id is None:
            return
        try:
            result = messenger.edit_actions(action.address, action.message_id, None)
            if not result.sent:
                LOGGER.warning("interactive_action_cleanup_failed: %s", result.error)
        except Exception:  # noqa: BLE001 - best-effort UI cleanup at transport boundary
            LOGGER.warning("interactive_action_cleanup_failed", exc_info=True)

    @staticmethod
    def _acknowledge(
        incoming: IncomingAction,
        text: str,
        messenger: InteractiveMessenger,
    ) -> None:
        result = messenger.acknowledge(incoming, text)
        if not result.sent:
            LOGGER.warning("interactive_action_ack_failed: %s", result.error)

    @staticmethod
    def _send_action_reply(
        incoming: IncomingAction,
        reply: BotReply,
        messenger: InteractiveMessenger,
    ) -> None:
        result = messenger.send_reply(incoming.address, reply)
        if not result.sent:
            LOGGER.warning("interactive_action_reply_failed: %s", result.error)

    def _dispatch_registered_action(
        self,
        registered: RegisteredAction,
        incoming: IncomingAction,
        messenger: InteractiveMessenger,
    ) -> str:
        self._clear_origin_actions(incoming, messenger)
        if registered.kind == "cancel_prompt":
            self._acknowledge(incoming, "Confirmation required.", messenger)
            self._send_action_reply(
                incoming,
                self._cancel_confirmation_reply(
                    registered.target,
                    address=incoming.address,
                    actor=incoming.actor,
                ),
                messenger,
            )
            return "cancel-confirmation-sent"
        if registered.kind == "cancel_confirm":
            reply = self._cancel_result(registered.target)
            self._acknowledge(incoming, "Cancellation processed.", messenger)
            self._send_action_reply(incoming, reply, messenger)
            self._send_action_reply(
                incoming,
                self._list_reply(address=incoming.address, actor=incoming.actor),
                messenger,
            )
            return "cancel-processed"
        if registered.kind == "cancel_dismiss":
            self._acknowledge(incoming, "Cancellation dismissed.", messenger)
            self._send_action_reply(
                incoming,
                BotReply(
                    "Cancellation dismissed.",
                    message=info_message(
                        "Kept running", "Cancellation dismissed — the activity keeps running."
                    ),
                ),
                messenger,
            )
            return "cancel-dismissed"
        if registered.kind == "list_refresh":
            self._acknowledge(incoming, "Refreshed.", messenger)
            self._send_action_reply(
                incoming,
                self._list_reply(
                    address=incoming.address,
                    actor=incoming.actor,
                    filter_status=registered.target,
                ),
                messenger,
            )
            return "list-refreshed"
        if registered.kind == "run_confirm":
            text = "This legacy upload action is no longer available. Upload the file again."
            self._acknowledge(incoming, text, messenger)
            self._send_action_reply(
                incoming,
                BotReply(text, message=error_message("Action unavailable", text)),
                messenger,
            )
            return "run-unavailable"
        if registered.kind == "run_dismiss":
            text = "This legacy upload action is no longer available."
            self._acknowledge(incoming, text, messenger)
            self._send_action_reply(
                incoming,
                BotReply(text, message=error_message("Action unavailable", text)),
                messenger,
            )
            return "run-unavailable"
        if registered.kind == "list_clear":
            self._acknowledge(incoming, "Finished entries cleared.", messenger)
            clear_text = self._list_clear_text()
            self._send_action_reply(
                incoming,
                BotReply(
                    clear_text,
                    format="preformatted",
                    message=reply_message(
                        "Cleared", reply_line(code_block(clear_text)), severity="success"
                    ),
                ),
                messenger,
            )
            self._send_action_reply(
                incoming,
                self._list_reply(
                    address=incoming.address,
                    actor=incoming.actor,
                    filter_status=registered.target,
                ),
                messenger,
            )
            return "list-cleared"
        raise AssertionError(f"unsupported registered action kind: {registered.kind}")

    def _dispatch_upload_action(
        self,
        incoming: IncomingAction,
        messenger: InteractiveMessenger,
    ) -> str | None:
        """Consume a durable upload action, or return ``None`` when it is not one."""

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
            self._clear_origin_actions(incoming, messenger)
            if consumed.action.kind is UploadActionKind.DISMISS:
                self._acknowledge(incoming, "Upload discarded.", messenger)
                self._send_action_reply(
                    incoming,
                    BotReply(
                        "Upload discarded.",
                        message=info_message("Upload discarded", "The upload was discarded."),
                    ),
                    messenger,
                )
                return "run-dismissed"

            self._acknowledge(incoming, "Submitting…", messenger)
            outcome = self._run_submit_session(consumed.session)
            self._send_action_reply(incoming, outcome.reply, messenger)
            return outcome.status

        text = self._invalid_action_text(status)
        self._acknowledge(incoming, text, messenger)
        self._send_action_reply(
            incoming,
            BotReply(text, message=error_message("Action unavailable", text)),
            messenger,
        )
        return status

    def dispatch_action(
        self,
        action: IncomingAction,
        *,
        messenger: InteractiveMessenger,
    ) -> str:
        """Resolve one bound one-time action and perform its application effect."""

        self._check_provider(action.address, messenger)
        upload_status = self._dispatch_upload_action(action, messenger)
        if upload_status is not None:
            return upload_status
        resolution = self.actions.consume(
            action.action_id,
            address=action.address,
            actor=action.actor,
        )
        if resolution.action is None:
            text = self._invalid_action_text(resolution.status)
            self._acknowledge(action, text, messenger)
            self._send_action_reply(
                action,
                BotReply(text, message=error_message("Action unavailable", text)),
                messenger,
            )
            return resolution.status
        return self._dispatch_registered_action(resolution.action, action, messenger)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.debug("upload_unlink_skip: %s", path.name)


def _replace_directory(source: Path, destination: Path) -> None:
    """Small crash-injection seam around the atomic publication primitive."""

    os.replace(source, destination)


def dispatch_command(
    application: BotApplication,
    command: IncomingCommand,
    *,
    messenger: InteractiveMessenger,
) -> str:
    """Adapter-facing functional facade for command dispatch."""

    return application.dispatch_command(command, messenger=messenger)


def dispatch_upload(
    application: BotApplication,
    upload: IncomingUpload,
    *,
    messenger: InteractiveMessenger,
) -> str:
    """Adapter-facing functional facade for upload dispatch."""

    return application.dispatch_upload(upload, messenger=messenger)


def dispatch_action(
    application: BotApplication,
    action: IncomingAction,
    *,
    messenger: InteractiveMessenger,
) -> str:
    """Adapter-facing functional facade for component dispatch."""

    return application.dispatch_action(action, messenger=messenger)


__all__ = [
    "BotApplication",
    "BotApplicationDeps",
    "RunSubmissionOutcome",
    "SubmissionReceipt",
    "dispatch_action",
    "dispatch_command",
    "dispatch_upload",
]
