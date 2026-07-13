from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from orca_auto.core.messaging.channel import SendResult
from orca_auto.core.messaging.interactive import (
    Actor,
    BotReply,
    CardAction,
    ConversationAddress,
    IncomingAction,
    IncomingCommand,
)
from orca_auto.flow.bot import (
    ActionRegistry,
    BotApplication,
    BotApplicationDeps,
    BotSettings,
    settings_from_config,
)


class FakeMessenger:
    provider = "discord"

    def __init__(self) -> None:
        self.replies: list[tuple[ConversationAddress, BotReply]] = []
        self.edits: list[tuple[ConversationAddress, str, object]] = []
        self.acks: list[tuple[IncomingAction, str]] = []

    def send_reply(
        self,
        address: ConversationAddress,
        reply: BotReply,
        *,
        silent: bool = False,
    ) -> SendResult:
        del silent
        self.replies.append((address, reply))
        message_id = str(len(self.replies))
        return SendResult(sent=True, provider=self.provider, message_id=message_id)

    def edit_actions(
        self,
        address: ConversationAddress,
        message_id: str,
        actions: object,
    ) -> SendResult:
        self.edits.append((address, message_id, actions))
        return SendResult(sent=True, provider=self.provider, message_id=message_id)

    def acknowledge(self, action: IncomingAction, text: str) -> SendResult:
        self.acks.append((action, text))
        return SendResult(sent=True, provider=self.provider)


