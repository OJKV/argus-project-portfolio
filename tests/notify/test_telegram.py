"""Telegram command routing and alert degradation (ARCHITECTURE.md §9, RUNBOOK security)."""

from __future__ import annotations

from argus.config import Settings
from argus.notify.telegram import CommandRouter, Notifier, RecordingBot, build_router

OWNER = "424242"


def _router() -> CommandRouter:
    return build_router(
        OWNER,
        status=lambda _: "status ok",
        positions=lambda _: "no positions",
        pnl=lambda args: f"pnl {args[0] if args else 'today'}",
        halt=lambda _: "halted",
        resume=lambda _: "resumed",
        close=lambda args: f"closing {args[0]}",
    )


def test_a_stranger_cannot_drive_the_bot() -> None:
    # The whole control surface is guarded by one check; it gets its own test.
    assert _router().dispatch("999", "/halt", from_id="999") == "unauthorised"
    assert _router().dispatch("999", "/close BTC", from_id="999") == "unauthorised"


def test_owner_commands_dispatch_with_arguments() -> None:
    router = _router()
    assert router.dispatch(OWNER, "/status", from_id=OWNER) == "status ok"
    assert router.dispatch(OWNER, "/pnl week", from_id=OWNER) == "pnl week"
    assert router.dispatch(OWNER, "/close BTC-USDT-SWAP", from_id=OWNER) == "closing BTC-USDT-SWAP"


def test_group_style_command_suffix_is_tolerated() -> None:
    assert _router().dispatch(OWNER, "/status@argus_bot", from_id=OWNER) == "status ok"


def test_unknown_and_malformed_input_is_reported_not_raised() -> None:
    router = _router()
    assert router.dispatch(OWNER, "/nope", from_id=OWNER) == "unknown command /nope"
    assert router.dispatch(OWNER, "hello", from_id=OWNER) == "not a command"
    assert router.dispatch(OWNER, "", from_id=OWNER) == "not a command"


def test_a_failing_handler_does_not_kill_the_bot() -> None:
    router = CommandRouter(chat_id=OWNER)

    def boom(_: list[str]) -> str:
        raise RuntimeError("db is gone")

    router.register("status", boom)
    assert router.dispatch(OWNER, "/status", from_id=OWNER) == "/status failed: RuntimeError"


def test_alerts_degrade_to_logs_when_no_token_is_configured() -> None:
    notifier = Notifier.from_settings(Settings(mode="demo"))
    assert notifier.bot is None
    assert notifier.send("anything") is False  # reported as not delivered, never raised


def test_a_broken_transport_never_breaks_the_caller() -> None:
    class Broken:
        def send(self, chat_id: str, text: str) -> None:
            raise ConnectionError("telegram down")

    assert Notifier(bot=Broken(), chat_id=OWNER).send("hi") is False


def test_alert_vocabulary_reaches_the_bot() -> None:
    bot = RecordingBot()
    notifier = Notifier(bot=bot, chat_id=OWNER)
    notifier.unprotected_repaired("BTC-USDT-SWAP", "70000", "plan")
    notifier.drawdown_halt("16.2", "838", "1000")
    assert len(bot.sent) == 2
    assert all(chat == OWNER for chat, _ in bot.sent)


def test_a_group_member_who_is_not_the_owner_is_refused() -> None:
    """In a group, chat.id is the group: every member passed the old check (review O-2)."""
    group = "-100777"
    router = build_router(
        group,
        owner_id=OWNER,
        status=lambda _: "status ok",
        positions=lambda _: "-",
        pnl=lambda _: "-",
        halt=lambda _: "halted",
        resume=lambda _: "resumed",
        close=lambda _: "closing",
    )
    assert router.dispatch(group, "/resume", from_id="31337") == "unauthorised"
    assert router.dispatch(group, "/close BTC", from_id="31337") == "unauthorised"
    assert router.dispatch(group, "/status", from_id=OWNER) == "status ok"


def test_without_an_owner_id_only_the_private_chat_owner_is_obeyed() -> None:
    """A private chat's id IS the user's id, so that is the safe default."""
    assert _router().dispatch(OWNER, "/halt", from_id="31337") == "unauthorised"
    assert _router().dispatch(OWNER, "/halt", from_id=OWNER) == "halted"
