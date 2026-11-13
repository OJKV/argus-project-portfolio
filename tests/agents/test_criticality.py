"""What happens when an agent cannot answer (owner, 2026-09-20).

Three outcomes, not two. "Optional" is not the opposite of "critical": an agent whose job is to
*object* must not be skipped when it fails, because skipping it silently converts a safety check
into an approval. Trading continues where it safely can, and anything else reaches Telegram.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from argus.journal.db import open_journal
from argus.llm.router import Criticality, agent_policy
from argus.notify.telegram import Notifier, RecordingBot
from argus.observability import alerts

AGENTS = (
    "regime",
    "catalyst",
    "analyst",
    "redteam",
    "riskdebate",
    "trader",
    "consensus",
    "reviewer",
    "postmortem",
    "reflector",
)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def test_every_agent_declares_what_its_failure_means() -> None:
    for agent in AGENTS:
        assert agent_policy(agent).criticality in tuple(Criticality)


def test_the_agents_that_produce_the_signal_are_required() -> None:
    for agent in ("regime", "analyst", "trader"):
        assert agent_policy(agent).criticality is Criticality.REQUIRED, agent


def test_the_agents_that_object_fail_closed() -> None:
    """A Red Team that cannot run has not approved the trade — it has not looked at it."""
    for agent in ("redteam", "consensus", "riskdebate"):
        assert agent_policy(agent).criticality is Criticality.SAFETY, agent


def test_the_agents_that_only_learn_are_optional() -> None:
    for agent in ("postmortem", "reflector", "catalyst"):
        assert agent_policy(agent).criticality is Criticality.OPTIONAL, agent


def test_a_required_agent_failing_is_a_critical_alert() -> None:
    alert = alerts.agent_failed("trader", Criticality.REQUIRED, "BTC", "all 4 models failed")
    assert alert is not None
    assert alert.severity == "critical"
    assert "BTC" in alert.message and "trader" in alert.message


def test_a_safety_agent_failing_alerts_and_says_the_trade_was_dropped() -> None:
    alert = alerts.agent_failed("consensus", Criticality.SAFETY, "ETH", "timeout")
    assert alert is not None
    assert alert.severity == "critical"
    assert "not traded" in alert.message.lower()


def test_an_optional_agent_failing_does_not_wake_anyone() -> None:
    assert alerts.agent_failed("postmortem", Criticality.OPTIONAL, "SOL", "timeout") is None


def test_the_alert_reaches_telegram() -> None:
    bot = RecordingBot()
    alerts.dispatch(
        [alerts.agent_failed("trader", Criticality.REQUIRED, "BTC", "boom")],
        notifier=Notifier(bot=bot, chat_id="42"),
    )
    assert bot.sent and "trader" in bot.sent[0][1]


def test_a_failure_never_carries_the_provider_error_verbatim() -> None:
    """Provider errors quote the request, and a Telegram alert is not the place to find out
    what a URL contained (F-070)."""
    alert = alerts.agent_failed(
        "trader", Criticality.REQUIRED, "BTC", "401 from https://x/api?api_key=SECRET123"
    )
    assert alert is not None
    assert "SECRET123" not in alert.message


def test_a_dead_trader_alerts_once_per_asset_and_does_not_stop_the_run(engine: Engine) -> None:
    """The run must survive one asset's agent dying: fourteen good assets are not worth losing
    to the fifteenth. But each loss is announced — a silently shrinking universe is how a broken
    provider looks exactly like a quiet market."""
    from argus.llm.router import AllModelsFailedError, Criticality

    bot = RecordingBot()
    notifier = Notifier(bot=bot, chat_id="42")
    for symbol in ("BTC", "ETH"):
        exc = AllModelsFailedError(f"trader: all 4 models failed on {symbol}")
        alerts.dispatch(
            [alerts.agent_failed("trader", Criticality.REQUIRED, symbol, str(exc))],
            notifier=notifier,
        )

    assert len(bot.sent) == 2
    assert "BTC" in bot.sent[0][1] and "ETH" in bot.sent[1][1]
    assert all("no signal for this asset" in text for _, text in bot.sent)
