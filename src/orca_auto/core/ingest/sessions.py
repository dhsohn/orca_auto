"""Durable state and staging for untrusted messenger uploads.

The archive extractor deliberately knows nothing about messengers or queue
commit semantics.  This module owns the boundary around it: it reserves bounded
staging space, binds a session and its one-shot actions to the originating
messenger identity, and persists every state transition before callers perform
the next externally visible side effect.

All mutable state is stored in one atomically replaced JSON document protected
by an advisory file lock.  Archive bytes live at the fixed, store-owned path
``<root>/<upload_id>/archive``; cleanup never follows or removes a published
run-directory path recorded by a caller.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from orca_auto.core.utils.lock import file_lock
from orca_auto.core.utils.persistence import atomic_write_json

UPLOAD_SESSIONS_FILE_NAME = "upload_sessions.json"
UPLOAD_SESSIONS_LOCK_NAME = ".upload_sessions.lock"
UPLOAD_ARCHIVE_FILE_NAME = "archive"
_OWNERSHIP_FILE_NAME = ".upload-session"
_SCHEMA_VERSION = 1
_UPLOAD_ID_RE = re.compile(r"\Aupl_[A-Za-z0-9_-]{16,80}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_ACTION_ID_RE = re.compile(r"\Aact_[A-Za-z0-9_-]{20,95}\Z")
_MAX_STATE_FILE_BYTES = 16 * 1024 * 1024
_MAX_VERIFICATION_BYTES = 64 * 1024


class UploadState(str, Enum):
    """Durable upload lifecycle states."""

    RECEIVING = "receiving"
    VERIFIED = "verified"
    AWAITING_CONFIRM = "awaiting_confirm"
    PROCESSING = "processing"
    PUBLISHED = "published"
    COMMITTED = "committed"
    DISCARDED = "discarded"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


class UploadActionKind(str, Enum):
    CONFIRM = "confirm"
    DISMISS = "dismiss"


class UploadSessionError(RuntimeError):
    """Base error for durable upload session operations."""


class UploadSessionNotFoundError(UploadSessionError):
    """Raised when an upload ID does not exist."""


class UploadSessionStoreCorruptError(UploadSessionError):
    """Raised when persisted upload state cannot be trusted."""


class UploadStateConflictError(UploadSessionError):
    """Raised when a compare-and-set transition observes another state."""


class UploadBindingMismatchError(UploadSessionError):
    """Raised when an action or idempotent retry comes from another identity."""


class UploadQuotaExceededError(UploadSessionError):
    """Raised before a reservation would exceed a configured staging quota."""


class UploadActionNotFoundError(UploadSessionError):
    """Raised for an unknown opaque action ID."""


class UploadActionConsumedError(UploadSessionError):
    """Raised when a one-shot action has already lost its CAS race."""


class UploadActionExpiredError(UploadSessionError):
    """Raised when an action is consumed after its wall-clock deadline."""


class UploadArchiveError(UploadSessionError):
    """Raised when staged archive bytes are absent, unstable, or not regular."""


@dataclass(frozen=True)
class UploadBinding:
    """Messenger identity to which a session and its actions are bound."""

    provider: str
    channel_id: str
    actor_id: str
    thread_id: str | None = None

    def __post_init__(self) -> None:
        provider = str(self.provider).strip().lower()
        channel_id = str(self.channel_id).strip()
        actor_id = str(self.actor_id).strip()
        thread_id = None if self.thread_id is None else str(self.thread_id).strip()
        if not provider or not channel_id or not actor_id:
            raise ValueError("provider, channel_id, and actor_id must be non-empty")
        if thread_id == "":
            thread_id = None
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "channel_id", channel_id)
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "thread_id", thread_id)

    @property
    def actor_key(self) -> tuple[str, str]:
        """Provider-scoped identity used by per-actor quotas."""

        return self.provider, self.actor_id


@dataclass(frozen=True)
class UploadAction:
    action_id: str
    kind: UploadActionKind
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass(frozen=True)
class UploadCommitReceipt:
    """Durable proof that at least one downstream identity was committed."""

    committed_at: datetime
    queue_id: str | None = None
    workflow_id: str | None = None


@dataclass(frozen=True)
class UploadSession:
    upload_id: str
    state: UploadState
    binding: UploadBinding
    message_id: str
    attachment_ids: tuple[str, ...]
    idempotency_key: str
    archive_path: Path
    expected_bytes: int
    actual_bytes: int | None
    sha256: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    verification: dict[str, object]
    actions: tuple[UploadAction, ...]
    consumed_action_id: str | None = None
    published_path: Path | None = None
    receipt: UploadCommitReceipt | None = None
    reason: str | None = None


@dataclass(frozen=True)
class UploadReservation:
    session: UploadSession
    created: bool


@dataclass(frozen=True)
class UploadActionSet:
    session: UploadSession
    confirm_action_id: str
    dismiss_action_id: str
    expires_at: datetime


@dataclass(frozen=True)
class UploadActionConsumption:
    session: UploadSession
    action: UploadAction


@dataclass(frozen=True)
class UploadSweepResult:
    expired_upload_ids: tuple[str, ...] = ()
    ambiguous_upload_ids: tuple[str, ...] = ()
    cleaned_upload_ids: tuple[str, ...] = ()
    orphaned_upload_ids: tuple[str, ...] = ()
    cleanup_failed_upload_ids: tuple[str, ...] = ()
    pruned_upload_ids: tuple[str, ...] = ()


_ALLOWED_TRANSITIONS: dict[UploadState, frozenset[UploadState]] = {
    UploadState.RECEIVING: frozenset(
        {UploadState.VERIFIED, UploadState.DISCARDED, UploadState.FAILED}
    ),
    UploadState.VERIFIED: frozenset(
        {UploadState.AWAITING_CONFIRM, UploadState.DISCARDED, UploadState.FAILED}
    ),
    UploadState.AWAITING_CONFIRM: frozenset(
        {UploadState.PROCESSING, UploadState.DISCARDED, UploadState.FAILED}
    ),
    UploadState.PROCESSING: frozenset(
        {UploadState.PUBLISHED, UploadState.FAILED, UploadState.AMBIGUOUS}
    ),
    UploadState.PUBLISHED: frozenset(
        {UploadState.COMMITTED, UploadState.FAILED, UploadState.AMBIGUOUS}
    ),
    UploadState.AMBIGUOUS: frozenset({UploadState.COMMITTED, UploadState.FAILED}),
    UploadState.COMMITTED: frozenset(),
    UploadState.DISCARDED: frozenset(),
    UploadState.FAILED: frozenset(),
}
_QUOTA_STATES = frozenset(
    {
        UploadState.RECEIVING,
        UploadState.VERIFIED,
        UploadState.AWAITING_CONFIRM,
        UploadState.PROCESSING,
        UploadState.PUBLISHED,
        UploadState.AMBIGUOUS,
    }
)
_EXPIRING_PRE_PROCESS_STATES = frozenset(
    {UploadState.RECEIVING, UploadState.VERIFIED, UploadState.AWAITING_CONFIRM}
)
_CLEANABLE_STATES = frozenset({UploadState.DISCARDED, UploadState.FAILED, UploadState.COMMITTED})


def upload_idempotency_key(
    binding: UploadBinding,
    *,
    message_id: str,
    attachment_ids: Sequence[str],
) -> str:
    """Return a stable key for one provider message and its attachment set."""

    normalized_message_id = str(message_id).strip()
    normalized_attachment_ids = tuple(
        sorted({str(attachment_id).strip() for attachment_id in attachment_ids})
    )
    if (
        not normalized_message_id
        or not normalized_attachment_ids
        or any(not attachment_id for attachment_id in normalized_attachment_ids)
    ):
        raise ValueError("message_id and every attachment ID must be non-empty")
    canonical = json.dumps(
        {
            "provider": binding.provider,
            "channel_id": binding.channel_id,
            "thread_id": binding.thread_id,
            "message_id": normalized_message_id,
            "attachment_ids": normalized_attachment_ids,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _positive_number(value: float, *, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be positive")
    return float(value)


def _positive_int(value: int, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("wall-clock timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _parse_time(raw: object, *, field_name: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"invalid timestamp field {field_name!r}")
    return _utc(datetime.fromisoformat(raw.strip()))


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_mapping(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    try:
        # A JSON round trip both copies caller-owned mutable values and rejects
        # objects that atomic_write_json could not durably encode later.
        encoded = json.dumps(dict(value), ensure_ascii=True, allow_nan=False)
        normalized = json.loads(encoded)
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError("verification metadata must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > _MAX_VERIFICATION_BYTES:
        raise ValueError("verification metadata is too large")
    if not isinstance(normalized, dict):  # pragma: no cover - dict(value) guarantees this
        raise ValueError("verification metadata must be a mapping")
    return normalized


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


class UploadSessionStore:
    """File-backed upload sessions with quotas and CAS action consumption."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_staged_count: int = 32,
        max_staged_bytes: int = 512 * 1024 * 1024,
        max_staged_per_actor: int = 4,
        max_staged_bytes_per_actor: int | None = None,
        session_ttl_seconds: float = 15 * 60,
        action_ttl_seconds: float = 5 * 60,
        processing_ttl_seconds: float = 60 * 60,
        committed_retention_seconds: float = 24 * 60 * 60,
        lock_timeout_seconds: float = 10.0,
        now_fn: Callable[[], datetime] | None = None,
        sweep_on_startup: bool = True,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.max_staged_count = _positive_int(max_staged_count, name="max_staged_count")
        self.max_staged_bytes = _positive_int(max_staged_bytes, name="max_staged_bytes")
        self.max_staged_per_actor = _positive_int(max_staged_per_actor, name="max_staged_per_actor")
        self.max_staged_bytes_per_actor = (
            None
            if max_staged_bytes_per_actor is None
            else _positive_int(max_staged_bytes_per_actor, name="max_staged_bytes_per_actor")
        )
        self.session_ttl_seconds = _positive_number(session_ttl_seconds, name="session_ttl_seconds")
        self.action_ttl_seconds = _positive_number(action_ttl_seconds, name="action_ttl_seconds")
        self.processing_ttl_seconds = _positive_number(
            processing_ttl_seconds, name="processing_ttl_seconds"
        )
        self.committed_retention_seconds = _positive_number(
            committed_retention_seconds, name="committed_retention_seconds"
        )
        self.lock_timeout_seconds = _positive_number(
            lock_timeout_seconds, name="lock_timeout_seconds"
        )
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if sweep_on_startup:
            self.sweep()

    @property
    def state_path(self) -> Path:
        return self.root / UPLOAD_SESSIONS_FILE_NAME

    @property
    def lock_path(self) -> Path:
        return self.root / UPLOAD_SESSIONS_LOCK_NAME

    def _now(self) -> datetime:
        return _utc(self._now_fn())

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize durable session mutations through the shared disk lock."""

        with ExitStack() as stack:
            try:
                stack.enter_context(
                    file_lock(
                        self.lock_path,
                        timeout_seconds=self.lock_timeout_seconds,
                    )
                )
            except TimeoutError:
                raise
            except (OSError, ValueError) as exc:
                raise UploadSessionStoreCorruptError(
                    f"upload session lock cannot be opened safely: {self.lock_path}"
                ) from exc
            yield

    def _session_dir(self, upload_id: str) -> Path:
        if not _UPLOAD_ID_RE.fullmatch(upload_id):
            raise ValueError(f"invalid upload ID: {upload_id!r}")
        path = self.root / upload_id
        if path.parent != self.root:
            raise ValueError(f"upload path escapes the store: {upload_id!r}")
        return path

    def archive_path(self, upload_id: str) -> Path:
        """Return the fixed contained archive path for ``upload_id``."""

        session_dir = self._session_dir(upload_id)
        archive = session_dir / UPLOAD_ARCHIVE_FILE_NAME
        if archive.parent != session_dir:
            raise ValueError(f"archive path escapes the upload directory: {upload_id!r}")
        return archive

    def reserve(
        self,
        binding: UploadBinding,
        *,
        message_id: str,
        attachment_ids: Sequence[str],
        expected_bytes: int,
        ttl_seconds: float | None = None,
    ) -> UploadReservation:
        """Reserve staging capacity, or return the prior idempotent session."""

        expected_bytes = _nonnegative_int(expected_bytes, name="expected_bytes")
        lifetime = (
            self.session_ttl_seconds
            if ttl_seconds is None
            else _positive_number(ttl_seconds, name="ttl_seconds")
        )
        message = str(message_id).strip()
        attachments = tuple(sorted({str(value).strip() for value in attachment_ids}))
        key = upload_idempotency_key(
            binding,
            message_id=message,
            attachment_ids=attachments,
        )
        with self._locked():
            now = self._now()
            sessions = self._load_current_locked(now=now)
            for existing in sessions:
                if existing.idempotency_key != key:
                    continue
                if existing.binding != binding:
                    raise UploadBindingMismatchError(
                        "idempotent upload retry does not match the original actor binding"
                    )
                if existing.expected_bytes != expected_bytes:
                    raise UploadStateConflictError(
                        "idempotent upload retry changed the declared attachment bytes"
                    )
                return UploadReservation(session=existing, created=False)

            self._check_quota(
                sessions,
                binding=binding,
                additional_count=1,
                additional_bytes=expected_bytes,
            )
            upload_id, archive_path = self._create_owned_session_dir(sessions)
            session = UploadSession(
                upload_id=upload_id,
                state=UploadState.RECEIVING,
                binding=binding,
                message_id=message,
                attachment_ids=attachments,
                idempotency_key=key,
                archive_path=archive_path,
                expected_bytes=expected_bytes,
                actual_bytes=None,
                sha256=None,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(seconds=lifetime),
                verification={},
                actions=(),
            )
            sessions.append(session)
            try:
                self._save(sessions)
            except BaseException:
                # Remove only the directory allocated by this call, and only if
                # the new record is definitely not visible after atomic replace.
                try:
                    visible: list[UploadSession] | None = self._load()
                except BaseException:  # noqa: BLE001 - preserve original failure
                    # A failed read makes the replace outcome ambiguous.  Keep
                    # the bytes for startup reconciliation rather than deleting
                    # a directory that a durable record may already reference.
                    visible = None
                if visible is not None and not any(item.upload_id == upload_id for item in visible):
                    self._remove_owned_session_dir(upload_id)
                raise
            return UploadReservation(session=session, created=True)

    def finalize_upload(self, upload_id: str) -> UploadSession:
        """Record the stable archive byte count and SHA-256 digest."""

        with self._locked():
            now = self._now()
            sessions = self._load_current_locked(now=now)
            index, session = self._find(sessions, upload_id)
            now = self._mutation_time(session, now)
            self._expect_state(session, {UploadState.RECEIVING})
            other_sessions = sessions[:index] + sessions[index + 1 :]
            try:
                # The lstat is a cheap rejection for obviously oversized files.
                # The stable hash result is checked again and is the only size
                # persisted, so a swap between these operations cannot shrink
                # the quota charge.
                observed_bytes = self._regular_archive_size(session.archive_path)
                self._check_quota(
                    other_sessions,
                    binding=session.binding,
                    additional_count=1,
                    additional_bytes=observed_bytes,
                )
                actual_bytes, digest = self._hash_stable_archive(session.archive_path)
                self._check_quota(
                    other_sessions,
                    binding=session.binding,
                    additional_count=1,
                    additional_bytes=actual_bytes,
                )
            except UploadQuotaExceededError as exc:
                failed = self._transition(
                    session,
                    UploadState.FAILED,
                    now=now,
                    reason=str(exc),
                )
                sessions[index] = failed
                self._save(sessions)
                self._remove_owned_session_dir(upload_id)
                raise
            updated = replace(
                session,
                actual_bytes=actual_bytes,
                sha256=digest,
                updated_at=now,
            )
            sessions[index] = updated
            self._save(sessions)
            return updated

    def mark_verified(
        self,
        upload_id: str,
        *,
        verification: Mapping[str, object] | None = None,
    ) -> UploadSession:
        """Persist successful archive inspection before prompting the user."""

        metadata = _json_mapping(verification)
        with self._locked():
            now = self._now()
            sessions = self._load_current_locked(now=now)
            index, session = self._find(sessions, upload_id)
            if session.state is UploadState.VERIFIED and session.verification == metadata:
                return session
            self._expect_state(session, {UploadState.RECEIVING})
            if session.actual_bytes is None or session.sha256 is None:
                raise UploadStateConflictError(
                    "archive bytes must be finalized before verification"
                )
            updated = replace(
                self._transition(session, UploadState.VERIFIED, now=now),
                verification=metadata,
            )
            sessions[index] = updated
            self._save(sessions)
            return updated

    def verify_archive(self, upload_id: str) -> UploadSession:
        """Re-hash staged bytes and prove they still match finalization.

        Inspection and extraction are separated by a human confirmation.  A
        stable digest check at both boundaries prevents a replaced staging file
        from making the confirmation describe different bytes than the ones
        ultimately published.
        """

        with self._locked():
            now = self._now()
            sessions = self._load_current_locked(now=now)
            _, session = self._find(sessions, upload_id)
            self._expect_state(
                session,
                {
                    UploadState.RECEIVING,
                    UploadState.VERIFIED,
                    UploadState.AWAITING_CONFIRM,
                    UploadState.PROCESSING,
                },
            )
            if session.actual_bytes is None or session.sha256 is None:
                raise UploadStateConflictError("archive bytes have not been finalized")
            actual_bytes, digest = self._hash_stable_archive(session.archive_path)
            if actual_bytes != session.actual_bytes or not secrets.compare_digest(
                digest,
                session.sha256,
            ):
                raise UploadArchiveError("staged archive no longer matches its finalized bytes")
            return session

    def await_confirmation(
        self,
        upload_id: str,
        *,
        ttl_seconds: float | None = None,
    ) -> UploadActionSet:
        """Create durable opaque confirm/dismiss actions and enter the wait state."""

        lifetime = (
            self.action_ttl_seconds
            if ttl_seconds is None
            else _positive_number(ttl_seconds, name="ttl_seconds")
        )
        with self._locked():
            now = self._now()
            sessions = self._load_current_locked(now=now)
            index, session = self._find(sessions, upload_id)
            if session.state is UploadState.AWAITING_CONFIRM:
                by_kind = {action.kind: action for action in session.actions}
                confirm = by_kind.get(UploadActionKind.CONFIRM)
                dismiss = by_kind.get(UploadActionKind.DISMISS)
                if confirm is None or dismiss is None:
                    raise UploadSessionStoreCorruptError(
                        "awaiting upload is missing durable confirmation actions"
                    )
                return UploadActionSet(
                    session=session,
                    confirm_action_id=confirm.action_id,
                    dismiss_action_id=dismiss.action_id,
                    expires_at=min(confirm.expires_at, dismiss.expires_at),
                )
            self._expect_state(session, {UploadState.VERIFIED})
            now = self._mutation_time(session, now)
            expires_at = now + timedelta(seconds=lifetime)
            used_action_ids = {action.action_id for item in sessions for action in item.actions}
            confirm_id = self._new_action_id(used_action_ids)
            used_action_ids.add(confirm_id)
            dismiss_id = self._new_action_id(used_action_ids)
            actions = (
                UploadAction(
                    action_id=confirm_id,
                    kind=UploadActionKind.CONFIRM,
                    created_at=now,
                    expires_at=expires_at,
                ),
                UploadAction(
                    action_id=dismiss_id,
                    kind=UploadActionKind.DISMISS,
                    created_at=now,
                    expires_at=expires_at,
                ),
            )
            updated = replace(
                self._transition(session, UploadState.AWAITING_CONFIRM, now=now),
                actions=actions,
                expires_at=expires_at,
            )
            sessions[index] = updated
            self._save(sessions)
            return UploadActionSet(
                session=updated,
                confirm_action_id=confirm_id,
                dismiss_action_id=dismiss_id,
                expires_at=expires_at,
            )

    def consume_action(
        self,
        action_id: str,
        *,
        binding: UploadBinding,
    ) -> UploadActionConsumption:
        """CAS-consume one action after exact provider/channel/thread/actor binding."""

        opaque_id = str(action_id).strip()
        if not _ACTION_ID_RE.fullmatch(opaque_id):
            raise UploadActionNotFoundError("unknown upload action")
        with self._locked():
            sessions = self._load()
            located = self._find_action(sessions, opaque_id)
            if located is None:
                raise UploadActionNotFoundError("unknown upload action")
            session_index, action_index, session, action = located
            if session.binding != binding:
                raise UploadBindingMismatchError("upload action identity binding does not match")
            if action.consumed_at is not None or session.consumed_action_id is not None:
                raise UploadActionConsumedError("upload action has already been consumed")
            self._expect_state(session, {UploadState.AWAITING_CONFIRM})
            now = self._mutation_time(session, self._now())
            if now >= action.expires_at or now >= session.expires_at:
                expired_actions = tuple(
                    replace(item, consumed_at=now) if item.consumed_at is None else item
                    for item in session.actions
                )
                discarded = replace(
                    self._transition(
                        session,
                        UploadState.DISCARDED,
                        now=now,
                        reason="confirmation expired",
                    ),
                    actions=expired_actions,
                )
                sessions[session_index] = discarded
                self._save(sessions)
                self._remove_owned_session_dir(session.upload_id)
                raise UploadActionExpiredError("upload action expired")

            consumed_action = replace(action, consumed_at=now)
            actions = list(session.actions)
            actions[action_index] = consumed_action
            target = (
                UploadState.PROCESSING
                if action.kind is UploadActionKind.CONFIRM
                else UploadState.DISCARDED
            )
            updated = replace(
                self._transition(
                    session,
                    target,
                    now=now,
                    reason="dismissed by actor" if target is UploadState.DISCARDED else None,
                ),
                actions=tuple(actions),
                consumed_action_id=action.action_id,
                expires_at=(
                    now + timedelta(seconds=self.processing_ttl_seconds)
                    if target is UploadState.PROCESSING
                    else now
                ),
            )
            sessions[session_index] = updated
            self._save(sessions)
            if target is UploadState.DISCARDED:
                self._remove_owned_session_dir(session.upload_id)
            return UploadActionConsumption(session=updated, action=consumed_action)

    def mark_published(self, upload_id: str, *, published_path: str | Path) -> UploadSession:
        """Record the externally owned run directory without ever cleaning it."""

        path = Path(published_path).expanduser().resolve()
        with self._locked():
            now = self._now()
            sessions = self._load_current_locked(now=now)
            index, session = self._find(sessions, upload_id)
            now = self._mutation_time(session, now)
            if (
                session.state
                in {UploadState.PUBLISHED, UploadState.COMMITTED, UploadState.AMBIGUOUS}
                and session.published_path == path
            ):
                return session
            if not path.is_dir():
                raise ValueError(f"published run directory does not exist: {path}")
            if session.state is UploadState.AMBIGUOUS and session.published_path is None:
                # Crash recovery may discover an ownership marker after a
                # PROCESSING timeout was conservatively made AMBIGUOUS. Record
                # the path without pretending the downstream commit is known.
                updated = replace(session, published_path=path, updated_at=now)
                sessions[index] = updated
                self._save(sessions)
                return updated
            self._expect_state(session, {UploadState.PROCESSING})
            updated = replace(
                self._transition(session, UploadState.PUBLISHED, now=now),
                published_path=path,
            )
            sessions[index] = updated
            self._save(sessions)
            return updated

    def mark_committed(
        self,
        upload_id: str,
        *,
        queue_id: str | None = None,
        workflow_id: str | None = None,
    ) -> UploadSession:
        """Persist a downstream commit receipt, then release only staging bytes."""

        normalized_queue_id = _optional_text(queue_id)
        normalized_workflow_id = _optional_text(workflow_id)
        if normalized_queue_id is None and normalized_workflow_id is None:
            raise ValueError("a queue_id or workflow_id is required for a commit receipt")
        with self._locked():
            now = self._now()
            sessions = self._load_current_locked(now=now)
            index, session = self._find(sessions, upload_id)
            now = self._mutation_time(session, now)
            if session.state is UploadState.COMMITTED:
                if session.receipt is not None and (
                    session.receipt.queue_id,
                    session.receipt.workflow_id,
                ) == (normalized_queue_id, normalized_workflow_id):
                    return session
                raise UploadStateConflictError(
                    "upload already has a different downstream commit receipt"
                )
            self._expect_state(session, {UploadState.PUBLISHED, UploadState.AMBIGUOUS})
            if session.published_path is None:
                raise UploadStateConflictError(
                    "upload cannot be committed before a published path is durable"
                )
            receipt = UploadCommitReceipt(
                committed_at=now,
                queue_id=normalized_queue_id,
                workflow_id=normalized_workflow_id,
            )
            updated = replace(
                self._transition(session, UploadState.COMMITTED, now=now),
                receipt=receipt,
            )
            sessions[index] = updated
            self._save(sessions)
            self._remove_owned_session_dir(upload_id, preserve_path=updated.published_path)
            return updated

    def mark_failed(self, upload_id: str, *, reason: str) -> UploadSession:
        """Record a known pre-commit failure and clean only owned staging."""

        message = str(reason).strip() or "upload failed"
        with self._locked():
            now = self._now()
            sessions = self._load_current_locked(now=now)
            index, session = self._find(sessions, upload_id)
            self._expect_state(
                session,
                {
                    UploadState.RECEIVING,
                    UploadState.VERIFIED,
                    UploadState.AWAITING_CONFIRM,
                    UploadState.PROCESSING,
                    UploadState.PUBLISHED,
                    UploadState.AMBIGUOUS,
                },
            )
            updated = self._transition(session, UploadState.FAILED, now=now, reason=message)
            sessions[index] = updated
            self._save(sessions)
            self._remove_owned_session_dir(upload_id, preserve_path=updated.published_path)
            return updated

    def mark_discarded(self, upload_id: str, *, reason: str = "discarded") -> UploadSession:
        """Cancel a session that has not started processing."""

        message = str(reason).strip() or "discarded"
        with self._locked():
            now = self._now()
            sessions = self._load_current_locked(now=now)
            index, session = self._find(sessions, upload_id)
            self._expect_state(
                session,
                {
                    UploadState.RECEIVING,
                    UploadState.VERIFIED,
                    UploadState.AWAITING_CONFIRM,
                },
            )
            updated = self._transition(session, UploadState.DISCARDED, now=now, reason=message)
            sessions[index] = updated
            self._save(sessions)
            self._remove_owned_session_dir(upload_id)
            return updated

    def mark_ambiguous(self, upload_id: str, *, reason: str) -> UploadSession:
        """Preserve staging and publication paths when commit outcome is unknown."""

        message = str(reason).strip() or "downstream commit outcome is ambiguous"
        with self._locked():
            now = self._now()
            sessions = self._load_current_locked(now=now)
            index, session = self._find(sessions, upload_id)
            if session.state in {UploadState.AMBIGUOUS, UploadState.COMMITTED}:
                return session
            self._expect_state(session, {UploadState.PROCESSING, UploadState.PUBLISHED})
            updated = self._transition(session, UploadState.AMBIGUOUS, now=now, reason=message)
            sessions[index] = updated
            self._save(sessions)
            return updated

    def get(self, upload_id: str) -> UploadSession:
        """Load one session after applying wall-clock expiration."""

        with self._locked():
            sessions = self._load_current_locked(now=self._now())
            _, session = self._find(sessions, upload_id)
            return session

    def list_sessions(self) -> tuple[UploadSession, ...]:
        """Return a stable snapshot after applying wall-clock expiration."""

        with self._locked():
            sessions = self._load_current_locked(now=self._now())
            return tuple(sessions)

    def find_by_idempotency_key(self, key: str) -> UploadSession | None:
        """Find a durable session by a previously calculated source key."""

        normalized = str(key).strip().lower()
        with self._locked():
            sessions = self._load_current_locked(now=self._now())
            for session in sessions:
                if session.idempotency_key == normalized:
                    return session
        return None

    def sweep(self) -> UploadSweepResult:
        """Expire stale work and clean only marker-proven store-owned paths.

        ``PROCESSING`` becomes ``AMBIGUOUS`` instead of being deleted because a
        downstream commit may have succeeded before the process crashed.  The
        staging and published paths of ``PUBLISHED`` and ``AMBIGUOUS`` records
        are preserved.  ``COMMITTED`` records retain their published path and
        receipt through the configured idempotency retention window while their
        now-redundant staging directory may be removed.
        """

        with self._locked():
            sessions = self._load()
            now = self._now()
            sessions, changed, expired, ambiguous = self._expire_details_locked(sessions, now=now)
            if changed:
                self._save(sessions)

            cleaned: list[str] = []
            failed: list[str] = []
            for session in sessions:
                if session.state not in _CLEANABLE_STATES:
                    continue
                if self._remove_owned_session_dir(
                    session.upload_id,
                    preserve_path=session.published_path,
                ):
                    cleaned.append(session.upload_id)
                elif self._session_dir_present(session.upload_id):
                    failed.append(session.upload_id)

            pruned: list[str] = []
            retained: list[UploadSession] = []
            retention = timedelta(seconds=self.committed_retention_seconds)
            for session in sessions:
                terminal_at = (
                    session.receipt.committed_at
                    if session.receipt is not None
                    else session.updated_at
                )
                retention_elapsed = now >= terminal_at and now - terminal_at >= retention
                unpublished_failure_evidence = (
                    session.state is not UploadState.COMMITTED
                    and self._published_path_present(session.published_path)
                )
                can_prune = (
                    session.state in _CLEANABLE_STATES
                    and retention_elapsed
                    and not self._session_dir_present(session.upload_id)
                    and not unpublished_failure_evidence
                )
                if can_prune:
                    pruned.append(session.upload_id)
                else:
                    retained.append(session)
            if pruned:
                sessions = retained
                self._save(sessions)

            orphaned: list[str] = []
            by_id = {session.upload_id: session for session in sessions}
            for child in self.root.iterdir():
                if child.name in by_id or not _UPLOAD_ID_RE.fullmatch(child.name):
                    continue
                if not self._owned_session_dir(child.name):
                    continue
                if self._remove_owned_session_dir(child.name):
                    orphaned.append(child.name)
                elif child.exists():
                    failed.append(child.name)

            return UploadSweepResult(
                expired_upload_ids=tuple(expired),
                ambiguous_upload_ids=tuple(ambiguous),
                cleaned_upload_ids=tuple(cleaned),
                orphaned_upload_ids=tuple(orphaned),
                cleanup_failed_upload_ids=tuple(failed),
                pruned_upload_ids=tuple(pruned),
            )

    def _transition(
        self,
        session: UploadSession,
        target: UploadState,
        *,
        now: datetime,
        reason: str | None = None,
    ) -> UploadSession:
        if target not in _ALLOWED_TRANSITIONS[session.state]:
            raise UploadStateConflictError(
                f"upload {session.upload_id} cannot transition "
                f"{session.state.value} -> {target.value}"
            )
        return replace(
            session,
            state=target,
            updated_at=UploadSessionStore._mutation_time(session, now),
            reason=reason,
        )

    @staticmethod
    def _mutation_time(session: UploadSession, now: datetime) -> datetime:
        """Keep persisted lifecycle time monotonic across wall-clock regressions."""

        return max(_utc(now), session.created_at, session.updated_at)

    @staticmethod
    def _expect_state(session: UploadSession, expected: Iterable[UploadState]) -> None:
        allowed = frozenset(expected)
        if session.state not in allowed:
            shown = ", ".join(sorted(state.value for state in allowed))
            raise UploadStateConflictError(
                f"upload {session.upload_id} is {session.state.value}; expected {shown}"
            )

    def _check_quota(
        self,
        sessions: Sequence[UploadSession],
        *,
        binding: UploadBinding,
        additional_count: int,
        additional_bytes: int,
    ) -> None:
        # A terminal record whose directory could not be removed still occupies
        # staging capacity.  Charging it prevents repeated cleanup failures (or
        # a tampered ownership marker) from becoming a disk-quota bypass.
        staged = [
            session
            for session in sessions
            if session.state in _QUOTA_STATES or self._session_dir_present(session.upload_id)
        ]
        actor_staged = [
            session for session in staged if session.binding.actor_key == binding.actor_key
        ]
        if len(staged) + additional_count > self.max_staged_count:
            raise UploadQuotaExceededError("maximum staged upload count reached")
        if len(actor_staged) + additional_count > self.max_staged_per_actor:
            raise UploadQuotaExceededError("maximum staged uploads for actor reached")
        total_bytes = sum(self._charged_bytes(session) for session in staged)
        if total_bytes + additional_bytes > self.max_staged_bytes:
            raise UploadQuotaExceededError("maximum staged upload bytes reached")
        if self.max_staged_bytes_per_actor is not None:
            actor_bytes = sum(self._charged_bytes(session) for session in actor_staged)
            if actor_bytes + additional_bytes > self.max_staged_bytes_per_actor:
                raise UploadQuotaExceededError("maximum staged upload bytes for actor reached")

    def _charged_bytes(self, session: UploadSession) -> int:
        charged = (
            session.actual_bytes if session.actual_bytes is not None else session.expected_bytes
        )
        # During RECEIVING the downloader writes after reservation.  Observe the
        # contained regular file so another reservation cannot exploit a stale
        # or dishonest declared size before finalize_upload runs.
        try:
            observed = session.archive_path.lstat()
        except OSError:
            return charged
        if stat.S_ISREG(observed.st_mode) and not stat.S_ISLNK(observed.st_mode):
            charged = max(charged, observed.st_size)
        return charged

    def _session_dir_present(self, upload_id: str) -> bool:
        try:
            self._session_dir(upload_id).lstat()
        except FileNotFoundError:
            return False
        except OSError:
            # An unreadable path is conservatively still charged.
            return True
        return True

    @staticmethod
    def _published_path_present(path: Path | None) -> bool:
        if path is None:
            return False
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            # Do not discard reconciliation evidence merely because the path is
            # temporarily unreadable.
            return True
        return True

    def _create_owned_session_dir(self, sessions: Sequence[UploadSession]) -> tuple[str, Path]:
        used = {session.upload_id for session in sessions}
        for _ in range(100):
            upload_id = f"upl_{secrets.token_urlsafe(18)}"
            if upload_id in used or not _UPLOAD_ID_RE.fullmatch(upload_id):
                continue
            session_dir = self._session_dir(upload_id)
            try:
                session_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
            except FileExistsError:
                continue
            directory_fd: int | None = None
            try:
                directory_flags = os.O_RDONLY
                directory_flags |= os.O_DIRECTORY
                directory_flags |= os.O_CLOEXEC
                directory_flags |= os.O_NOFOLLOW
                directory_fd = os.open(session_dir, directory_flags)
                opened = os.fstat(directory_fd)
                named = session_dir.lstat()
                if not stat.S_ISDIR(opened.st_mode) or (
                    opened.st_dev,
                    opened.st_ino,
                ) != (named.st_dev, named.st_ino):
                    raise UploadSessionError("upload staging directory changed during creation")

                marker_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                marker_flags |= os.O_CLOEXEC
                marker_flags |= os.O_NOFOLLOW
                fd = os.open(
                    _OWNERSHIP_FILE_NAME,
                    marker_flags,
                    0o600,
                    dir_fd=directory_fd,
                )
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(f"{upload_id}\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                after = session_dir.lstat()
                if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
                    raise UploadSessionError("upload staging directory changed during creation")
            except BaseException:
                if directory_fd is not None:
                    os.close(directory_fd)
                    directory_fd = None
                # This directory was empty before marker creation.  Avoid a
                # recursive cleanup here: a path swap must never make an error
                # path delete an attacker-selected directory.
                if not self._remove_owned_session_dir(upload_id):
                    with suppress(OSError):
                        session_dir.rmdir()
                raise
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
            return upload_id, self.archive_path(upload_id)
        raise UploadSessionError("could not allocate a unique upload ID")

    def _owned_session_dir(self, upload_id: str) -> bool:
        opened = self._open_owned_session_dir(upload_id)
        if opened is None:
            return False
        fd, _identity = opened
        os.close(fd)
        return True

    def _open_owned_session_dir(
        self,
        upload_id: str,
    ) -> tuple[int, tuple[int, int]] | None:
        """Open and authenticate a store directory without following links."""

        session_dir = self._session_dir(upload_id)
        directory_flags = os.O_RDONLY
        directory_flags |= os.O_DIRECTORY
        directory_flags |= os.O_CLOEXEC
        directory_flags |= os.O_NOFOLLOW
        fd: int | None = None
        try:
            fd = os.open(session_dir, directory_flags)
            opened = os.fstat(fd)
            if not stat.S_ISDIR(opened.st_mode):
                return None
            marker_flags = os.O_RDONLY
            marker_flags |= os.O_CLOEXEC
            marker_flags |= os.O_NOFOLLOW
            marker_fd = os.open(_OWNERSHIP_FILE_NAME, marker_flags, dir_fd=fd)
            with os.fdopen(marker_fd, "rb") as marker:
                marker_before = os.fstat(marker.fileno())
                payload = marker.read(128)
                marker_after = os.fstat(marker.fileno())
            if (
                not stat.S_ISREG(marker_before.st_mode)
                or (marker_before.st_dev, marker_before.st_ino, marker_before.st_size)
                != (marker_after.st_dev, marker_after.st_ino, marker_after.st_size)
                or payload != f"{upload_id}\n".encode("ascii")
            ):
                return None
            named = session_dir.lstat()
            identity = (opened.st_dev, opened.st_ino)
            if identity != (named.st_dev, named.st_ino):
                return None
            result = (fd, identity)
            fd = None
            return result
        except OSError:
            return None
        finally:
            if fd is not None:
                os.close(fd)

    def _remove_owned_session_dir(
        self,
        upload_id: str,
        *,
        preserve_path: Path | None = None,
    ) -> bool:
        session_dir = self._session_dir(upload_id)
        opened = self._open_owned_session_dir(upload_id)
        if opened is None:
            return False
        fd, identity = opened
        try:
            if preserve_path is not None and self._path_is_within(preserve_path, session_dir):
                return False
            tombstone = self.root / f".{upload_id}.cleanup.{secrets.token_hex(8)}"
            os.rename(session_dir, tombstone)
            moved = tombstone.lstat()
            if identity != (moved.st_dev, moved.st_ino):
                # The source was swapped after authentication.  Never delete
                # the moved object; best-effort restoration minimizes surprise.
                if not self._session_dir_present(upload_id):
                    with suppress(OSError):
                        os.rename(tombstone, session_dir)
                return False
        except OSError:
            return False
        finally:
            os.close(fd)
        try:
            # CPython's fd-based implementation does not traverse child
            # symlinks; the preceding rename also removes the public race name.
            shutil.rmtree(tombstone)
        except OSError:
            if not self._session_dir_present(upload_id):
                with suppress(OSError):
                    os.rename(tombstone, session_dir)
            return False
        return True

    @staticmethod
    def _path_is_within(path: Path, parent: Path) -> bool:
        resolved_path = path.expanduser().resolve()
        resolved_parent = parent.expanduser().resolve()
        return resolved_path == resolved_parent or resolved_parent in resolved_path.parents

    @staticmethod
    def _regular_archive_size(path: Path) -> int:
        """Reject non-regular staging and return size before a potentially long hash."""

        try:
            observed = path.lstat()
        except OSError as exc:
            raise UploadArchiveError("staged archive is missing") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
            raise UploadArchiveError("staged archive must be a regular file")
        return observed.st_size

    @staticmethod
    def _hash_stable_archive(path: Path) -> tuple[int, str]:
        # Open the owning directory first and address the archive relative to
        # that fd.  This prevents an intermediate-directory symlink swap from
        # redirecting the hash to an attacker-selected path.
        directory_flags = os.O_RDONLY
        directory_flags |= os.O_DIRECTORY
        directory_flags |= os.O_CLOEXEC
        directory_flags |= os.O_NOFOLLOW
        archive_flags = os.O_RDONLY
        archive_flags |= os.O_CLOEXEC
        archive_flags |= os.O_NOFOLLOW
        digest = hashlib.sha256()
        count = 0
        directory_fd: int | None = None
        try:
            directory_fd = os.open(path.parent, directory_flags)
            directory_before = os.fstat(directory_fd)
            named_directory_before = path.parent.lstat()
            if not stat.S_ISDIR(directory_before.st_mode) or (
                directory_before.st_dev,
                directory_before.st_ino,
            ) != (named_directory_before.st_dev, named_directory_before.st_ino):
                raise UploadArchiveError("staged archive directory changed before hashing")
            before_path = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
                raise UploadArchiveError("staged archive must be a regular file")
            fd = os.open(path.name, archive_flags, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as handle:
                before_fd = os.fstat(handle.fileno())
                while chunk := handle.read(1024 * 1024):
                    count += len(chunk)
                    digest.update(chunk)
                after_fd = os.fstat(handle.fileno())
            after_path = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            directory_after = os.fstat(directory_fd)
            named_directory_after = path.parent.lstat()
        except UploadArchiveError:
            raise
        except OSError as exc:
            raise UploadArchiveError("staged archive could not be read safely") from exc
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
        identity_before = (
            before_path.st_dev,
            before_path.st_ino,
            before_path.st_size,
            before_path.st_mtime_ns,
        )
        identity_fd_before = (
            before_fd.st_dev,
            before_fd.st_ino,
            before_fd.st_size,
            before_fd.st_mtime_ns,
        )
        identity_fd_after = (
            after_fd.st_dev,
            after_fd.st_ino,
            after_fd.st_size,
            after_fd.st_mtime_ns,
        )
        identity_after = (
            after_path.st_dev,
            after_path.st_ino,
            after_path.st_size,
            after_path.st_mtime_ns,
        )
        if not (
            identity_before == identity_fd_before == identity_fd_after == identity_after
            and (directory_before.st_dev, directory_before.st_ino)
            == (directory_after.st_dev, directory_after.st_ino)
            == (named_directory_after.st_dev, named_directory_after.st_ino)
            and count == after_fd.st_size
        ):
            raise UploadArchiveError("staged archive changed while it was finalized")
        return count, digest.hexdigest()

    @staticmethod
    def _new_action_id(used: set[str]) -> str:
        for _ in range(100):
            action_id = f"act_{secrets.token_urlsafe(24)}"
            if _ACTION_ID_RE.fullmatch(action_id) and action_id not in used:
                return action_id
        raise UploadSessionError("could not allocate a unique action ID")

    @staticmethod
    def _find(sessions: Sequence[UploadSession], upload_id: str) -> tuple[int, UploadSession]:
        for index, session in enumerate(sessions):
            if session.upload_id == upload_id:
                return index, session
        raise UploadSessionNotFoundError(f"unknown upload ID: {upload_id}")

    @staticmethod
    def _find_action(
        sessions: Sequence[UploadSession], action_id: str
    ) -> tuple[int, int, UploadSession, UploadAction] | None:
        for session_index, session in enumerate(sessions):
            for action_index, action in enumerate(session.actions):
                if secrets.compare_digest(action.action_id, action_id):
                    return session_index, action_index, session, action
        return None

    def _load_current_locked(self, *, now: datetime) -> list[UploadSession]:
        """Load, durably expire, and clean safe terminal staging under the lock."""

        sessions = self._load()
        sessions, changed, _, _ = self._expire_details_locked(sessions, now=now)
        if changed:
            # State is made durable before any archive is deleted.  In
            # particular a timed-out PROCESSING record becomes AMBIGUOUS and is
            # therefore excluded from cleanup before control can return.
            self._save(sessions)
        for session in sessions:
            if session.state in _CLEANABLE_STATES:
                self._remove_owned_session_dir(
                    session.upload_id,
                    preserve_path=session.published_path,
                )
        return sessions

    def _expire_details_locked(
        self,
        sessions: list[UploadSession],
        *,
        now: datetime,
    ) -> tuple[list[UploadSession], bool, list[str], list[str]]:
        changed = False
        expired: list[str] = []
        ambiguous: list[str] = []
        for index, session in enumerate(sessions):
            if now < session.expires_at:
                continue
            if session.state in _EXPIRING_PRE_PROCESS_STATES:
                sessions[index] = self._transition(
                    session,
                    UploadState.DISCARDED,
                    now=now,
                    reason="upload session expired",
                )
                expired.append(session.upload_id)
                changed = True
            elif session.state is UploadState.PROCESSING:
                sessions[index] = self._transition(
                    session,
                    UploadState.AMBIGUOUS,
                    now=now,
                    reason="processing expired before a durable downstream outcome",
                )
                ambiguous.append(session.upload_id)
                changed = True
        return sessions, changed, expired, ambiguous

    def _read_state_text(self) -> str | None:
        flags = os.O_RDONLY
        flags |= os.O_CLOEXEC
        flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.state_path, flags)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("upload session state is not a regular file")
            if opened.st_size > _MAX_STATE_FILE_BYTES:
                raise ValueError("upload session state is too large")
            payload = handle.read(_MAX_STATE_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
            named = self.state_path.lstat()
        if len(payload) > _MAX_STATE_FILE_BYTES:
            raise ValueError("upload session state is too large")
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        named_identity = (
            named.st_dev,
            named.st_ino,
            named.st_size,
            named.st_mtime_ns,
        )
        if not stat.S_ISREG(named.st_mode) or not (
            opened_identity == after_identity == named_identity
        ):
            raise ValueError("upload session state changed while it was read")
        return payload.decode("utf-8")

    def _load(self) -> list[UploadSession]:
        try:
            text = self._read_state_text()
            if text is None:
                return []
            raw = json.loads(text, object_pairs_hook=_strict_json_object)
            if (
                not isinstance(raw, dict)
                or type(raw.get("schema_version")) is not int
                or raw.get("schema_version") != _SCHEMA_VERSION
            ):
                raise ValueError("unsupported upload session schema")
            raw_sessions = raw.get("sessions")
            if not isinstance(raw_sessions, list):
                raise ValueError("sessions must be a list")
            sessions = [self._session_from_dict(item) for item in raw_sessions]
            self._validate_unique_records(sessions)
            return sessions
        except UploadSessionStoreCorruptError:
            raise
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            OverflowError,
            RecursionError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            raise UploadSessionStoreCorruptError(
                f"upload session store is corrupt: {self.state_path}"
            ) from exc

    def _save(self, sessions: Sequence[UploadSession]) -> None:
        self._validate_unique_records(sessions)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "sessions": [self._session_to_dict(session) for session in sessions],
        }
        encoded = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=False)
        if len(encoded.encode("utf-8")) > _MAX_STATE_FILE_BYTES:
            raise UploadSessionError("upload session store exceeds its safe size limit")
        atomic_write_json(
            self.state_path,
            payload,
            ensure_ascii=True,
            indent=2,
        )

    @staticmethod
    def _validate_unique_records(sessions: Sequence[UploadSession]) -> None:
        upload_ids: set[str] = set()
        idempotency_keys: set[str] = set()
        action_ids: set[str] = set()
        for session in sessions:
            if session.upload_id in upload_ids:
                raise ValueError(f"duplicate upload ID: {session.upload_id}")
            if session.idempotency_key in idempotency_keys:
                raise ValueError("duplicate upload idempotency key")
            upload_ids.add(session.upload_id)
            idempotency_keys.add(session.idempotency_key)
            for action in session.actions:
                if action.action_id in action_ids:
                    raise ValueError("duplicate upload action ID")
                action_ids.add(action.action_id)

    def _session_to_dict(self, session: UploadSession) -> dict[str, object]:
        return {
            "upload_id": session.upload_id,
            "state": session.state.value,
            "binding": {
                "provider": session.binding.provider,
                "channel_id": session.binding.channel_id,
                "thread_id": session.binding.thread_id,
                "actor_id": session.binding.actor_id,
            },
            "message_id": session.message_id,
            "attachment_ids": list(session.attachment_ids),
            "idempotency_key": session.idempotency_key,
            "archive_relpath": f"{session.upload_id}/{UPLOAD_ARCHIVE_FILE_NAME}",
            "expected_bytes": session.expected_bytes,
            "actual_bytes": session.actual_bytes,
            "sha256": session.sha256,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "expires_at": session.expires_at.isoformat(),
            "verification": session.verification,
            "actions": [self._action_to_dict(action) for action in session.actions],
            "consumed_action_id": session.consumed_action_id,
            "published_path": (
                str(session.published_path) if session.published_path is not None else None
            ),
            "receipt": self._receipt_to_dict(session.receipt),
            "reason": session.reason,
        }

    def _session_from_dict(self, raw: object) -> UploadSession:
        if not isinstance(raw, dict):
            raise ValueError("session record must be an object")
        upload_id = str(raw.get("upload_id", "")).strip()
        if not _UPLOAD_ID_RE.fullmatch(upload_id):
            raise ValueError("invalid persisted upload ID")
        state = UploadState(str(raw.get("state", "")).strip())
        created_at = _parse_time(raw.get("created_at"), field_name="created_at")
        updated_at = _parse_time(raw.get("updated_at"), field_name="updated_at")
        expires_at = _parse_time(raw.get("expires_at"), field_name="expires_at")
        if updated_at < created_at or expires_at < created_at:
            raise ValueError("upload session timestamps are out of order")
        raw_binding = raw.get("binding")
        if not isinstance(raw_binding, dict):
            raise ValueError("upload binding must be an object")
        binding = UploadBinding(
            provider=str(raw_binding.get("provider", "")),
            channel_id=str(raw_binding.get("channel_id", "")),
            thread_id=_optional_text(raw_binding.get("thread_id")),
            actor_id=str(raw_binding.get("actor_id", "")),
        )
        message_id = str(raw.get("message_id", "")).strip()
        raw_attachment_ids = raw.get("attachment_ids")
        if not message_id or not isinstance(raw_attachment_ids, list):
            raise ValueError("invalid upload source")
        attachment_ids = tuple(str(item).strip() for item in raw_attachment_ids)
        if not attachment_ids or any(not item for item in attachment_ids):
            raise ValueError("invalid attachment IDs")
        if attachment_ids != tuple(sorted(set(attachment_ids))):
            raise ValueError("attachment IDs are not in canonical order")
        key = str(raw.get("idempotency_key", "")).strip().lower()
        expected_key = upload_idempotency_key(
            binding,
            message_id=message_id,
            attachment_ids=attachment_ids,
        )
        if key != expected_key:
            raise ValueError("upload idempotency key does not match its source")
        expected_relpath = f"{upload_id}/{UPLOAD_ARCHIVE_FILE_NAME}"
        if raw.get("archive_relpath") != expected_relpath:
            raise ValueError("archive path is not contained in the upload session directory")
        expected_bytes = _nonnegative_int(raw.get("expected_bytes"), name="expected_bytes")
        raw_actual_bytes = raw.get("actual_bytes")
        actual_bytes = (
            None
            if raw_actual_bytes is None
            else _nonnegative_int(raw_actual_bytes, name="actual_bytes")
        )
        sha256 = _optional_text(raw.get("sha256"))
        if sha256 is not None and not _SHA256_RE.fullmatch(sha256):
            raise ValueError("invalid upload SHA-256")
        if (actual_bytes is None) != (sha256 is None):
            raise ValueError("upload size and SHA-256 must be finalized together")
        if state not in {UploadState.RECEIVING, UploadState.FAILED, UploadState.DISCARDED} and (
            actual_bytes is None or sha256 is None
        ):
            raise ValueError("advanced upload state is missing finalized archive identity")
        verification_raw = raw.get("verification", {})
        if not isinstance(verification_raw, dict):
            raise ValueError("verification must be an object")
        verification = _json_mapping(verification_raw)
        raw_actions = raw.get("actions", [])
        if not isinstance(raw_actions, list):
            raise ValueError("actions must be a list")
        actions = tuple(self._action_from_dict(item) for item in raw_actions)
        action_kinds = {action.kind for action in actions}
        if actions and (
            len(actions) != 2
            or action_kinds != {UploadActionKind.CONFIRM, UploadActionKind.DISMISS}
        ):
            raise ValueError("upload session has an invalid action set")
        if any(
            action.created_at < created_at
            or action.created_at > updated_at
            or (action.consumed_at is not None and action.consumed_at > updated_at)
            for action in actions
        ):
            raise ValueError("upload action timestamps are outside the session timeline")
        consumed_action_id = _optional_text(raw.get("consumed_action_id"))
        if consumed_action_id is not None and consumed_action_id not in {
            action.action_id for action in actions if action.consumed_at is not None
        }:
            raise ValueError("consumed action ID is not a consumed session action")
        if state is UploadState.AWAITING_CONFIRM:
            if action_kinds != {
                UploadActionKind.CONFIRM,
                UploadActionKind.DISMISS,
            } or any(action.consumed_at is not None for action in actions):
                raise ValueError("awaiting upload has invalid confirmation actions")
            if any(action.expires_at != expires_at for action in actions):
                raise ValueError("awaiting upload expiry does not match its actions")
        if state in {UploadState.RECEIVING, UploadState.VERIFIED} and (
            actions or consumed_action_id is not None
        ):
            raise ValueError("pre-confirmation upload unexpectedly has actions")
        if state in {
            UploadState.PROCESSING,
            UploadState.PUBLISHED,
            UploadState.COMMITTED,
            UploadState.AMBIGUOUS,
        }:
            consumed = next(
                (action for action in actions if action.action_id == consumed_action_id),
                None,
            )
            if consumed is None or consumed.kind is not UploadActionKind.CONFIRM:
                raise ValueError("processed upload has no consumed confirm action")
            if sum(action.consumed_at is not None for action in actions) != 1:
                raise ValueError("processed upload has an invalid consumed action set")
        published_text = _optional_text(raw.get("published_path"))
        published_path = Path(published_text).expanduser().resolve() if published_text else None
        receipt = self._receipt_from_dict(raw.get("receipt"))
        if state is UploadState.COMMITTED and receipt is None:
            raise ValueError("committed upload is missing a receipt")
        if receipt is not None and state is not UploadState.COMMITTED:
            raise ValueError("only committed uploads may carry a receipt")
        if receipt is not None and not (created_at <= receipt.committed_at <= updated_at):
            raise ValueError("upload commit receipt timestamp is outside the session timeline")
        if state in {UploadState.PUBLISHED, UploadState.COMMITTED} and published_path is None:
            raise ValueError("published upload is missing its path")
        if published_path is not None and state not in {
            UploadState.PUBLISHED,
            UploadState.COMMITTED,
            UploadState.AMBIGUOUS,
            UploadState.FAILED,
        }:
            raise ValueError("unpublished upload unexpectedly has a published path")
        return UploadSession(
            upload_id=upload_id,
            state=state,
            binding=binding,
            message_id=message_id,
            attachment_ids=attachment_ids,
            idempotency_key=key,
            archive_path=self.archive_path(upload_id),
            expected_bytes=expected_bytes,
            actual_bytes=actual_bytes,
            sha256=sha256,
            created_at=created_at,
            updated_at=updated_at,
            expires_at=expires_at,
            verification=verification,
            actions=actions,
            consumed_action_id=consumed_action_id,
            published_path=published_path,
            receipt=receipt,
            reason=_optional_text(raw.get("reason")),
        )

    @staticmethod
    def _action_to_dict(action: UploadAction) -> dict[str, object]:
        return {
            "action_id": action.action_id,
            "kind": action.kind.value,
            "created_at": action.created_at.isoformat(),
            "expires_at": action.expires_at.isoformat(),
            "consumed_at": (
                action.consumed_at.isoformat() if action.consumed_at is not None else None
            ),
        }

    @staticmethod
    def _action_from_dict(raw: object) -> UploadAction:
        if not isinstance(raw, dict):
            raise ValueError("upload action must be an object")
        action_id = str(raw.get("action_id", "")).strip()
        if not _ACTION_ID_RE.fullmatch(action_id):
            raise ValueError("invalid upload action ID")
        consumed_raw = raw.get("consumed_at")
        action = UploadAction(
            action_id=action_id,
            kind=UploadActionKind(str(raw.get("kind", "")).strip()),
            created_at=_parse_time(raw.get("created_at"), field_name="action.created_at"),
            expires_at=_parse_time(raw.get("expires_at"), field_name="action.expires_at"),
            consumed_at=(
                None
                if consumed_raw is None
                else _parse_time(consumed_raw, field_name="action.consumed_at")
            ),
        )
        if action.expires_at <= action.created_at:
            raise ValueError("upload action expires before it was created")
        if action.consumed_at is not None and action.consumed_at < action.created_at:
            raise ValueError("upload action was consumed before it was created")
        return action

    @staticmethod
    def _receipt_to_dict(receipt: UploadCommitReceipt | None) -> dict[str, object] | None:
        if receipt is None:
            return None
        return {
            "committed_at": receipt.committed_at.isoformat(),
            "queue_id": receipt.queue_id,
            "workflow_id": receipt.workflow_id,
        }

    @staticmethod
    def _receipt_from_dict(raw: object) -> UploadCommitReceipt | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("upload commit receipt must be an object")
        queue_id = _optional_text(raw.get("queue_id"))
        workflow_id = _optional_text(raw.get("workflow_id"))
        if queue_id is None and workflow_id is None:
            raise ValueError("upload commit receipt has no downstream identity")
        return UploadCommitReceipt(
            committed_at=_parse_time(raw.get("committed_at"), field_name="committed_at"),
            queue_id=queue_id,
            workflow_id=workflow_id,
        )


__all__ = [
    "UPLOAD_ARCHIVE_FILE_NAME",
    "UPLOAD_SESSIONS_FILE_NAME",
    "UPLOAD_SESSIONS_LOCK_NAME",
    "UploadAction",
    "UploadActionConsumedError",
    "UploadActionConsumption",
    "UploadActionExpiredError",
    "UploadActionKind",
    "UploadActionNotFoundError",
    "UploadActionSet",
    "UploadArchiveError",
    "UploadBinding",
    "UploadBindingMismatchError",
    "UploadCommitReceipt",
    "UploadQuotaExceededError",
    "UploadReservation",
    "UploadSession",
    "UploadSessionError",
    "UploadSessionNotFoundError",
    "UploadSessionStore",
    "UploadSessionStoreCorruptError",
    "UploadState",
    "UploadStateConflictError",
    "UploadSweepResult",
    "upload_idempotency_key",
]