class ActivityFixture:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.clear_count = 0
        self.child_job_engine_requests: list[tuple[str, ...] | None] = []
        self.activities = [
            {
                "activity_id": "run-1",
                "label": "running-job",
                "status": "running",
                "kind": "job",
                "engine": "orca",
                "source": "orca_auto_orca",
            },
            {
                "activity_id": "run-2",
                "label": "failed-job",
                "status": "failed",
                "kind": "job",
                "engine": "orca",
                "source": "orca_auto_orca",
            },
        ]

    def list_activities(self, **kwargs: Any) -> dict[str, Any]:
        self.child_job_engine_requests.append(kwargs.get("child_job_engines"))
        return {"activities": [dict(item) for item in self.activities], "sources": {}}

    def clear_activities(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.clear_count += 1
        return {"total_cleared": 3}

    def cancel_activity(self, *, target: str, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.cancelled.append(target)
        return {"activity_id": target, "label": f"cancelled-{target}", "status": "cancelled"}


def _filter_items(
    items: list[dict[str, Any]],
    *,
    statuses: tuple[str, ...],
) -> list[dict[str, Any]]:
    wanted = {status.lower() for status in statuses}
    return [item for item in items if str(item.get("status", "")).lower() in wanted]


def _present(_payload: dict[str, Any], *, request: Any, deps: Any) -> Any:
    del deps
    labels = [str(item.get("label")) for item in request.visible_items]
    return SimpleNamespace(lines=["activities", *labels])


def _settings() -> BotSettings:
    return BotSettings(
        workflow_root="/runs",
        crest_config="/config/orca_auto.yaml",
        xtb_config="/config/orca_auto.yaml",
        orca_config="/config/orca_auto.yaml",
        orca_repo_root="/repo",
    )


def _deps(fixture: ActivityFixture) -> BotApplicationDeps:
    return BotApplicationDeps(
        list_activities=fixture.list_activities,
        clear_activities=fixture.clear_activities,
        cancel_activity=fixture.cancel_activity,
        filter_activity_items=_filter_items,
        queue_list_text_presentation=_present,
        queue_list_default_visible_items=lambda items: list(items),
        queue_clear_lines=lambda payload: [f"cleared: {payload['total_cleared']}"],
        status_icon=lambda status: {"cancelled": "X"}.get(status, "?"),
    )


def _application(
    fixture: ActivityFixture,
    *,
    registry: ActionRegistry | None = None,
) -> BotApplication:
    return BotApplication(
        settings=_settings(),
        actions=registry or ActionRegistry(),
        deps=_deps(fixture),
    )


ADDRESS = ConversationAddress(provider="discord", channel_id="100")
OTHER_ADDRESS = ConversationAddress(provider="discord", channel_id="200")
ACTOR = Actor(user_id="user-1", label="owner")
OTHER_ACTOR = Actor(user_id="user-2", label="other")


def _command(name: str, args: str = "", *, actor: Actor = ACTOR) -> IncomingCommand:
    return IncomingCommand(address=ADDRESS, actor=actor, command=name, args=args)


def _find_action(reply: BotReply, label: str) -> CardAction:
    return next(action for row in reply.actions for action in row if action.label == label)


def _incoming_action(
    action_id: str,
    *,
    address: ConversationAddress = ADDRESS,
    actor: Actor = ACTOR,
    message_id: str = "message-1",
) -> IncomingAction:
    return IncomingAction(
        address=address,
        actor=actor,
        action_id=action_id,
        ack_token="ack-1",
        message_id=message_id,
    )


def test_list_filter_is_preformatted_and_builds_neutral_actions() -> None:
    fixture = ActivityFixture()
    application = _application(fixture)
    messenger = FakeMessenger()

    assert application.dispatch_command(_command("/list", "running"), messenger=messenger) == (
        "list-sent"
    )

    reply = messenger.replies[-1][1]
    assert reply.format == "preformatted"
    assert "running-job" in reply.text
    assert "failed-job" not in reply.text
    labels = [action.label for row in reply.actions for action in row]
    assert any(label.startswith("Cancel ") and label.endswith("running-job") for label in labels)
    assert labels[-2:] == ["Refresh", "Clear finished"]
    assert "<b>" not in reply.text
    assert "```" not in reply.text


def test_list_clear_and_help_use_discord_command_prefix() -> None:
    fixture = ActivityFixture()
    application = _application(fixture)
    messenger = FakeMessenger()

    assert application.dispatch_command(_command("list", "clear"), messenger=messenger) == (
        "list-cleared"
    )
    assert fixture.clear_count == 1
    clear_reply = messenger.replies[-1][1]
    assert clear_reply.text == "cleared: 3"
    assert clear_reply.format == "preformatted"
    assert clear_reply.message is not None
    assert clear_reply.message.title == "Cleared"
    assert clear_reply.message.author == "orca_auto"

    assert application.dispatch_command(_command("help"), messenger=messenger) == "help-sent"
    help_reply = messenger.replies[-1][1]
    assert "!list" in help_reply.text and "!cancel TARGET" in help_reply.text
    assert "<b>" not in help_reply.text
    assert "```" not in help_reply.text


def test_interactive_replies_carry_orca_auto_embed_message() -> None:
    application = _application(ActivityFixture())
    messenger = FakeMessenger()

    application.dispatch_command(_command("help"), messenger=messenger)
    help_message = messenger.replies[-1][1].message
    assert help_message is not None
    assert help_message.author == "orca_auto"
    assert help_message.title == "Commands"

    application.dispatch_command(_command("bogus"), messenger=messenger)
    error = messenger.replies[-1][1].message
    assert error is not None
    assert error.severity == "error"
    assert error.title == "Unknown command"


def test_oversized_list_table_falls_back_to_plain_pagination(monkeypatch: Any) -> None:
    application = _application(ActivityFixture())
    messenger = FakeMessenger()

    monkeypatch.setattr(application, "_list_text", lambda *_a, **_k: "row\n" * 2000)
    application.dispatch_command(_command("list"), messenger=messenger)
    reply = messenger.replies[-1][1]
    # A table too large for an embed description degrades to the paginated plain
    # path (no embed) so every row is delivered instead of truncated.
    assert reply.message is None
    assert reply.format == "preformatted"
    assert reply.text.count("row") == 2000


def test_help_uses_telegram_command_prefix() -> None:
    application = _application(ActivityFixture())
    messenger = FakeMessenger()
    messenger.provider = "telegram"
    command = replace(
        _command("help"),
        address=ConversationAddress(provider="telegram", channel_id="100"),
    )

    assert application.dispatch_command(command, messenger=messenger) == "help-sent"
    help_text = messenger.replies[-1][1].text
    assert "/list" in help_text and "/cancel TARGET" in help_text


def test_list_cancel_button_still_requires_a_second_confirmation() -> None:
    fixture = ActivityFixture()
    application = _application(fixture)
    messenger = FakeMessenger()
    application.dispatch_command(_command("list"), messenger=messenger)
    cancel_prompt = next(
        action
        for row in messenger.replies[-1][1].actions
        for action in row
        if action.label.endswith("running-job")
    )

    assert (
        application.dispatch_action(_incoming_action(cancel_prompt.action_id), messenger=messenger)
        == "cancel-confirmation-sent"
    )
    assert fixture.cancelled == []
    confirm = _find_action(messenger.replies[-1][1], "Yes, cancel")
    assert (
        application.dispatch_action(
            _incoming_action(confirm.action_id, message_id="message-2"), messenger=messenger
        )
        == "cancel-processed"
    )
    assert fixture.cancelled == ["run-1"]


def test_long_cancel_target_is_opaque_and_always_requires_confirmation() -> None:
    fixture = ActivityFixture()
    target = "workflow-" + "x" * 400
    fixture.activities[0]["activity_id"] = target
    fixture.activities[0]["label"] = "long-running-workflow"
    application = _application(fixture)
    messenger = FakeMessenger()

    status = application.dispatch_command(_command("cancel", target), messenger=messenger)

    assert status == "cancel-confirmation-sent"
    assert fixture.cancelled == []
    reply = messenger.replies[-1][1]
    confirm = _find_action(reply, "Yes, cancel")
    assert target not in confirm.action_id
    assert len(confirm.action_id.encode("utf-8")) <= 64

    assert (
        application.dispatch_action(_incoming_action(confirm.action_id), messenger=messenger)
        == "cancel-processed"
    )
    assert fixture.cancelled == [target]
    assert messenger.edits[-1] == (ADDRESS, "message-1", None)


def test_cancel_confirmation_rejects_reused_activity_generation() -> None:
    fixture = ActivityFixture()
    fixture.activities[0]["submitted_at"] = "2026-01-01T00:00:00Z"
    application = _application(fixture)
    messenger = FakeMessenger()
    application.dispatch_command(_command("cancel", "run-1"), messenger=messenger)
    confirm = _find_action(messenger.replies[-1][1], "Yes, cancel")

    fixture.activities[0]["submitted_at"] = "2026-01-02T00:00:00Z"
    reply_count = len(messenger.replies)
    assert (
        application.dispatch_action(_incoming_action(confirm.action_id), messenger=messenger)
        == "cancel-processed"
    )

    assert fixture.cancelled == []
    new_replies = [reply.text for _address, reply in messenger.replies[reply_count:]]
    assert any("changed" in text for text in new_replies)


def test_filtered_list_actions_and_refresh_preserve_the_filter() -> None:
    fixture = ActivityFixture()
    application = _application(fixture)
    messenger = FakeMessenger()

    application.dispatch_command(_command("list", "failed"), messenger=messenger)
    first = messenger.replies[-1][1]
    assert "failed-job" in first.text and "running-job" not in first.text
    assert not any(action.label.startswith("Cancel ") for row in first.actions for action in row)
    refresh = _find_action(first, "Refresh")

    application.dispatch_action(_incoming_action(refresh.action_id), messenger=messenger)
    refreshed = messenger.replies[-1][1]
    assert "failed-job" in refreshed.text and "running-job" not in refreshed.text
    assert not any(
        action.label.startswith("Cancel ") for row in refreshed.actions for action in row
    )


def test_filtered_list_buttons_include_matching_workflow_children() -> None:
    fixture = ActivityFixture()
    fixture.activities = [
        {
            "activity_id": "wf-1",
            "label": "parent",
            "status": "running",
            "kind": "workflow",
            "engine": "workflow",
            "source": "orca_auto_flow",
        },
        {
            "activity_id": "xtb-child-1",
            "label": "queued-child",
            "status": "queued",
            "kind": "job",
            "engine": "xtb",
            "source": "orca_auto_xtb",
            "parent_workflow_id": "wf-1",
        },
    ]
    deps = replace(
        _deps(fixture),
        queue_list_default_visible_items=lambda items: [
            item for item in items if not item.get("parent_workflow_id")
        ],
    )
    application = BotApplication(settings=_settings(), deps=deps)
    messenger = FakeMessenger()

    application.dispatch_command(_command("list", "queued"), messenger=messenger)

    reply = messenger.replies[-1][1]
    assert "queued-child" in reply.text
    assert any(action.label.endswith("queued-child") for row in reply.actions for action in row)


def test_cancel_and_clear_actions_refresh_the_list() -> None:
    fixture = ActivityFixture()
    application = _application(fixture)
    messenger = FakeMessenger()

    application.dispatch_command(_command("list"), messenger=messenger)
    clear = _find_action(messenger.replies[-1][1], "Clear finished")
    before_clear = len(messenger.replies)
    application.dispatch_action(_incoming_action(clear.action_id), messenger=messenger)
    clear_replies = [reply for _address, reply in messenger.replies[before_clear:]]
    assert fixture.clear_count == 1
    assert clear_replies[0].text == "cleared: 3"
    assert clear_replies[0].format == "preformatted"
    assert clear_replies[0].message is not None
    assert clear_replies[0].message.title == "Cleared"
    assert clear_replies[-1].actions

    application.dispatch_command(_command("cancel", "run-1"), messenger=messenger)
    confirm = _find_action(messenger.replies[-1][1], "Yes, cancel")
    before_cancel = len(messenger.replies)
    application.dispatch_action(_incoming_action(confirm.action_id), messenger=messenger)
    cancel_replies = [reply for _address, reply in messenger.replies[before_cancel:]]
    assert fixture.cancelled == ["run-1"]
    assert cancel_replies[-1].actions


def test_cancel_dismiss_never_calls_cancel() -> None:
    fixture = ActivityFixture()
    application = _application(fixture)
    messenger = FakeMessenger()
    application.dispatch_command(_command("cancel", "run-1"), messenger=messenger)
    dismiss = _find_action(messenger.replies[-1][1], "Keep running")

    assert (
        application.dispatch_action(_incoming_action(dismiss.action_id), messenger=messenger)
        == "cancel-dismissed"
    )
    assert fixture.cancelled == []
    assert messenger.acks[-1][1] == "Cancellation dismissed."


def test_unknown_and_expired_actions_are_acknowledged_without_side_effects() -> None:
    fixture = ActivityFixture()
    now = [10.0]
    tokens = iter(("confirm", "dismiss"))
    registry = ActionRegistry(
        ttl_seconds=5,
        clock=lambda: now[0],
        token_factory=lambda: next(tokens),
    )
    application = _application(fixture, registry=registry)
    messenger = FakeMessenger()

    assert (
        application.dispatch_action(_incoming_action("oa:missing"), messenger=messenger)
        == "unknown"
    )
    assert "unavailable" in messenger.acks[-1][1]

    application.dispatch_command(_command("cancel", "run-1"), messenger=messenger)
    confirm = _find_action(messenger.replies[-1][1], "Yes, cancel")
    now[0] = 16.0
    assert (
        application.dispatch_action(_incoming_action(confirm.action_id), messenger=messenger)
        == "expired"
    )
    assert "expired" in messenger.acks[-1][1].lower()
    assert fixture.cancelled == []


def test_duplicate_confirmation_is_one_time() -> None:
    fixture = ActivityFixture()
    application = _application(fixture)
    messenger = FakeMessenger()
    application.dispatch_command(_command("cancel", "run-1"), messenger=messenger)
    confirm = _find_action(messenger.replies[-1][1], "Yes, cancel")
    incoming = _incoming_action(confirm.action_id)

    assert application.dispatch_action(incoming, messenger=messenger) == "cancel-processed"
    assert application.dispatch_action(incoming, messenger=messenger) == "unknown"
    assert fixture.cancelled == ["run-1"]
    assert "already used" in messenger.acks[-1][1]


def test_wrong_conversation_does_not_consume_confirmation() -> None:
    fixture = ActivityFixture()
    application = _application(fixture)
    messenger = FakeMessenger()
    application.dispatch_command(_command("cancel", "run-1"), messenger=messenger)
    confirm = _find_action(messenger.replies[-1][1], "Yes, cancel")

    assert (
        application.dispatch_action(
            _incoming_action(confirm.action_id, address=OTHER_ADDRESS),
            messenger=messenger,
        )
        == "wrong_address"
    )
    assert fixture.cancelled == []
    assert (
        application.dispatch_action(_incoming_action(confirm.action_id), messenger=messenger)
        == "cancel-processed"
    )
    assert fixture.cancelled == ["run-1"]


def test_wrong_actor_does_not_consume_confirmation() -> None:
    fixture = ActivityFixture()
    application = _application(fixture)
    messenger = FakeMessenger()
    application.dispatch_command(_command("cancel", "run-1"), messenger=messenger)
    confirm = _find_action(messenger.replies[-1][1], "Yes, cancel")

    assert (
        application.dispatch_action(
            _incoming_action(confirm.action_id, actor=OTHER_ACTOR), messenger=messenger
        )
        == "wrong_actor"
    )
    assert fixture.cancelled == []
    assert (
        application.dispatch_action(_incoming_action(confirm.action_id), messenger=messenger)
        == "cancel-processed"
    )
    assert fixture.cancelled == ["run-1"]


def test_authorized_operator_action_audience_is_not_bound_to_originator() -> None:
    registry = ActionRegistry(token_factory=lambda: "operator-action")
    action_id = registry.issue(
        "list_refresh",
        address=ADDRESS,
        actor=Actor(user_id="", label="scheduled-notification"),
        audience="authorized_operator",
    )

    resolution = registry.consume(action_id, address=ADDRESS, actor=OTHER_ACTOR)

    assert resolution.status == "ok"
    assert resolution.action is not None
    assert resolution.action.audience == "authorized_operator"


def test_registry_is_bounded_and_evicts_complete_action_groups() -> None:
    tokens = iter(("a", "b", "c", "d", "e", "f"))
    registry = ActionRegistry(max_entries=2, token_factory=lambda: next(tokens))
    first = registry.issue_group(
        (("cancel_confirm", "one"), ("cancel_dismiss", "one")),
        address=ADDRESS,
        actor=ACTOR,
    )
    second = registry.issue_group(
        (("cancel_confirm", "two"), ("cancel_dismiss", "two")),
        address=ADDRESS,
        actor=ACTOR,
    )

    assert registry.pending_count == 2
    assert registry.consume(first[0], address=ADDRESS, actor=ACTOR).status == "unknown"
    assert registry.consume(second[0], address=ADDRESS, actor=ACTOR).status == "ok"


def test_settings_and_activity_paths_are_forwarded_to_cancel() -> None:
    captured: dict[str, Any] = {}

    def cancel(*, target: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"target": target, **kwargs})
        return {"activity_id": target, "status": "cancelled"}

    fixture = ActivityFixture()
    deps = _deps(fixture)
    deps = replace(deps, cancel_activity=cancel)
    application = BotApplication(settings=_settings(), deps=deps)
    messenger = FakeMessenger()
    application.dispatch_command(_command("cancel", "run-1"), messenger=messenger)
    confirm = _find_action(messenger.replies[-1][1], "Yes, cancel")
    application.dispatch_action(_incoming_action(confirm.action_id), messenger=messenger)

    assert captured == {
        "target": "run-1",
        "workflow_root": "/runs",
        "crest_config": "/config/orca_auto.yaml",
        "xtb_config": "/config/orca_auto.yaml",
        "orca_config": "/config/orca_auto.yaml",
        "orca_repo_root": "/repo",
    }


def test_settings_from_config_resolves_shared_paths_without_provider_credentials() -> None:
    activity_sources = SimpleNamespace(
        discover_shared_config=lambda explicit: explicit or "/shared/orca_auto.yaml",
        discover_workflow_root=lambda _explicit: "/fallback-runs",
    )

    settings = settings_from_config(
        "/explicit/orca_auto.yaml",
        activity_sources=activity_sources,
        getenv=lambda name, default="": "/repo" if name.endswith("REPO_ROOT") else default,
        workflow_root_from_config=lambda config: (
            "/configured-runs" if config == "/explicit/orca_auto.yaml" else None
        ),
    )

    assert settings == BotSettings(
        workflow_root="/configured-runs",
        crest_config="/explicit/orca_auto.yaml",
        xtb_config="/explicit/orca_auto.yaml",
        orca_config="/explicit/orca_auto.yaml",
        orca_repo_root="/repo",
        runs_root="/configured-runs",
    )
