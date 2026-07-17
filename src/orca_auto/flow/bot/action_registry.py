"""Bounded, expiring storage for opaque interactive action ids."""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from orca_auto.core.messaging.interactive import Actor, ConversationAddress

ActionKind = Literal[
    "cancel_prompt",
    "cancel_confirm",
    "cancel_dismiss",
    "list_refresh",
    "list_clear",
    "run_confirm",
    "run_dismiss",
]
ResolutionStatus = Literal["ok", "unknown", "expired", "wrong_address", "wrong_actor"]

_ACTION_ID_PREFIX = "oa:"
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_MAX_TOKEN_ATTEMPTS = 32


@dataclass(frozen=True)
class RegisteredAction:
    action_id: str
    kind: ActionKind
    target: str
    address: ConversationAddress
    actor_id: str
    expires_at: float
    group_id: str


@dataclass(frozen=True)
class ActionResolution:
    status: ResolutionStatus
    action: RegisteredAction | None = None


@runtime_checkable
class ActionStore(Protocol):
    """Storage port; a durable implementation can span worker and gateway processes."""

    def issue_group(
        self,
        specs: Sequence[tuple[ActionKind, str]],
        *,
        address: ConversationAddress,
        actor: Actor,
    ) -> tuple[str, ...]: ...

    def consume(
        self,
        action_id: str,
        *,
        address: ConversationAddress,
        actor: Actor,
    ) -> ActionResolution: ...


class ActionRegistry:
    """Issue short opaque ids and consume them exactly once.

    Entries are bound to provider-qualified conversation and actor.  A failed
    binding check does not consume the entry, so another user cannot invalidate
    the intended user's confirmation button.  Related actions (for example,
    Confirm/Dismiss) share a group and are invalidated atomically when either is
    selected.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("action ttl_seconds must be positive")
        if max_entries < 2:
            raise ValueError("action max_entries must be at least 2")
        self._ttl_seconds = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(12))
        self._entries: dict[str, RegisteredAction] = {}
        self._lock = threading.RLock()

    def _new_action_id(self, reserved: set[str]) -> str:
        for _attempt in range(_MAX_TOKEN_ATTEMPTS):
            token = str(self._token_factory()).strip()
            if not _SAFE_TOKEN.fullmatch(token):
                continue
            action_id = f"{_ACTION_ID_PREFIX}{token}"
            if action_id not in self._entries and action_id not in reserved:
                return action_id
        raise RuntimeError("could not allocate a unique safe action id")

    def _drop_group(self, group_id: str) -> None:
        for action_id in tuple(self._entries):
            if self._entries[action_id].group_id == group_id:
                del self._entries[action_id]

    def _purge_expired(self, now: float) -> None:
        expired_groups = {
            action.group_id for action in self._entries.values() if action.expires_at <= now
        }
        for group_id in expired_groups:
            self._drop_group(group_id)

    def _make_room(self, count: int) -> None:
        if count > self._max_entries:
            raise ValueError("action group exceeds registry capacity")
        while len(self._entries) + count > self._max_entries:
            oldest = next(iter(self._entries.values()))
            self._drop_group(oldest.group_id)

    def issue_group(
        self,
        specs: Sequence[tuple[ActionKind, str]],
        *,
        address: ConversationAddress,
        actor: Actor,
    ) -> tuple[str, ...]:
        """Register related actions and return provider-safe opaque ids."""

        if not specs:
            return ()
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            self._make_room(len(specs))
            reserved: set[str] = set()
            action_ids: list[str] = []
            for _kind, _target in specs:
                action_id = self._new_action_id(reserved)
                reserved.add(action_id)
                action_ids.append(action_id)
            group_id = action_ids[0]
            actor_id = actor.user_id.strip()
            expires_at = now + self._ttl_seconds
            for action_id, (kind, target) in zip(action_ids, specs, strict=True):
                self._entries[action_id] = RegisteredAction(
                    action_id=action_id,
                    kind=kind,
                    target=target,
                    address=address,
                    actor_id=actor_id,
                    expires_at=expires_at,
                    group_id=group_id,
                )
            return tuple(action_ids)

    def issue(
        self,
        kind: ActionKind,
        *,
        target: str = "",
        address: ConversationAddress,
        actor: Actor,
    ) -> str:
        return self.issue_group(
            ((kind, target),),
            address=address,
            actor=actor,
        )[0]

    def consume(
        self,
        action_id: str,
        *,
        address: ConversationAddress,
        actor: Actor,
    ) -> ActionResolution:
        """Resolve and atomically invalidate an action group on a valid click."""

        now = self._clock()
        with self._lock:
            action = self._entries.get(action_id)
            if action is not None and action.expires_at <= now:
                self._drop_group(action.group_id)
                return ActionResolution("expired")
            self._purge_expired(now)
            action = self._entries.get(action_id)
            if action is None:
                return ActionResolution("unknown")
            if action.address != address:
                return ActionResolution("wrong_address")
            if action.actor_id != actor.user_id.strip():
                return ActionResolution("wrong_actor")
            self._drop_group(action.group_id)
            return ActionResolution("ok", action)

    @property
    def pending_count(self) -> int:
        with self._lock:
            self._purge_expired(self._clock())
            return len(self._entries)


__all__ = [
    "ActionKind",
    "ActionRegistry",
    "ActionResolution",
    "ActionStore",
    "RegisteredAction",
    "ResolutionStatus",
]
