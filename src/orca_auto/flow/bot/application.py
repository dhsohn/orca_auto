"""Provider-neutral command and component dispatch for the orca_auto bot."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

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
from .interaction_delivery import (
    acknowledge,
    check_provider,
    clear_origin_actions,
    invalid_action_text,
    send_action_reply,
)
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

if TYPE_CHECKING:
    from .upload_application import UploadApplication

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


@dataclass
class BotApplication:
    """Own provider-neutral list/cancel/help routing for every native adapter."""

    settings: BotSettings
    actions: ActionStore = field(default_factory=ActionRegistry)
    deps: BotApplicationDeps = field(default_factory=BotApplicationDeps)
    uploads: UploadApplication | None = None

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

    def _help_reply(self, prefix: str, *, provider: str) -> BotReply:
        commands = [
            (f"{prefix}list", "Show unified activities"),
            (f"{prefix}list clear", "Remove completed, failed, and cancelled entries"),
            (f"{prefix}list running", "Show running activities only"),
            (f"{prefix}list failed", "Show failed activities only"),
            (f"{prefix}cancel TARGET", "Ask to cancel a workflow or queued job"),
        ]
        if self.uploads is not None and self.uploads.command_available(provider):
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
    def _command_name(command: IncomingCommand) -> str:
        return command.command.strip().lstrip("/!").split("@", 1)[0].lower()

    def dispatch_command(
        self,
        command: IncomingCommand,
        *,
        messenger: InteractiveMessenger,
    ) -> str:
        """Dispatch one normalized provider command and send its reply."""

        check_provider(command.address, messenger)
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
            elif self.uploads is None or not self.uploads.enabled:
                reply = BotReply(
                    "File uploads are disabled.",
                    message=error_message("Uploads disabled", "File uploads are disabled."),
                )
                status = "run-disabled"
            elif not self.uploads.configured:
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

    def _dispatch_registered_action(
        self,
        registered: RegisteredAction,
        incoming: IncomingAction,
        messenger: InteractiveMessenger,
    ) -> str:
        clear_origin_actions(incoming, messenger)
        if registered.kind == "cancel_prompt":
            acknowledge(incoming, "Confirmation required.", messenger)
            send_action_reply(
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
            acknowledge(incoming, "Cancellation processed.", messenger)
            send_action_reply(incoming, reply, messenger)
            send_action_reply(
                incoming,
                self._list_reply(address=incoming.address, actor=incoming.actor),
                messenger,
            )
            return "cancel-processed"
        if registered.kind == "cancel_dismiss":
            acknowledge(incoming, "Cancellation dismissed.", messenger)
            send_action_reply(
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
            acknowledge(incoming, "Refreshed.", messenger)
            send_action_reply(
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
            acknowledge(incoming, "Finished entries cleared.", messenger)
            clear_text = self._list_clear_text()
            send_action_reply(
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
            send_action_reply(
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

        check_provider(action.address, messenger)
        upload_status = (
            self.uploads.dispatch_action(action, messenger) if self.uploads is not None else None
        )
        if upload_status is not None:
            return upload_status
        resolution = self.actions.consume(
            action.action_id,
            address=action.address,
            actor=action.actor,
        )
        if resolution.action is None:
            text = invalid_action_text(resolution.status)
            acknowledge(action, text, messenger)
            send_action_reply(
                action,
                BotReply(text, message=error_message("Action unavailable", text)),
                messenger,
            )
            return resolution.status
        return self._dispatch_registered_action(resolution.action, action, messenger)


__all__ = [
    "BotApplication",
    "BotApplicationDeps",
]
