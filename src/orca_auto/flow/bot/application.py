"""Provider-neutral command and component dispatch for the orca_auto bot."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
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
from orca_auto.core.ingest import (
    UploadPolicy,
    UploadRejected,
    extract_archive,
    inspect_archive,
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

from .._orca_stage_materialization import safe_name
from ..activity import cancel_activity, clear_activities, list_activities
from .action_registry import ActionKind, ActionRegistry, ActionStore, RegisteredAction
from .settings import BotSettings

LOGGER = logging.getLogger(__name__)

# Uploaded archives stage under this hidden directory inside the runs root while
# they await a confirm/dismiss decision.  Abandoned stagings are swept on the
# next upload so a never-confirmed archive cannot accumulate on disk.
_UPLOAD_STAGING_DIRNAME = ".uploads"
_UPLOAD_STAGING_TTL_SECONDS = 3600.0
_RUN_BINDING_PREFIX = "u1:"

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
    upload_policy: UploadPolicy | None = None

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

    def stage_upload_path(self, filename: str) -> Path:
        """Reserve a fresh host path for a provider to download an attachment to.

        The returned path lives inside the runs root so a later extraction and
        submission validate against the same trusted root.
        """

        runs_root = self._runs_root()
        if runs_root is None:
            raise RuntimeError("runs_root is not configured; uploads are unavailable")
        staging = self._staging_dir(runs_root)
        staging.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(8)
        safe = safe_name(Path(filename).name, fallback="upload")
        return staging / f"{token}__{safe}"

    def _sweep_stale_uploads(self, runs_root: Path) -> None:
        staging = self._staging_dir(runs_root)
        if not staging.is_dir():
            return
        cutoff = time.time() - _UPLOAD_STAGING_TTL_SECONDS
        for entry in staging.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:  # noqa: PERF203 - best-effort cleanup, keep sweeping
                LOGGER.debug("upload_staging_sweep_skip: %s", entry.name)

    @staticmethod
    def _encode_run_binding(*, archive: str, name: str, engine: str) -> str:
        return _RUN_BINDING_PREFIX + json.dumps(
            {"archive": archive, "name": name, "engine": engine},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode_run_binding(binding: str) -> tuple[Path, str, str]:
        if not binding.startswith(_RUN_BINDING_PREFIX):
            raise ValueError("Upload binding is invalid.")
        try:
            payload = json.loads(binding.removeprefix(_RUN_BINDING_PREFIX))
        except (TypeError, ValueError):
            raise ValueError("Upload binding is invalid.") from None
        if not isinstance(payload, dict):
            raise ValueError("Upload binding is invalid.")
        archive = str(payload.get("archive") or "").strip()
        name = str(payload.get("name") or "").strip()
        engine = str(payload.get("engine") or "unknown").strip()
        if not archive or not name:
            raise ValueError("Upload binding is invalid.")
        return Path(archive), name, engine

    def _staged_archive(self, archive: Path, runs_root: Path) -> Path | None:
        """Return ``archive`` only when it still resolves inside the staging dir."""

        staging = self._staging_dir(runs_root).resolve()
        try:
            resolved = archive.resolve()
        except OSError:
            return None
        if resolved.parent != staging or not resolved.is_file():
            return None
        return resolved

    def dispatch_upload(
        self,
        upload: IncomingUpload,
        *,
        messenger: InteractiveMessenger,
    ) -> str:
        """Validate an uploaded archive and ask the operator to confirm the queue."""

        self._check_provider(upload.address, messenger)
        policy = self.upload_policy
        archive_path = Path(upload.archive_path)
        runs_root = self._runs_root()
        if policy is None or not policy.enabled:
            _safe_unlink(archive_path)
            status, reply = "upload-disabled", BotReply("File uploads are disabled.")
        elif runs_root is None:
            _safe_unlink(archive_path)
            status, reply = "upload-misconfigured", BotReply("Upload staging is not configured.")
        else:
            self._sweep_stale_uploads(runs_root)
            status, reply = self._upload_confirmation(upload, archive_path, policy)
        result = messenger.send_reply(upload.address, reply)
        return status if result.sent else f"{status}-delivery-failed"

    def _upload_confirmation(
        self,
        upload: IncomingUpload,
        archive_path: Path,
        policy: UploadPolicy,
    ) -> tuple[str, BotReply]:
        try:
            report = inspect_archive(archive_path, policy)
        except UploadRejected as exc:
            _safe_unlink(archive_path)
            return "upload-rejected", BotReply(f"Rejected: {exc.reason}")
        job_name = safe_name(
            report.suggested_name or Path(upload.filename).stem,
            fallback="upload",
        )
        binding = self._encode_run_binding(
            archive=str(archive_path),
            name=job_name,
            engine=report.engine_hint,
        )
        confirm_id, dismiss_id = self.actions.issue_group(
            (("run_confirm", binding), ("run_dismiss", binding)),
            address=upload.address,
            actor=upload.actor,
        )
        mib = report.total_uncompressed / (1024 * 1024)
        return "upload-confirmation-sent", BotReply(
            f"Queue {job_name}? ({report.engine_hint}, {report.entry_count} files, {mib:.1f} MiB)",
            actions=(
                (
                    CardAction(confirm_id, "Queue it"),
                    CardAction(dismiss_id, "Discard"),
                ),
            ),
        )

    def _run_submit_result(self, binding: str) -> BotReply:
        try:
            archive, name, _engine = self._decode_run_binding(binding)
        except ValueError as exc:
            return BotReply(str(exc))
        policy = self.upload_policy
        runs_root = self._runs_root()
        if policy is None or not policy.enabled:
            return BotReply("File uploads are disabled.")
        if runs_root is None:
            return BotReply("Upload staging is not configured.")
        staged = self._staged_archive(archive, runs_root)
        if staged is None:
            return BotReply("Upload expired or was already processed.")
        try:
            job_dir = extract_archive(staged, runs_root, name, policy)
        except UploadRejected as exc:
            return BotReply(f"Rejected: {exc.reason}")
        finally:
            _safe_unlink(staged)
        ok, _detail = self._submit_extracted_run_dir(job_dir)
        if not ok:
            # The run-dir was freshly extracted but never enqueued; leaving it in
            # the runs root would strand a phantom un-submitted dir the sweep
            # (which only touches staging) never reclaims.
            _rmtree_quiet(job_dir)
            return BotReply(f"Submission failed for {job_dir.name}. See the bot log.")
        return BotReply(f"Queued {job_dir.name}. Track it with the list command.")

    def _run_discard(self, binding: str) -> None:
        try:
            archive, _name, _engine = self._decode_run_binding(binding)
        except ValueError:
            return
        runs_root = self._runs_root()
        if runs_root is None:
            return
        staged = self._staged_archive(archive, runs_root)
        if staged is not None:
            _safe_unlink(staged)

    def _submit_extracted_run_dir(self, job_dir: Path) -> tuple[bool, str]:
        """Enqueue an extracted run-dir through the shared CLI submission path.

        Sub-handlers are called directly (not the ``cmd_orca_run_dir`` wrapper) so
        the long-lived bot process never has its logging reconfigured per upload.
        Their stdout/stderr flows to the daemon log as-is: the streams are NOT
        redirected, because ``redirect_stdout`` mutates the process-global
        ``sys.stdout`` and two channels submitting concurrently on the worker pool
        would corrupt each other's restore of it.
        """

        from orca_auto import cli_handlers
        from orca_auto.cli_common import _engine_config_for_command

        args = argparse.Namespace(
            path=str(job_dir),
            workflow_dir=str(job_dir),
            config=self.settings.orca_config,
            priority=10,
            max_cores=None,
            max_memory_gb=None,
            force=False,
            json=False,
            verbose=False,
            log_file=None,
        )
        try:
            run_dir_app = cli_handlers._detect_run_dir_app(args)
        except ValueError as exc:
            LOGGER.warning("upload_submit_detect_failed: %s", exc)
            return False, str(exc)

        try:
            if run_dir_app == "workflow":
                args.run_dir_app = "workflow"
                rc = int(cli_handlers.cmd_workflow_run_dir(args))
            else:
                from orca_auto.orca.commands.run_inp import cmd_run_inp

                args.run_dir_app = "orca"
                args.config = _engine_config_for_command(args)
                rc = int(cmd_run_inp(args))
        except Exception as exc:  # noqa: BLE001 - submitter failures are reported to the operator
            LOGGER.warning("upload_submit_failed: %s", exc, exc_info=True)
            return False, type(exc).__name__
        return rc == 0, ""

    @staticmethod
    def _help_reply(prefix: str = "/") -> BotReply:
        return BotReply(
            "orca_auto bot commands\n\n"
            f"{prefix}list — Show unified activities\n"
            f"{prefix}list clear — Remove completed, failed, and cancelled entries\n"
            f"{prefix}list running — Show running activities only\n"
            f"{prefix}list failed — Show failed activities only\n"
            f"{prefix}cancel TARGET — Ask to cancel a workflow or queued job\n"
            f"{prefix}run — Attach a .zip/.tar.gz run-dir to queue it\n"
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
        elif name == "run":
            reply = BotReply(f"Attach a .zip or .tar.gz run-dir to {prefix}run to queue it.")
            status = "run-usage"
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
        if registered.kind == "run_confirm":
            self._acknowledge(incoming, "Submitting…", messenger)
            self._send_action_reply(incoming, self._run_submit_result(registered.target), messenger)
            return "run-submitted"
        if registered.kind == "run_dismiss":
            self._run_discard(registered.target)
            self._acknowledge(incoming, "Upload discarded.", messenger)
            self._send_action_reply(incoming, BotReply("Upload discarded."), messenger)
            return "run-dismissed"
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


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.debug("upload_unlink_skip: %s", path.name)


def _rmtree_quiet(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


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
    "dispatch_action",
    "dispatch_command",
    "dispatch_upload",
]
