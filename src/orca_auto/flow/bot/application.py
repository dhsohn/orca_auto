"""Provider-neutral command and component dispatch for the orca_auto bot."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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
from orca_auto.core.messaging.interactive import (
    Actor,
    BotReply,
    CardAction,
    ConversationAddress,
    IncomingAction,
    IncomingCommand,
    InteractiveMessenger,
)
from orca_auto.core.statuses import QUEUE_ACTIVE_STATUSES

from ..activity import cancel_activity, clear_activities, list_activities
from .action_registry import ActionKind, ActionRegistry, ActionStore, RegisteredAction
from .settings import BotSettings

LOGGER = logging.getLogger(__name__)

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


@dataclass
class BotApplication:
    """Shared orca_auto bot application used by every native adapter."""

    settings: BotSettings
    actions: ActionStore = field(default_factory=ActionRegistry)
    deps: BotApplicationDeps = field(default_factory=BotApplicationDeps)

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

    @staticmethod
    def _help_reply(prefix: str = "/") -> BotReply:
        return BotReply(
            "orca_auto bot commands\n\n"
            f"{prefix}list — Show unified activities\n"
            f"{prefix}list clear — Remove completed, failed, and cancelled entries\n"
            f"{prefix}list running — Show running activities only\n"
            f"{prefix}list failed — Show failed activities only\n"
            f"{prefix}cancel TARGET — Ask to cancel a workflow or queued job\n"
            f"{prefix}help — Show this help message"
        )

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
        elif name in {"help", "start"}:
            reply = self._help_reply(prefix)
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

    def dispatch_action(
        self,
        action: IncomingAction,
        *,
        messenger: InteractiveMessenger,
    ) -> str:
        """Resolve one bound one-time action and perform its application effect."""

        self._check_provider(action.address, messenger)
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


def dispatch_command(
    application: BotApplication,
    command: IncomingCommand,
    *,
    messenger: InteractiveMessenger,
) -> str:
    """Adapter-facing functional facade for command dispatch."""

    return application.dispatch_command(command, messenger=messenger)


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
    "dispatch_action",
    "dispatch_command",
]
