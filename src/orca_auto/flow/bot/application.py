"""Provider-neutral command and component dispatch for the orca_auto bot."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
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
from orca_auto.core.statuses import QUEUE_ACTIVE_STATUSES
from orca_auto.core.utils.lock import file_lock

from .._orca_stage_materialization import safe_name
from ..activity import cancel_activity, clear_activities, list_activities
from .action_registry import ActionKind, ActionRegistry, ActionStore, RegisteredAction
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
_REMOTE_WORKFLOW_COUNT_LIMITS = {
    "max_orca_stages": 20,
    "max_crest_candidates": 8,
    "max_xtb_stages": 8,
    "max_candidates": 20,
    "max_scan_extensions": 4,
    "max_xtb_handoff_retries": 4,
}
_REMOTE_ROUTE_LINE_KEYS = frozenset({"route_line", "orca_route_line", "orca_optts_route_line"})
_REMOTE_SCAN_POINTS_LIMIT = 200
_REMOTE_SCAN_COORDINATE_RE = re.compile(
    r"\A\s*(?P<kind>[BADbad])\s+(?P<atoms>\d+(?:\s+\d+){1,3})\s*=\s*"
    r"(?P<start>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s*,\s*"
    r"(?P<end>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s*,\s*"
    r"(?P<points>\d+)\s*\Z"
)
_REMOTE_ORCA_SIMPLE_INPUT_REFERENCE_KEYS = frozenset(
    {"%cclib", "%ljcoefficients", "%moinp", "%pointcharges"}
)
_REMOTE_ORCA_BLOCK_INPUT_REFERENCE_KEYS = frozenset(
    {
        "hessfile",
        "hess_filename",
        "inhessname",
        "ircinithess",
        "moinp",
        "neb_end_xyzfile",
        "neb_end_pdbfile",
        "neb_restart_xyzfile",
        "neb_ts_xyzfile",
        "orcafffilename",
        "product_pdbfile",
        "restart_allxyzfile",
        "ts_pdbfile",
    }
)
_REMOTE_ORCA_BLOCK_CONTAINED_OUTPUT_KEYS = frozenset(
    {"neb_restart_gbwname", "restart_gbw_basename"}
)
_REMOTE_ORCA_FORBIDDEN_IDENTIFIERS = frozenset(
    {
        "%compound",
        "%compound_file",
        "$new_job",
        "$newjob",
        "compound",
        "compound_file",
        "ext_params",
        "ext_args",
        "extargs",
        "extparams",
        "extopt",
        "frag1_methodfile",
        "frag1methodfile",
        "frag2_methodfile",
        "frag2methodfile",
        "gcpmethod",
        "openfile",
        "progext",
        "progplot",
        "qm2customfile",
        "qm2_customfile",
        "sys_cmd",
        "write2file",
        "xtbinputstring",
        "xtbinputstring2",
    }
)
_REMOTE_ORCA_IDENTIFIER_RE = re.compile(r"\A[%$A-Za-z_][%$A-Za-z0-9_]*")
_REMOTE_ORCA_SAFE_PATH_COMPONENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}\Z")
_REMOTE_ORCA_SHELL_META = frozenset("$`;|&<>")


def _remote_orca_identifier_is_forbidden(identifier: str) -> bool:
    normalized = identifier.strip().lower()
    return (
        normalized in _REMOTE_ORCA_FORBIDDEN_IDENTIFIERS
        or normalized.startswith("%compound")
        or normalized.startswith("compound_file")
        or (normalized.startswith("prog") and len(normalized) > len("prog"))
    )


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
        return BotReply(
            self._list_text(filter_status, payload=payload),
            format="preformatted",
            actions=self._list_actions(
                address=address,
                actor=actor,
                filter_status=filter_status,
                payload=payload,
            ),
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
            return BotReply(str(exc))
        label = str(payload.get("label", payload.get("activity_id", target)))
        status = str(payload.get("status", "unknown"))
        return BotReply(f"{self.deps.status_icon(status)} {label}\nstatus: {status}")

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
            status, reply = "upload-disabled", BotReply("File uploads are disabled.")
        elif self.upload_sessions is None:
            if upload.upload_id is None:
                self._discard_legacy_upload_path(upload.archive_path)
            status, reply = "upload-misconfigured", BotReply("Upload staging is not configured.")
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
                    reply = BotReply("This upload was already submitted.")
                elif session.state in {
                    UploadState.PROCESSING,
                    UploadState.PUBLISHED,
                    UploadState.AMBIGUOUS,
                }:
                    status = "upload-already-processing"
                    reply = BotReply(
                        "This upload is already being processed; check the list command."
                    )
                else:
                    status = "upload-unavailable"
                    reply = BotReply("This upload is no longer available.")
            except UploadRejected as exc:
                if "session" in locals():
                    self.abandon_upload(session.upload_id, f"archive rejected: {exc.reason}")
                status, reply = "upload-rejected", BotReply(f"Rejected: {exc.reason}")
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
                status, reply = "upload-rejected", BotReply(f"Rejected: {exc}")

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
    def _uploaded_flow_manifest(job_dir: Path) -> dict[str, Any]:
        from orca_auto.flow.manifest import load_flow_manifest

        try:
            return load_flow_manifest(job_dir)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize parser/filesystem failures
            raise ValueError("uploaded flow.yaml could not be safely parsed") from exc

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
            manifest = BotApplication._uploaded_flow_manifest(job_dir)
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
            return RunSubmissionOutcome(
                "run-submission-failed",
                BotReply(f"Submission failed for {job_dir.name}. See the bot log."),
                receipt,
            )
        if receipt.committed is False:
            return RunSubmissionOutcome(
                "run-submission-conflict",
                BotReply(
                    f"{job_dir.name} is already owned by an existing submission; "
                    "its files were preserved. Check the list command before retrying."
                ),
                receipt,
            )
        if receipt.committed is None:
            return RunSubmissionOutcome(
                "run-submission-uncertain",
                BotReply(
                    f"Submission outcome is uncertain for {job_dir.name}; its files were "
                    "preserved. Check the list command and bot log before retrying."
                ),
                receipt,
            )

        identifier = f" (id: {receipt.submission_id})" if receipt.submission_id else ""
        if receipt.failure_reason == "submission_conflict":
            return RunSubmissionOutcome(
                "run-already-submitted",
                BotReply(
                    f"{job_dir.name} is already queued{identifier}; its files were preserved. "
                    "Track it with the list command."
                ),
                receipt,
            )
        if receipt.detail:
            return RunSubmissionOutcome(
                "run-submitted-with-warning",
                BotReply(
                    f"Queued {job_dir.name}{identifier}, but a post-submission step failed. "
                    "The committed run was preserved; check the bot log."
                ),
                receipt,
            )
        return RunSubmissionOutcome(
            "run-submitted",
            BotReply(f"Queued {job_dir.name}{identifier}. Track it with the list command."),
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
                BotReply("File uploads are disabled."),
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
                BotReply("Upload expired or changed before it could be processed."),
            )
        except Exception as exc:  # noqa: BLE001 - stable durability boundary
            LOGGER.warning("upload_archive_identity_check_failed", exc_info=True)
            return RunSubmissionOutcome("run-unavailable", BotReply(str(exc)))

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
                BotReply(f"Rejected: {reason}"),
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
            return RunSubmissionOutcome(
                "run-submission-uncertain",
                BotReply(
                    "Publication outcome is uncertain; any visible files were preserved. "
                    "Check the bot log before retrying."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - no public namespace change was observed
            reason = self._exception_detail(exc)
            try:
                store.mark_failed(session.upload_id, reason=reason)
            except Exception:  # noqa: BLE001 - no downstream commit was attempted
                LOGGER.warning("upload_publish_failure_state_failed", exc_info=True)
            return RunSubmissionOutcome(
                "run-publish-failed",
                BotReply("Could not safely publish the upload. See the bot log."),
            )

        try:
            store.mark_published(session.upload_id, published_path=published_dir)
        except Exception as exc:  # noqa: BLE001 - publication exists; never delete it
            reason = f"published before state persistence: {self._exception_detail(exc)}"
            self._mark_upload_ambiguous(session.upload_id, reason)
            return RunSubmissionOutcome(
                "run-submission-uncertain",
                BotReply(
                    f"Publication outcome is uncertain for {published_dir.name}; its files "
                    "were preserved. Check the bot log before retrying."
                ),
            )

        if not publication_durable:
            reason = "published run-dir could not be durably synced"
            self._mark_upload_ambiguous(session.upload_id, reason)
            return RunSubmissionOutcome(
                "run-submission-uncertain",
                BotReply(
                    f"Publication durability is uncertain for {published_dir.name}; its files "
                    "were preserved and were not submitted. Check the bot log before retrying."
                ),
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
                return RunSubmissionOutcome(
                    "run-submission-uncertain",
                    BotReply(
                        f"Submission {receipt.submission_id} reached the downstream system, "
                        "but its local receipt could not be persisted. The run directory and "
                        "reconciliation marker were preserved."
                    ),
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

    @staticmethod
    def _workflow_commit(job_dir: Path) -> tuple[bool | None, str | None]:
        """Inspect the durable workflow file after a submission exception."""

        workflow_file = job_dir / "workflow.json"
        if not workflow_file.exists():
            return False, None
        try:
            payload = json.loads(workflow_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, None
        if not isinstance(payload, dict):
            return None, None
        workflow_id = str(payload.get("workflow_id") or "").strip()
        if not workflow_id:
            return None, None
        return True, workflow_id

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

    def _trusted_upload_resource_limits(self) -> tuple[int, int]:
        """Load server-owned per-task limits used for all remote submissions."""

        from orca_auto.core.config.schema import CommonResourceConfig
        from orca_auto.orca.config import load_config

        config_path = str(self.settings.orca_config or "").strip()
        resources = load_config(config_path).resources if config_path else CommonResourceConfig()
        return (
            max(1, int(resources.max_cores_per_task)),
            max(1, int(resources.max_memory_gb_per_task)),
        )

    @staticmethod
    def _validate_orca_resource_limits(
        job_dir: Path,
        *,
        max_cores: int,
        max_memory_gb: int,
    ) -> None:
        """Reject standalone inputs whose explicit directives exceed server caps."""

        from orca_auto.orca.resource_directives import MAXCORE_RE, NPROCS_RE, PAL_ROUTE_RE

        for inp_path in sorted(job_dir.glob("*.inp")):
            lines = inp_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            BotApplication._validate_orca_file_references(job_dir, inp_path, lines)
            core_requests: list[int] = []
            maxcore_requests: list[int] = []
            for line in lines:
                normalized_line = re.sub(
                    r"\A(?P<indent>\s*)%\s+(?=[A-Za-z])",
                    r"\g<indent>%",
                    line,
                    count=1,
                )
                nprocs_match = NPROCS_RE.search(normalized_line)
                if nprocs_match:
                    core_requests.append(int(nprocs_match.group(1)))
                stripped = normalized_line.lstrip()
                if stripped.startswith("!"):
                    route = stripped.split("#", 1)[0]
                    core_requests.extend(
                        int(match.group(1)) for match in PAL_ROUTE_RE.finditer(route)
                    )
                maxcore_match = MAXCORE_RE.match(normalized_line)
                if maxcore_match:
                    maxcore_requests.append(int(maxcore_match.group(1)))

            requested_cores = max(core_requests, default=0) or None
            if requested_cores is not None and requested_cores > max_cores:
                raise ValueError(
                    f"{inp_path.name} requests {requested_cores} cores; server limit is {max_cores}"
                )
            maxcore_mb = max(maxcore_requests, default=0)
            if maxcore_mb <= 0:
                continue
            effective_cores = requested_cores or max_cores
            requested_memory_gb = max(
                1,
                (effective_cores * maxcore_mb + 1023) // 1024,
            )
            if requested_memory_gb > max_memory_gb:
                raise ValueError(
                    f"{inp_path.name} requests about {requested_memory_gb} GiB; "
                    f"server limit is {max_memory_gb} GiB"
                )

    @staticmethod
    def _validate_orca_file_references(
        job_dir: Path,
        inp_path: Path,
        lines: list[str],
    ) -> None:
        """Confine remote ORCA input/output paths to the uploaded run directory."""

        from orca_auto.orca.input_blocks import OrcaLineToken, orca_line_tokens

        resolved_root = job_dir.resolve()

        def normalized_path_text(value: str, *, raw_token: str) -> str:
            text = value.strip()
            raw = raw_token.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
                raw = raw[1:-1]
            if not text or any(ord(character) < 32 for character in text):
                raise ValueError(f"{inp_path.name} contains an invalid file reference")
            # Backslashes are ambiguous because ORCA's quoting rules and the
            # POSIX filesystem do not agree on whether they escape or separate
            # path components. Remote ingress accepts one canonical form only.
            if "\\" in raw:
                raise ValueError(f"{inp_path.name} file references must use forward slashes")
            normalized = text.replace("\\", "/")
            pure = PurePosixPath(normalized)
            if (
                normalized.startswith(("~", "$"))
                or pure.is_absolute()
                or (len(normalized) >= 2 and normalized[1] == ":")
                or ".." in pure.parts
            ):
                raise ValueError(
                    f"{inp_path.name} file reference escapes the uploaded run directory: {text!r}"
                )
            if any(
                _REMOTE_ORCA_SAFE_PATH_COMPONENT_RE.fullmatch(component) is None
                for component in pure.parts
            ):
                raise ValueError(f"{inp_path.name} contains an unsafe ORCA filename: {text!r}")
            return normalized

        def validate_reference(
            value: str,
            *,
            raw_token: str,
            directive: str,
            must_exist: bool,
            single_component: bool = False,
        ) -> None:
            normalized = normalized_path_text(value, raw_token=raw_token)
            pure = PurePosixPath(normalized)
            if not pure.parts or pure == PurePosixPath("."):
                raise ValueError(f"{inp_path.name} has an invalid {directive} file reference")
            if single_component and len(pure.parts) != 1:
                raise ValueError(
                    f"{inp_path.name} {directive} must be one shell-safe filename component"
                )
            candidate = (resolved_root / Path(*pure.parts)).resolve()
            try:
                candidate.relative_to(resolved_root)
            except ValueError as exc:
                raise ValueError(
                    f"{inp_path.name} {directive} escapes the uploaded run directory"
                ) from exc
            if must_exist:
                if candidate.is_symlink() or not candidate.is_file():
                    raise ValueError(f"{inp_path.name} {directive} file is missing: {normalized!r}")
                return
            if candidate.is_symlink() or not candidate.parent.is_dir():
                raise ValueError(
                    f"{inp_path.name} {directive} output path is unavailable: {normalized!r}"
                )

        def value_token_after(
            tokens: Sequence[OrcaLineToken],
            index: int,
            directive: str,
        ) -> OrcaLineToken:
            value_index = index + 1
            if value_index < len(tokens) and tokens[value_index].value == "=":
                value_index += 1
            if value_index >= len(tokens):
                raise ValueError(f"{inp_path.name} has an invalid {directive} file reference")
            value_token = tokens[value_index]
            if not value_token.value.strip() or (
                not value_token.quoted and value_token.value.lower() == "end"
            ):
                raise ValueError(f"{inp_path.name} has an invalid {directive} file reference")
            return value_token

        for line in lines:
            if "\x00" in line:
                raise ValueError(f"{inp_path.name} contains a NUL byte")
            tokens = orca_line_tokens(line)
            if not tokens:
                continue
            if any(
                character in _REMOTE_ORCA_SHELL_META
                for token in tokens
                for character in token.value
            ):
                raise ValueError(
                    f"{inp_path.name} contains shell metacharacters not accepted remotely"
                )
            spaced_percent_keyword = ""
            if len(tokens) >= 2 and tokens[0].value == "%" and not tokens[1].quoted:
                spaced_percent_keyword = f"%{tokens[1].value.lower()}"
            stripped = line.lstrip()
            if stripped.startswith("!"):
                route_start = line.index("!") + 1
                for route_token in orca_line_tokens(line, start=route_start):
                    if route_token.quoted:
                        continue
                    route_identifier_match = _REMOTE_ORCA_IDENTIFIER_RE.match(route_token.value)
                    route_identifier = (
                        route_identifier_match.group(0).lower()
                        if route_identifier_match is not None
                        else ""
                    )
                    if route_token.value.lower() == "md" or (
                        route_identifier and _remote_orca_identifier_is_forbidden(route_identifier)
                    ):
                        raise ValueError(
                            f"{inp_path.name} uses a remote-disabled ORCA feature: "
                            f"{route_identifier or route_token.value.lower()}"
                        )
            if tokens[0].value.lower() == "%md" or spaced_percent_keyword == "%md":
                raise ValueError(
                    f"{inp_path.name} uses the remote-disabled ORCA molecular dynamics block"
                )
            compact_tokens = "".join(token.value for token in tokens if not token.quoted).lower()
            if "gcp(file)" in compact_tokens:
                raise ValueError(
                    f"{inp_path.name} uses a remote-disabled external GCP parameter source"
                )

            # Reject unmistakable traversal syntax even for less-common ORCA
            # file directives. Known directives below additionally require the
            # referenced input file to exist in the extracted snapshot.
            for token in tokens:
                if not token.quoted:
                    identifier_match = _REMOTE_ORCA_IDENTIFIER_RE.match(token.value)
                    identifier = (
                        identifier_match.group(0).lower() if identifier_match is not None else ""
                    )
                    if _remote_orca_identifier_is_forbidden(identifier):
                        raise ValueError(
                            f"{inp_path.name} uses a remote-disabled ORCA feature: {identifier}"
                        )
                raw_token = line[token.start : token.end]
                raw_value = raw_token.strip()
                if (
                    len(raw_value) >= 2
                    and raw_value[0] == raw_value[-1]
                    and raw_value[0] in {'"', "'"}
                ):
                    raw_value = raw_value[1:-1]
                normalized_raw = raw_value.replace("\\", "/")
                raw_path = PurePosixPath(normalized_raw)
                if (
                    normalized_raw.startswith(("/", "~", "$"))
                    or (len(normalized_raw) >= 2 and normalized_raw[1] == ":")
                    or ".." in raw_path.parts
                ):
                    raise ValueError(
                        f"{inp_path.name} contains an external file reference: {token.value!r}"
                    )

            geometry_type = ""
            geometry_path_index = -1
            if len(tokens) >= 2 and tokens[0].value == "*":
                geometry_type = tokens[1].value.lower()
                geometry_path_index = 4
            elif tokens[0].value.startswith("*"):
                geometry_type = tokens[0].value[1:].lower()
                geometry_path_index = 3
            if geometry_type.endswith("file"):
                if len(tokens) <= geometry_path_index:
                    raise ValueError(f"{inp_path.name} has an invalid {geometry_type} reference")
                value_token = tokens[geometry_path_index]
                validate_reference(
                    value_token.value,
                    raw_token=line[value_token.start : value_token.end],
                    directive=geometry_type,
                    must_exist=True,
                )

            for index, token in enumerate(tokens):
                if token.quoted:
                    continue
                keyword = token.value.lower()
                simple_input = (
                    index == 0 and keyword in _REMOTE_ORCA_SIMPLE_INPUT_REFERENCE_KEYS
                ) or (
                    index == 1
                    and spaced_percent_keyword in _REMOTE_ORCA_SIMPLE_INPUT_REFERENCE_KEYS
                )
                block_input = keyword in _REMOTE_ORCA_BLOCK_INPUT_REFERENCE_KEYS
                contained_output = keyword in _REMOTE_ORCA_BLOCK_CONTAINED_OUTPUT_KEYS
                output_base = (index == 0 and keyword == "%base") or (
                    index == 1 and spaced_percent_keyword == "%base"
                )
                if not (simple_input or block_input or contained_output or output_base):
                    continue
                value_token = value_token_after(tokens, index, token.value)
                validate_reference(
                    value_token.value,
                    raw_token=line[value_token.start : value_token.end],
                    directive=token.value,
                    must_exist=not (contained_output or output_base),
                    single_component=contained_output or output_base,
                )

    @staticmethod
    def _validate_workflow_resource_limits(
        job_dir: Path,
        *,
        max_cores: int,
        max_memory_gb: int,
    ) -> None:
        """Reject resource overrides above caps anywhere in an uploaded manifest."""

        manifest = BotApplication._uploaded_flow_manifest(job_dir)
        limits = {
            "max_cores": max_cores,
            "max_cores_per_task": max_cores,
            "max_memory_gb": max_memory_gb,
            "max_memory_gb_per_task": max_memory_gb,
        }

        def validate_route_line(value: object, *, path: str) -> None:
            from orca_auto.orca.resource_directives import PAL_ROUTE_RE

            if not isinstance(value, str):
                raise ValueError(f"flow.yaml {path} must be a single route-line string")
            route = value.strip()
            if (
                not route.startswith("!")
                or len(route) > 500
                or any(character in route for character in ("\r", "\n", "\x00", "%", "#"))
            ):
                raise ValueError(f"flow.yaml {path} is not a safe single ORCA route line")
            unsafe_identifiers = {
                match.group(0).lower()
                for token in route[1:].split()
                if (match := _REMOTE_ORCA_IDENTIFIER_RE.match(token)) is not None
                and (token.lower() == "md" or _remote_orca_identifier_is_forbidden(match.group(0)))
            }
            if unsafe_identifiers:
                shown = ", ".join(sorted(unsafe_identifiers))
                raise ValueError(f"flow.yaml {path} uses remote-disabled ORCA features: {shown}")
            if "gcp(file)" in "".join(route.lower().split()):
                raise ValueError(
                    f"flow.yaml {path} uses a remote-disabled external GCP parameter source"
                )
            requested = max(
                (int(match.group(1)) for match in PAL_ROUTE_RE.finditer(route)),
                default=0,
            )
            if requested > max_cores:
                raise ValueError(
                    f"flow.yaml {path} requests {requested} cores; server limit is {max_cores}"
                )

        def validate_scan_coordinate(value: object, *, path: str) -> None:
            if not isinstance(value, str):
                raise ValueError(f"flow.yaml {path} must be a scan-coordinate string")
            match = _REMOTE_SCAN_COORDINATE_RE.fullmatch(value)
            if match is None:
                raise ValueError(f"flow.yaml {path} is not a safe ORCA scan coordinate")
            kind = match.group("kind").upper()
            atom_count = len(match.group("atoms").split())
            if atom_count != {"B": 2, "A": 3, "D": 4}[kind]:
                raise ValueError(f"flow.yaml {path} has the wrong atom count for {kind}")
            start = float(match.group("start"))
            end = float(match.group("end"))
            points = int(match.group("points"))
            if not math.isfinite(start) or not math.isfinite(end):
                raise ValueError(f"flow.yaml {path} scan range must be finite")
            if points < 2 or points > _REMOTE_SCAN_POINTS_LIMIT:
                raise ValueError(
                    f"flow.yaml {path} requests {points} points; "
                    f"server limit is {_REMOTE_SCAN_POINTS_LIMIT}"
                )

        stack: list[tuple[Any, str]] = [(manifest, "")]
        seen_containers: set[int] = set()
        while stack:
            value, path = stack.pop()
            if isinstance(value, dict):
                identity = id(value)
                if identity in seen_containers:
                    continue
                seen_containers.add(identity)
                for raw_key, item in value.items():
                    key = str(raw_key).strip()
                    item_path = f"{path}.{key}" if path else key
                    limit = limits.get(key)
                    if limit is not None and item is not None:
                        try:
                            requested = int(item)
                        except (OverflowError, TypeError, ValueError):
                            # The workflow parser owns ordinary type validation.
                            requested = 0
                        if requested > limit:
                            raise ValueError(
                                f"flow.yaml {item_path} requests {requested}; "
                                f"server limit is {limit}"
                            )
                    count_limit = _REMOTE_WORKFLOW_COUNT_LIMITS.get(key)
                    if count_limit is not None and item is not None:
                        try:
                            requested_count = int(item)
                        except (OverflowError, TypeError, ValueError):
                            requested_count = 0
                        if requested_count > count_limit:
                            raise ValueError(
                                f"flow.yaml {item_path} requests {requested_count}; "
                                f"server limit is {count_limit}"
                            )
                    if key in _REMOTE_ROUTE_LINE_KEYS and item is not None:
                        validate_route_line(item, path=item_path)
                    if key == "scan_coordinate" and item is not None:
                        validate_scan_coordinate(item, path=item_path)
                    stack.append((item, item_path))
            elif isinstance(value, list):
                identity = id(value)
                if identity in seen_containers:
                    continue
                seen_containers.add(identity)
                for index, item in enumerate(value):
                    stack.append((item, f"{path}[{index}]"))

    def _submit_orca_run_dir(
        self,
        job_dir: Path,
        runs_root: Path,
        args: _UploadRunDirSubmissionArgs,
    ) -> SubmissionReceipt:
        from orca_auto.orca.commands.run_inp import submit_reaction_dir_to_queue
        from orca_auto.orca.queue import adapter as queue_adapter

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
            max_cores, max_memory_gb = self._trusted_upload_resource_limits()
            if kind == "workflow":
                self._validate_workflow_resource_limits(
                    job_dir,
                    max_cores=max_cores,
                    max_memory_gb=max_memory_gb,
                )
            else:
                self._validate_orca_resource_limits(
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
        lines = [
            "orca_auto bot commands",
            "",
            f"{prefix}list — Show unified activities",
            f"{prefix}list clear — Remove completed, failed, and cancelled entries",
            f"{prefix}list running — Show running activities only",
            f"{prefix}list failed — Show failed activities only",
            f"{prefix}cancel TARGET — Ask to cancel a workflow or queued job",
        ]
        if self._upload_command_available(provider):
            lines.append(f"{prefix}run — Attach a .zip/.tar.gz run-dir to queue it")
        lines.append(f"{prefix}help — Show this help message")
        return BotReply("\n".join(lines))

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
                reply = BotReply(self._list_clear_text(), format="preformatted")
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
                reply = BotReply(f"Usage: {prefix}cancel TARGET")
                status = "cancel-usage"
            else:
                # Cancellation is never executed from the text command.  Even a
                # target too long for native callback data is represented by a
                # short opaque registry id and must be confirmed.
                try:
                    binding = self._resolve_cancel_binding(args)
                except (LookupError, ValueError) as exc:
                    reply = BotReply(str(exc))
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
                reply = BotReply("File uploads are available only through Discord.")
                status = "run-unavailable"
            elif self.upload_policy is None or not self.upload_policy.enabled:
                reply = BotReply("File uploads are disabled.")
                status = "run-disabled"
            elif self.upload_sessions is None:
                reply = BotReply("Upload staging is not configured.")
                status = "run-misconfigured"
            else:
                reply = BotReply(f"Attach a .zip or .tar.gz run-dir to {prefix}run to queue it.")
                status = "run-usage"
        elif name in {"help", "start"}:
            reply = self._help_reply(prefix, provider=command.address.provider)
            status = "help-sent"
        else:
            reply = BotReply(
                f"Unknown command: {prefix}{name}\nType {prefix}help for available commands."
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
            self._send_action_reply(incoming, BotReply("Cancellation dismissed."), messenger)
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
            self._send_action_reply(incoming, BotReply(text), messenger)
            return "run-unavailable"
        if registered.kind == "run_dismiss":
            text = "This legacy upload action is no longer available."
            self._acknowledge(incoming, text, messenger)
            self._send_action_reply(incoming, BotReply(text), messenger)
            return "run-unavailable"
        if registered.kind == "list_clear":
            self._acknowledge(incoming, "Finished entries cleared.", messenger)
            self._send_action_reply(
                incoming,
                BotReply(self._list_clear_text(), format="preformatted"),
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
                self._send_action_reply(incoming, BotReply("Upload discarded."), messenger)
                return "run-dismissed"

            self._acknowledge(incoming, "Submitting…", messenger)
            outcome = self._run_submit_session(consumed.session)
            self._send_action_reply(incoming, outcome.reply, messenger)
            return outcome.status

        text = self._invalid_action_text(status)
        self._acknowledge(incoming, text, messenger)
        self._send_action_reply(incoming, BotReply(text), messenger)
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
            self._send_action_reply(action, BotReply(text), messenger)
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
