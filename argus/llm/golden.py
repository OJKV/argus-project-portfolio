"""Golden dossiers: fixed inputs with known-correct answers (LLM_AGENTS.md §6).

Every model faces the identical question, so a difference in the answers is a difference in the
models rather than in the market. Without this, "which model should be the Red Team?" is settled
by taste, and a model that quietly fabricates evidence looks exactly like one that does not.

Each case names *why* its expected answer is right. An unarguable expectation is a bad test: if a
model disagrees and the reason is defensible, the case is wrong, not the model.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from argus.data.envelope import Observation
from argus.features.dossier import Dossier, assemble
from argus.features.regime import RegimeInputs, RegimeView, classify
from argus.llm.agents.catalyst import evidence_block, filter_sourced
from argus.llm.agents.postmortem import outcome_bias

# The scorer's citation matching now also guards the live chain (L-10); it lives there so the
# measurement and the check cannot drift apart.
from argus.llm.evidence import evidence_paths
from argus.llm.evidence import leaf as _leaf
from argus.llm.schemas import FactorScores, Target, TradePlan, TraderAnswer

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


class Expectation(StrEnum):
    """What a correct answer looks like. Trader verbs and Reviewer verbs in one vocabulary, so a
    case can say what it expects without the harness needing to know which agent it belongs to."""

    TRADE = "trade"
    NO_TRADE = "no_trade"
    HOLD = "HOLD"
    CLOSE = "CLOSE"
    TRIM = "TRIM"
    MOVE_STOP = "MOVE_STOP"
    AGREE = "agree"
    OBJECT = "object"
    NORMAL = "normal"
    REDUCE_RISK = "reduce_risk"
    STAND_ASIDE = "stand_aside"
    PROCEED = "proceed"
    REDUCE = "reduce"
    DECLINE = "decline"
    EXTRACT = "extract"
    NOTHING = "nothing"
    SOUND = "sound"
    FLAWED = "flawed"
    # Consensus verdicts, matching `agents.consensus.Agreement` so a case names the outcome
    # `compare()` actually produces rather than a parallel vocabulary that could drift from it.
    ONE_SIDED = "one_sided"
    CONFLICT = "conflict"
    DIVERGENT = "divergent"


def _regime(trend: str = "up") -> RegimeView:
    if trend == "up":
        inputs = RegimeInputs(
            btc_close=100,
            btc_ema20=95,
            btc_ema50=90,
            btc_ema20_slope=0.5,
            btc_rv20_pct=40,
            breadth_20=62,
            next_event_hours=96,
        )
    elif trend == "down":
        inputs = RegimeInputs(
            btc_close=88,
            btc_ema20=94,
            btc_ema50=99,
            btc_ema20_slope=-0.7,
            btc_rv20_pct=55,
            breadth_20=28,
            next_event_hours=96,
        )
    else:
        inputs = RegimeInputs(
            btc_close=100,
            btc_ema20=100.5,
            btc_ema50=99.8,
            btc_ema20_slope=0.01,
            btc_rv20_pct=25,
            breadth_20=48,
            next_event_hours=96,
        )
    return classify(inputs)


def _cross_market(
    fear_greed: int, mcap_rank: int, mcap_usd: float, **extra: Any
) -> list[dict[str, Any]]:
    """The cross-market block every real dossier carries, one entry per data source.

    `equities` renders as-is, so it is where sources that are neither price, derivatives nor
    chain data land: CoinMarketCap fundamentals, the Fear & Greed index, mempool fee pressure,
    prediction-market odds and ETF/macro prints. A fixture without them is easier to reason about
    and less like production, which is the wrong trade for a golden set.
    """
    block: list[dict[str, Any]] = [
        {"src": "cmc", "metric": "market_cap_usd", "value": mcap_usd, "rank": mcap_rank},
        {
            "src": "fear_greed",
            "metric": "index",
            "value": fear_greed,
            "label": "greed" if fear_greed >= 60 else "fear" if fear_greed <= 40 else "neutral",
        },
    ]
    for name, value in extra.items():
        block.append({"src": name.split("__")[0], "metric": name.split("__")[-1], "value": value})
    return block


def _dossier(symbol: str, regime: RegimeView, **families: Any) -> Dossier:
    return assemble(
        symbol=symbol,
        source="core",
        now=NOW,
        regime=regime,
        provenance=[Observation(source="okx.public.ticker", tier=1, asof=NOW)],
        **families,
    )


@dataclass(frozen=True)
class GoldenCase:
    name: str
    expect: Expectation
    why: str
    dossier: Callable[[], Dossier]
    agent: str = "trader"
    # Extra prompt inputs for agents that need more than a dossier — the Reviewer needs the
    # position and the plan it is judging.
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Score:
    case: str
    correct: bool
    invented_evidence: bool
    detail: str = ""


def score_answer(case: GoldenCase, answer: TraderAnswer) -> Score:
    """Did it decide correctly, and did it cite things that exist?"""
    correct = answer.decision == case.expect.value
    invented = False
    if answer.plan is not None:
        known = evidence_paths(case.dossier())
        cited = [p for paths in answer.plan.evidence.values() for p in paths]
        # Match on the LEAF key. Models quote values alongside paths ("tech.px=100.0"), which
        # is helpful and must not be punished — but a prefix match would accept
        # "technical.rsi_from_thin_air" just because "technical" is real, which is exactly
        # the fabrication this is meant to catch.
        invented = any(_leaf(path) not in known for path in cited)
    return Score(case=case.name, correct=correct, invented_evidence=invented)


CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        name="clean_uptrend",
        expect=Expectation.TRADE,
        why="aligned trend with every family present, room to the 20-period high, a structural "
        "stop below the 20-low, flat funding and no event — if this is not tradeable, nothing "
        "is. Built COMPLETE on purpose (2026-09-20): the first version left the EMAs null and "
        "onchain missing, and five models from four families all refused it for that reason. "
        "They were right and the case was wrong — a control that cannot be traded proves "
        "nothing about the gate. Prices are realistic too: one model called a round 100.0 "
        '"clearly a placeholder" and refused on that alone.',
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 106_150.0,
                "atr_4h_pct": 2.2,
                "high_20_4h": 113_400.0,
                "high_50_4h": 121_000.0,
                "low_20_4h": 102_400.0,
                "spread_pct": 0.01,
                "rsi_14_4h": 58.0,
                # Key names must match `_ema_block` in features/dossier.py: the first attempt
                # used "20"/"slope_pct", rendered every level as null, and earned a refusal.
                "ema_4h": {
                    "ema20": 108_400.0,
                    "ema50": 104_900.0,
                    "ema200": 96_000.0,
                    "slope20": 0.6,
                    "order": "bull",
                },
                "ema_1d": {"ema20": 106_800.0, "ema50": 101_500.0, "order": "bull"},
            },
            derivatives={
                "funding_8h": 0.0001,
                "oi_24h_pct": 3.0,
                "basis_pct": 0.05,
            },
            onchain={"exchange_netflow_7d_usd": -120_000_000, "active_addresses_7d_pct": 4.0},
            news=[
                {
                    "title": "Spot ETF inflows continue for a fourth week",
                    "published": "2026-09-19T08:00:00Z",
                    "source": "coindesk",
                    "tag": "factual",
                }
            ],
            catalysts=[
                {
                    "when": "2026-10-02",
                    "what": "quarterly options expiry",
                    "why_it_matters": "positioning unwind",
                }
            ],
        ),
    ),
    GoldenCase(
        name="chop_no_edge",
        expect=Expectation.NO_TRADE,
        why="price pinned between a near high and low with no trend: any stop is either inside "
        "the noise or further than the target",
        dossier=lambda: _dossier(
            "SOL",
            _regime("chop"),
            technical={
                "price": 100.0,
                "atr_4h_pct": 3.4,
                "high_20_4h": 101.5,
                "low_20_4h": 98.5,
                "ema_4h": {"order": "mixed"},
            },
            derivatives={"funding_8h": 0.0},
        ),
    ),
    GoldenCase(
        name="stale_data",
        expect=Expectation.NO_TRADE,
        why="the only price is hours old and marked stale; trading on it is trading blind",
        dossier=lambda: _dossier(
            "ARB",
            _regime("up"),
            technical={"price": 0.50, "atr_4h_pct": None, "stale": True},
        ),
    ),
    GoldenCase(
        name="counter_regime_short",
        expect=Expectation.NO_TRADE,
        why="a short into a confirmed uptrend needs an exceptional case, and nothing here is "
        "exceptional",
        dossier=lambda: _dossier(
            "ETH",
            _regime("up"),
            technical={
                "price": 100.0,
                "atr_4h_pct": 2.0,
                "high_20_4h": 103.0,
                "low_20_4h": 88.0,
                "ema_4h": {"order": "bull"},
            },
            derivatives={"funding_8h": 0.0002},
        ),
    ),
    GoldenCase(
        name="already_extended",
        expect=Expectation.NO_TRADE,
        why="price is at the 20-period high with the next resistance unknown: the target would "
        "be invented, and the stop is a full range away",
        dossier=lambda: _dossier(
            "DOGE",
            _regime("up"),
            technical={
                "price": 0.2295,
                "atr_4h_pct": 4.5,
                "high_20_4h": 0.2301,
                "low_20_4h": 0.1900,
                "ema_4h": {"order": "bull"},
            },
            derivatives={"funding_8h": 0.0009},
        ),
    ),
    GoldenCase(
        name="wide_spread",
        expect=Expectation.NO_TRADE,
        why="the spread eats a meaningful part of the expected move before the trade even starts",
        dossier=lambda: _dossier(
            "ENA",
            _regime("up"),
            technical={
                "price": 1.00,
                "atr_4h_pct": 1.2,
                "high_20_4h": 1.04,
                "low_20_4h": 0.97,
                "spread_pct": 0.45,
                "ema_4h": {"order": "bull"},
            },
        ),
    ),
    GoldenCase(
        name="breakout_retest",
        expect=Expectation.TRADE,
        why="price broke the 20-period high and came back to it; the broken level is the stop "
        "reference and the 50-period high is the first target. Risk ~3.5%, first target ~7.5% "
        "— comfortably past the 1.5R bar, which is what makes this a control and not a coin flip",
        dossier=lambda: _dossier(
            "SOL",
            _regime("up"),
            technical={
                "price": 214.40,
                "atr_4h_pct": 2.6,
                "high_20_4h": 216.00,
                # The broken level, now support — a retest stop sits here, not at the old
                # range low, which was 4.9x ATR away and outside the gate band.
                "low_20_4h": 206.50,
                "high_50_4h": 238.00,
                "vwap_1d": 211.80,
                "rsi_4h": 61.0,
                "rel_vol_4h": 1.6,
                "ret_7d": 8.4,
                "spread_pct": 0.012,
                "ema_4h": {
                    "ema20": 208.10,
                    "ema50": 199.40,
                    "ema200": 181.00,
                    "slope20": 0.9,
                    "order": "bull",
                },
                "ema_1d": {"ema20": 203.70, "ema50": 192.20, "order": "bull"},
            },
            # Coinalyze: funding flat while open interest builds — expansion, not a crowded long.
            derivatives={
                "funding_8h": 0.00008,
                "funding_avg_7d": 0.00009,
                "oi_24h_pct": 6.1,
                "oi_7d_pct": 14.0,
                "ls_ratio": 1.05,
                "basis_pct": 0.07,
            },
            # DefiLlama + Etherscan: chain activity confirms the move instead of lagging it.
            onchain={
                "tvl_usd": 9_400_000_000,
                "tvl_7d_pct": 5.2,
                "fees_24h_usd": 2_100_000,
                "fees_7d_pct": 11.0,
            },
            equities=_cross_market(
                fear_greed=66,
                mcap_rank=5,
                mcap_usd=118_000_000_000,
                kalshi__odds_sol_above_250_by_oct31=0.29,
                fred__policy_rate_pct=3.75,
            ),
            news=[
                {
                    "title": "Solana network fees hit a three-month high as activity rebounds",
                    "published": "2026-09-19T14:10:00Z",
                    "source": "theblock",
                    "tag": "factual",
                }
            ],
            catalysts=[
                {
                    "when": "2026-10-08",
                    "what": "protocol upgrade on mainnet",
                    "why_it_matters": "throughput increase, outside the time stop",
                }
            ],
        ),
    ),
    GoldenCase(
        name="downtrend_short",
        expect=Expectation.TRADE,
        why="a short WITH the regime, not against it: price failed at the 20-period high in a "
        "confirmed downtrend, funding is positive (longs paying to be wrong), stop above the "
        "failed level and the 50-period low is the target. The counter-regime bar does not apply",
        dossier=lambda: _dossier(
            "ETH",
            _regime("down"),
            technical={
                "price": 2_480.0,
                "atr_4h_pct": 3.1,
                "high_20_4h": 2_570.0,
                "low_20_4h": 2_180.0,
                "low_50_4h": 2_050.0,
                "rsi_4h": 41.0,
                "rel_vol_4h": 1.2,
                "ret_7d": -9.2,
                "spread_pct": 0.01,
                "ema_4h": {
                    "ema20": 2_555.0,
                    "ema50": 2_690.0,
                    "ema200": 2_880.0,
                    "slope20": -0.8,
                    "order": "bear",
                },
                "ema_1d": {"ema20": 2_640.0, "ema50": 2_810.0, "order": "bear"},
            },
            # Positive funding in a downtrend: the crowd is still long and paying for it.
            derivatives={
                "funding_8h": 0.00031,
                "funding_avg_7d": 0.00028,
                "funding_pctile_30d": 86.0,
                "oi_24h_pct": 2.2,
                "ls_ratio": 1.38,
                "basis_pct": -0.04,
            },
            onchain={"tvl_usd": 48_000_000_000, "tvl_7d_pct": -6.4, "fees_24h_usd": 3_400_000},
            # Farside: ETF outflows, the equities side of the same story.
            equities=_cross_market(
                fear_greed=31,
                mcap_rank=2,
                mcap_usd=298_000_000_000,
                farside__etf_5d_net_flow_usd=-184_000_000,
                fred__policy_rate_pct=3.75,
                polymarket__odds_eth_below_2000_by_oct31=0.38,
            ),
            news=[
                {
                    "title": "Ether ETFs post a fourth straight week of outflows",
                    "published": "2026-09-19T09:30:00Z",
                    "source": "coindesk",
                    "tag": "factual",
                }
            ],
        ),
    ),
    GoldenCase(
        name="funding_reset_long",
        expect=Expectation.TRADE,
        why="a washout that reset positioning: funding negative and long/short under 1 after a "
        "flush, price back above the 4h EMA20 in an uptrend, open interest down 11% (leverage "
        "flushed). Stop under the flush low, first target the prior range high — about 3R",
        dossier=lambda: _dossier(
            "ARB",
            _regime("up"),
            technical={
                "price": 0.8420,
                "atr_4h_pct": 4.2,
                "high_20_4h": 0.9650,
                "low_20_4h": 0.7900,
                "rsi_4h": 47.0,
                "rel_vol_4h": 2.1,
                "ret_7d": -4.1,
                "spread_pct": 0.02,
                "ema_4h": {
                    "ema20": 0.8330,
                    "ema50": 0.8510,
                    "ema200": 0.8100,
                    "slope20": 0.2,
                    "order": "mixed",
                },
                "ema_1d": {"ema20": 0.8240, "ema50": 0.7980, "order": "bull"},
            },
            # Coinalyze: funding went negative after the flush — shorts now pay longs.
            derivatives={
                "funding_8h": -0.00019,
                "funding_avg_7d": -0.00004,
                "funding_pctile_30d": 7.0,
                "oi_24h_pct": -11.5,
                "ls_ratio": 0.88,
                "taker_ratio": 0.94,
            },
            # DefiLlama unlock schedule: far enough out to ignore, but stated rather than absent.
            onchain={
                "tvl_usd": 2_600_000_000,
                "tvl_7d_pct": 1.1,
                "fees_24h_usd": 310_000,
                "unlock_in_days": 74,
                "unlock_pct_of_unlocked": 1.9,
            },
            equities=_cross_market(
                fear_greed=38,
                mcap_rank=42,
                mcap_usd=3_900_000_000,
                fred__policy_rate_pct=3.75,
            ),
            news=[
                {
                    "title": "Arbitrum DAO approves incentive programme for perp DEXs",
                    "published": "2026-09-18T16:00:00Z",
                    "source": "decrypt",
                    "tag": "factual",
                }
            ],
        ),
    ),
    GoldenCase(
        name="macro_tailwind_long",
        expect=Expectation.TRADE,
        why="the macro case a human would take: FRED shows the cut and a softening dollar, ETF "
        "flows are positive, BTC holds both daily EMAs after a shallow pullback. Stop below the "
        "20-period low (~2.8%), first target the prior high (~11%) — about 4R",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 104_800.0,
                "atr_4h_pct": 1.9,
                "high_20_4h": 116_200.0,
                "low_20_4h": 101_900.0,
                "high_50_4h": 121_000.0,
                "vwap_1d": 104_100.0,
                "rsi_4h": 55.0,
                "rel_vol_4h": 1.1,
                "ret_30d": 6.8,
                "spread_pct": 0.008,
                "ema_4h": {
                    "ema20": 103_900.0,
                    "ema50": 101_200.0,
                    "ema200": 94_500.0,
                    "slope20": 0.4,
                    "order": "bull",
                },
                "ema_1d": {"ema20": 102_600.0, "ema50": 97_400.0, "order": "bull"},
            },
            derivatives={
                "funding_8h": 0.00006,
                "funding_avg_7d": 0.00007,
                "oi_7d_pct": 4.4,
                "ls_ratio": 1.02,
                "basis_pct": 0.04,
            },
            # mempool.space chain health.
            onchain={"fees_24h_usd": 890_000, "fees_7d_pct": 3.0},
            # Farside ETF flows and the FRED macro read.
            equities=_cross_market(
                fear_greed=63,
                mcap_rank=1,
                mcap_usd=2_070_000_000_000,
                farside__etf_5d_net_flow_usd=412_000_000,
                fred__policy_rate_pct=3.75,
                fred__dollar_index_30d_pct=-1.8,
                mempool__fee_sat_vb=6,
                sec_edgar__institutional_13f_holders=1_284,
            ),
            news=[
                {
                    "title": "Fed cuts by 25bp, signals one more before year end",
                    "published": "2026-09-18T18:00:00Z",
                    "source": "reuters",
                    "tag": "factual",
                }
            ],
            catalysts=[
                {
                    "when": "2026-10-14",
                    "what": "US CPI release",
                    "why_it_matters": "next scheduled volatility event, outside the time stop",
                }
            ],
        ),
    ),
)


@dataclass
class ModelReport:
    """How one model did across the whole golden set, for one role."""

    label: str
    correct: int = 0
    invented: int = 0
    schema_fail: int = 0
    errors: int = 0
    total: int = 0
    latencies_ms: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def p50_ms(self) -> int:
        if not self.latencies_ms:
            return 0
        ordered = sorted(self.latencies_ms)
        return ordered[len(ordered) // 2]

    @property
    def usable(self) -> bool:
        """A model earns its place by answering in the right shape without inventing sources.

        Accuracy alone is a poor filter on six cases — a model that refuses everything scores
        well on a set where most answers are NO_TRADE. Shape and honesty are the hard gates;
        judgement is compared between models afterwards.
        """
        return self.errors + self.schema_fail <= self.total // 3 and self.invented == 0


def evaluate_model(ref: Any, cases: tuple[GoldenCase, ...] = CASES) -> ModelReport:
    """Put one model through the golden set directly — no routing, no fallback.

    Bypassing the router is the point: this measures the model, not the chain around it.
    """
    import time as clock

    from pydantic import ValidationError
    from pydantic_ai import Agent
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    from argus.llm.agents.trader import build_prompt
    from argus.llm.router import build_model

    report = ModelReport(label=ref.label)
    for case in cases:
        report.total += 1
        prompt = build_prompt(case.dossier())
        started = clock.perf_counter()
        try:
            model = build_model(ref)
            result = Agent(model, output_type=TraderAnswer).run_sync(
                prompt, model_settings={"temperature": 0.1, "max_tokens": 1500, "timeout": 60}
            )
        except (ValidationError, UnexpectedModelBehavior) as exc:
            report.schema_fail += 1
            report.notes.append(f"{case.name}: schema {type(exc).__name__}")
            continue
        except Exception as exc:  # noqa: BLE001 - a dead provider is a result, not a crash
            report.errors += 1
            report.notes.append(f"{case.name}: {type(exc).__name__}")
            continue
        report.latencies_ms.append(int((clock.perf_counter() - started) * 1000))
        score = score_answer(case, result.output)
        report.correct += int(score.correct)
        report.invented += int(score.invented_evidence)
        if not score.correct:
            report.notes.append(f"{case.name}: said {result.output.decision}")
    return report


# The daily screen: one clear trade, one clear refusal, one that needs the macro block. Three
# cases is 75 calls across the roster, which a free tier absorbs; the full ten is a weekly cost.
SCREEN_CASES: tuple[GoldenCase, ...] = tuple(
    c for c in CASES if c.name in ("clean_uptrend", "chop_no_edge", "macro_tailwind_long")
)


def record_eval(engine: Any, report: ModelReport, tier: str) -> str:
    """Persist one model's golden-set result. Returns the row id."""
    from sqlalchemy import text

    from argus.journal.db import new_id, utc_now

    provider, _, model = report.label.partition("/")
    eval_id = new_id()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO model_evals (eval_id, created_at, provider, model, tier, cases,"
                " correct, invented, schema_fail, errors, p50_ms, usable, notes)"
                " VALUES (:i, :t, :p, :m, :tier, :c, :ok, :inv, :sch, :err, :p50, :use, :n)"
            ),
            {
                "i": eval_id,
                "t": utc_now(),
                "p": provider,
                "m": model,
                "tier": tier,
                "c": report.total,
                "ok": report.correct,
                "inv": report.invented,
                "sch": report.schema_fail,
                "err": report.errors,
                "p50": report.p50_ms,
                "use": int(report.usable),
                "n": json.dumps(report.notes[:10]),
            },
        )
    return eval_id


def _position(symbol: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": symbol,
        "side": "long",
        "entry_price": 100.0,
        "mark_price": 100.0,
        "size": 1.0,
        "leverage": 2,
        "unrealised_r": 0.0,
        "hours_in_trade": 24,
        "current_stop": 96.0,
    }
    base.update(over)
    return base


def _plan_record(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "thesis": "trend continuation above the 20/50 with funding flat",
        "stop": 96.0,
        "targets": [{"price": 112.0, "close_pct": 50}, {"price": 124.0, "close_pct": 50}],
        "time_stop_hours": 120,
        "invalidation": "a 4h close below the 20-period low",
    }
    base.update(over)
    return base


REVIEW_CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        name="invalidation_hit_at_a_loss",
        agent="reviewer",
        expect=Expectation.CLOSE,
        why="the plan said what would prove it wrong and it happened",
        dossier=lambda: _dossier(
            "BTC",
            _regime("down"),
            technical={
                "price": 91.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 92.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bear"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position(
                "BTC", **{"mark_price": 91.0, "unrealised_r": -0.9, "hours_in_trade": 40}
            ),
            "plan": _plan_record(),
            "flags": {"invalidation_hit": True},
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="invalidation_hit_in_profit",
        agent="reviewer",
        expect=Expectation.CLOSE,
        why="a broken thesis in profit is a gift, not a reason to stay",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 108.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 109.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bear"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position("BTC", **{"mark_price": 108.0, "unrealised_r": 1.4}),
            "plan": _plan_record(),
            "flags": {"invalidation_hit": True},
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="drawdown_thesis_intact",
        agent="reviewer",
        expect=Expectation.HOLD,
        why="ordinary adverse movement; closing here re-originates the trade",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 97.8,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.5,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position(
                "BTC", **{"mark_price": 97.8, "unrealised_r": -0.55, "hours_in_trade": 50}
            ),
            "plan": _plan_record(),
            "flags": "AUTO",
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="thesis_weakening_in_profit",
        agent="reviewer",
        expect=Expectation.TRIM,
        why="bank some, keep exposure to the part of the thesis that holds",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 108.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "rel_vol_4h": 0.35,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": -19.0},
        ),
        payload={
            "position": _position("BTC", **{"mark_price": 108.0, "unrealised_r": 1.6}),
            "plan": _plan_record(),
            "flags": "AUTO",
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="working_and_intact",
        agent="reviewer",
        expect=Expectation.HOLD,
        why="churn costs spread and funding on every exit and re-entry",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 106.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position("BTC", **{"mark_price": 106.0, "unrealised_r": 0.6}),
            "plan": _plan_record(),
            "flags": "AUTO",
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="tp1_hit_thesis_intact",
        agent="reviewer",
        expect=Expectation.TRIM,
        why="the plan said take part off there",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 112.5,
                "atr_4h_pct": 2.4,
                "high_20_4h": 113.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position(
                "BTC", **{"mark_price": 112.5, "unrealised_r": 2.1, "hours_in_trade": 60}
            ),
            "plan": _plan_record(),
            "flags": {"tp1_hit": True},
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="deep_profit_stop_at_entry",
        agent="reviewer",
        expect=Expectation.MOVE_STOP,
        why="unrealised profit behind an unmoved stop is a round trip waiting to happen. "
        "The stop is still at its ORIGINAL level, not at entry — a stop already at "
        "breakeven would make HOLD correct, which is how the first version of this case "
        "tested nothing",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 124.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 126.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position(
                "BTC", **{"mark_price": 124.0, "unrealised_r": 2.9, "current_stop": 96.0}
            ),
            "plan": _plan_record(),
            # TP1 was trimmed on an earlier run, so the only open question is the stop. Leaving
            # both true made TRIM as defensible as MOVE_STOP, and an ambiguous case tests nothing.
            "flags": {"tp1_hit": False, "tp1_already_trimmed": True},
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="time_stop_breached_flat",
        agent="reviewer",
        expect=Expectation.CLOSE,
        why="the thesis had a horizon and the horizon passed",
        dossier=lambda: _dossier(
            "BTC",
            _regime("chop"),
            technical={
                "price": 100.4,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position(
                "BTC", **{"mark_price": 100.4, "unrealised_r": 0.1, "hours_in_trade": 132}
            ),
            "plan": _plan_record(),
            "flags": {"time_stop_breached": True},
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="time_stop_breached_in_profit",
        agent="reviewer",
        expect=Expectation.CLOSE,
        why="a profitable timeout is still a timeout",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 109.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position(
                "BTC", **{"mark_price": 109.0, "unrealised_r": 1.2, "hours_in_trade": 140}
            ),
            "plan": _plan_record(),
            "flags": {"time_stop_breached": True},
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="time_stop_near_working",
        agent="reviewer",
        expect=Expectation.HOLD,
        why="not breached is not breached",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 107.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position(
                "BTC", **{"mark_price": 107.0, "unrealised_r": 0.8, "hours_in_trade": 110}
            ),
            "plan": _plan_record(),
            "flags": "AUTO",
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="regime_flipped_against",
        agent="reviewer",
        expect=Expectation.CLOSE,
        why="the conditions the trade was taken in no longer exist",
        dossier=lambda: _dossier(
            "BTC",
            _regime("down"),
            technical={
                "price": 99.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bear"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position("BTC", **{"mark_price": 99.0, "unrealised_r": -0.3}),
            "plan": _plan_record(),
            "flags": {"regime_flipped": True},
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="regime_chop_position_working",
        agent="reviewer",
        expect=Expectation.HOLD,
        why="chop restrains new risk; it does not invalidate open risk",
        dossier=lambda: _dossier(
            "BTC",
            _regime("chop"),
            technical={
                "price": 105.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position("BTC", **{"mark_price": 105.0, "unrealised_r": 0.5}),
            "plan": _plan_record(),
            "flags": "AUTO",
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="event_inside_time_stop_flat",
        agent="reviewer",
        expect=Expectation.CLOSE,
        why="unpriced event risk on a position with no cushion",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 100.5,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position("BTC", **{"mark_price": 100.5, "unrealised_r": 0.05}),
            "plan": _plan_record(),
            "flags": {"event_inside_time_stop": True, "event": "US CPI release in 18h"},
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="event_inside_time_stop_in_profit",
        agent="reviewer",
        expect=Expectation.TRIM,
        why="keep a runner, remove the exposure that would hurt",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 110.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position("BTC", **{"mark_price": 110.0, "unrealised_r": 1.5}),
            "plan": _plan_record(),
            "flags": {"event_inside_time_stop": True, "event": "US CPI release in 18h"},
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="funding_turned_hostile",
        agent="reviewer",
        expect=Expectation.TRIM,
        why="cost of carry eats a thesis that needs time",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 104.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 0.0021, "oi_24h_pct": 1.0, "funding_pctile_30d": 98.0},
        ),
        payload={
            "position": _position("BTC", **{"mark_price": 104.0, "unrealised_r": 0.4}),
            "plan": _plan_record(),
            "flags": "AUTO",
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="flush_against_intact_thesis",
        agent="reviewer",
        expect=Expectation.HOLD,
        why="a leverage flush against a sound thesis is noise, and often the low",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 97.2,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "rel_vol_4h": 3.1,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": -0.0002, "oi_24h_pct": -22.0},
        ),
        payload={
            "position": _position("BTC", **{"mark_price": 97.2, "unrealised_r": -0.7}),
            "plan": _plan_record(),
            "flags": "AUTO",
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="spread_blown_out",
        agent="reviewer",
        expect=Expectation.HOLD,
        why="exiting into a bad book realises more than the position has lost",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 99.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "spread_pct": 0.55,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position("BTC", **{"mark_price": 99.0, "unrealised_r": -0.4}),
            "plan": _plan_record(),
            "flags": "AUTO",
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="stop_near_liquidation",
        agent="reviewer",
        expect=Expectation.CLOSE,
        why="being stopped by the venue instead of by the plan is not risk management",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 95.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position(
                "BTC",
                **{
                    "mark_price": 95.0,
                    "unrealised_r": -0.8,
                    "liq_price": 93.5,
                    "current_stop": 94.0,
                },
            ),
            "plan": _plan_record(),
            "flags": "AUTO",
            "portfolio": {},
        },
    ),
    GoldenCase(
        name="portfolio_near_kill_switch",
        agent="reviewer",
        expect=Expectation.CLOSE,
        why="choosing what to close beats having it chosen for you",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 98.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position("BTC", **{"mark_price": 98.0, "unrealised_r": -0.6}),
            "plan": _plan_record(),
            "flags": "AUTO",
            "portfolio": {
                "drawdown_pct": 13.2,
                "kill_switch_at_pct": 15.0,
                "open_positions": 4,
                "weakest_position": "BTC",
                "weakest_reason": "largest unrealised loss, thinnest thesis",
            },
        },
    ),
    GoldenCase(
        name="stale_price_feed",
        agent="reviewer",
        expect=Expectation.HOLD,
        why="acting on data you do not trust is worse than waiting one cycle",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 96.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 118.0,
                "low_20_4h": 96.0,
                "stale": True,
                "ema_4h": {"ema20": 103.0, "ema50": 99.0, "order": "bull"},
            },
            derivatives={"funding_8h": 8e-05, "oi_24h_pct": 1.0},
        ),
        payload={
            "position": _position("BTC", **{"mark_price": 96.0, "unrealised_r": -0.5}),
            "plan": _plan_record(),
            "flags": {"data_stale": True},
            "portfolio": {},
        },
    ),
)


def review_flags(case: GoldenCase) -> dict[str, Any]:
    """The flags production would hand the Reviewer for this case.

    Cases declare `"flags": "AUTO"` to mean "whatever code computes", so a golden case exercises
    the same inputs a live review gets rather than a hand-written convenience.
    """
    from argus.features.review_flags import compute_flags

    declared = case.payload.get("flags")
    dossier = case.dossier()
    computed = compute_flags(
        case.payload.get("position", {}),
        case.payload.get("plan", {}),
        dossier.derivatives,
        dossier.technical,
    )
    if isinstance(declared, dict):
        computed.update(declared)
    return computed


def score_review(case: GoldenCase, decision: Any) -> Score:
    """Did the Reviewer reach the action a careful trader would defend?"""
    return Score(
        case=case.name,
        correct=str(decision.action) == case.expect.value,
        invented_evidence=False,
    )


def score_analyst(case: GoldenCase, view: Any) -> Score:
    """Did it score only what the dossier supports, and cite levels that exist?

    `expect` for an Analyst case is the set of factors that MAY be non-zero. Scoring outside it
    is the failure worth catching: a five-factor reading of a three-family dossier.
    """
    allowed: set[str] = set(case.payload.get("may_score") or ())
    scored = set(view.scores.non_zero())
    known = evidence_paths(case.dossier())
    invented = any(_leaf(p) not in known for ps in view.evidence.values() for p in ps)
    levels = getattr(view, "key_levels", {}) or {}

    def _numbers(node: Any) -> set[float]:
        # EMA levels live nested inside technical["ema_4h"], so a flat scan of the top level
        # marked a genuine level as invented — the third time this scorer accused a model of
        # fabricating something real.
        if isinstance(node, dict):
            return {n for v in node.values() for n in _numbers(v)}
        if isinstance(node, list):
            return {n for v in node for n in _numbers(v)}
        if isinstance(node, int | float) and not isinstance(node, bool):
            return {round(float(node), 6)}
        return set()

    dossier_values = _numbers(case.dossier().technical)
    invented = invented or any(round(float(v), 6) not in dossier_values for v in levels.values())
    return Score(
        case=case.name,
        correct=scored <= allowed,
        invented_evidence=invented,
        detail=f"scored {sorted(scored)}, allowed {sorted(allowed)}",
    )


ANALYST_CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        name="complete_bullish_picture",
        agent="analyst",
        expect=Expectation.TRADE,
        why="every family present and pointing the same way: technical, onchain and macro may "
        "all carry a score because all three are measured",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 104_800.0,
                "atr_4h_pct": 1.9,
                "high_20_4h": 116_200.0,
                "low_20_4h": 101_900.0,
                "rsi_4h": 55.0,
                "ema_4h": {"ema20": 103_900.0, "ema50": 101_200.0, "order": "bull"},
            },
            derivatives={"funding_8h": 0.00006, "oi_7d_pct": 4.4},
            onchain={"fees_24h_usd": 890_000, "fees_7d_pct": 3.0},
            equities=_cross_market(63, 1, 2_070_000_000_000, fred__policy_rate_pct=3.75),
            news=[
                {
                    "title": "Spot ETF inflows continue for a fourth week",
                    "published": "2026-09-19T08:00:00Z",
                    "source": "coindesk",
                    "tag": "factual",
                }
            ],
        ),
        payload={"may_score": ["technical", "onchain", "macro", "fundamental", "catalyst"]},
    ),
    GoldenCase(
        name="missing_onchain_family",
        agent="analyst",
        expect=Expectation.TRADE,
        why="no onchain data at all, so `onchain` must be 0. Scoring it anyway is how a dossier "
        "with three families becomes a five-factor conviction",
        dossier=lambda: _dossier(
            "SOL",
            _regime("up"),
            technical={
                "price": 214.4,
                "atr_4h_pct": 2.6,
                "high_20_4h": 216.0,
                "low_20_4h": 206.5,
                "ema_4h": {"ema20": 208.1, "ema50": 199.4, "order": "bull"},
            },
            derivatives={"funding_8h": 0.00008, "oi_24h_pct": 6.1},
        ),
        payload={"may_score": ["technical"]},
    ),
    GoldenCase(
        name="promotional_news_only",
        agent="analyst",
        expect=Expectation.TRADE,
        why="the only news is somebody's price opinion. An opinion is not evidence, so "
        "`catalyst` and `fundamental` stay at 0",
        dossier=lambda: _dossier(
            "DOGE",
            _regime("up"),
            technical={
                "price": 0.229,
                "atr_4h_pct": 4.5,
                "high_20_4h": 0.2301,
                "low_20_4h": 0.19,
                "ema_4h": {"ema20": 0.221, "ema50": 0.208, "order": "bull"},
            },
            derivatives={"funding_8h": 0.0009},
            news=[
                {
                    "title": "Analysts say DOGE could rally 50% this quarter",
                    "published": "2026-09-20T08:00:00Z",
                    "source": "unknown",
                    "tag": "subjective",
                }
            ],
        ),
        payload={"may_score": ["technical", "onchain"]},
    ),
    GoldenCase(
        name="conflicting_signals",
        agent="analyst",
        expect=Expectation.TRADE,
        why="bullish structure with crowded funding is two readings, not one averaged one. Both "
        "factors may carry a score and they may disagree",
        dossier=lambda: _dossier(
            "ETH",
            _regime("up"),
            technical={
                "price": 2_480.0,
                "atr_4h_pct": 3.1,
                "high_20_4h": 2_570.0,
                "low_20_4h": 2_180.0,
                "ema_4h": {"ema20": 2_390.0, "ema50": 2_300.0, "order": "bull"},
            },
            derivatives={
                "funding_8h": 0.0021,
                "funding_pctile_30d": 96.0,
                "oi_24h_pct": 12.0,
            },
            onchain={"tvl_usd": 48_000_000_000, "tvl_7d_pct": -6.4},
        ),
        payload={"may_score": ["technical", "onchain"]},
    ),
    GoldenCase(
        name="stale_prices",
        agent="analyst",
        expect=Expectation.TRADE,
        why="the price is marked stale, so the technical read is unreliable and confidence "
        "should be low. Nothing else is measured either",
        dossier=lambda: _dossier(
            "ARB",
            _regime("chop"),
            technical={"price": 0.842, "stale": True},
            derivatives={"funding_8h": 0.0},
        ),
        payload={"may_score": ["technical"]},
    ),
)


# What a downstream consumer should act on. A Red Team that never says `agree` is not broken —
# a careful critic can almost always name something — but one whose SEVERITY never falls is
# useless, because a veto that always fires is indistinguishable from a system that never trades.
# Measured 2026-09-25: this model returns `agree=False` on sound readings while correctly rating
# them 2 of 5, so severity is the signal and `agree` is close to a constant (F-136).
from argus.llm.agents.redteam import ACTIONABLE_SEVERITY  # noqa: E402 - one constant


def score_redteam(case: GoldenCase, verdict: Any) -> Score:
    """Judged on the number a consumer would act on, not on the boolean.

    A red team is judged on both sides. Rating a sound reading as unusable costs a trade that
    should have been taken; waving a broken one through is worse than having no red team at all.
    A sound reading must come back BELOW the actionable bar, and a broken one at or above it —
    whether the model also chose to phrase it as agreement is style, not signal.
    """
    expected_agree = case.expect is Expectation.AGREE
    severity = int(getattr(verdict, "severity", 0) or 0)
    correct = (
        (severity < ACTIONABLE_SEVERITY) if expected_agree else (severity >= ACTIONABLE_SEVERITY)
    )
    vague = False
    if not expected_agree and not verdict.agree:
        # An objection that names nothing cannot be acted on. "The analysis seems optimistic"
        # is a mood, not a finding.
        needle = case.payload.get("must_mention") or ()
        text = " ".join(verdict.objections).lower()
        vague = bool(needle) and not any(n.lower() in text for n in needle)
    return Score(
        case=case.name,
        correct=correct and not vague,
        invented_evidence=vague,
        detail=f"agree={verdict.agree} severity={verdict.severity}",
    )


def _analysis(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "BTC",
        "scores": {"fundamental": 0, "onchain": 0, "macro": 0, "catalyst": 0, "technical": 2},
        "evidence": {"technical": ["tech.ema4h.order", "tech.px"]},
        "key_levels": {},
        "summary": "trend structure is constructive",
        "confidence": 3,
        "data_gaps": [],
    }
    base.update(over)
    return base


_REDTEAM_ANALYST_CASES: tuple[GoldenCase, ...] = (
    # AGREE cases rebuilt 2026-09-25. The first attempt failed all four, and the Red Team was
    # right: a confidence of 3 on a single-family read with declared gaps, and a price sitting
    # near the low of a 14% range while the summary called the structure constructive, are fair
    # objections. An AGREE fixture has to be a reading a careful critic genuinely cannot fault —
    # coverage matching confidence, every score cited, and the price where the thesis says it is
    # (F-136). Getting that wrong measures the fixture, not the model.
    GoldenCase(
        name="multi_family_reading_matched_confidence",
        agent="redteam",
        expect=Expectation.AGREE,
        why="three families carry data, every non-zero score is cited, confidence 3 against three "
        "of five families, and price sits mid-range on a pullback rather than at an extreme. "
        "Objecting here costs a trade that should have been taken",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 106_200.0,
                "atr_4h_pct": 2.2,
                "high_20_4h": 113_400.0,
                "low_20_4h": 102_400.0,
                "ema_4h": {"ema20": 104_900.0, "ema50": 101_800.0, "order": "bull"},
            },
            derivatives={"funding_8h": 0.00005, "oi_7d_pct": 3.2, "basis_pct": 0.04},
            # Fixed 2026-10-07: this used exchange netflow and active addresses, which the
            # dossier renderer does not carry. The model saw no chain block at all while the
            # reading cited `chain.netflow7d$`, so a careful critic was right to object — one of
            # the two "misses" on sound readings was the fixture's. Fees are rendered.
            onchain={"fees_24h_usd": 1_900_000, "fees_7d_pct": 6.5},
        ),
        payload={
            "analysis": _analysis(
                scores={"fundamental": 0, "onchain": 1, "macro": 0, "catalyst": 0, "technical": 2},
                evidence={
                    "technical": ["tech.ema4h.order", "tech.px"],
                    "onchain": ["chain.fees7d%"],
                },
                confidence=3,
                summary="pullback into a rising 20/50 with network fees up 6.5% on the week; "
                "funding flat",
            )
        },
    ),
    GoldenCase(
        name="low_confidence_matches_thin_coverage",
        agent="redteam",
        expect=Expectation.AGREE,
        why="one family, one citation, and confidence 1 to say so. Thin coverage honestly "
        "declared is the behaviour we want; objecting to it punishes the Analyst for candour",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 106_200.0,
                "atr_4h_pct": 2.2,
                "high_20_4h": 113_400.0,
                "low_20_4h": 102_400.0,
                "ema_4h": {"ema20": 104_900.0, "ema50": 101_800.0, "order": "bull"},
            },
        ),
        payload={
            "analysis": _analysis(
                scores={"fundamental": 0, "onchain": 0, "macro": 0, "catalyst": 0, "technical": 1},
                evidence={"technical": ["tech.ema4h.order"]},
                confidence=1,
                summary="technical only, and thin: stack is bullish, nothing else is visible",
                data_gaps=["onchain: missing", "macro: missing", "catalyst: missing"],
            )
        },
    ),
    GoldenCase(
        name="bearish_reading_held_to_the_same_standard",
        agent="redteam",
        expect=Expectation.AGREE,
        why="the same quality of evidence pointing down, in a down regime. An attacker that "
        "waves through longs and interrogates shorts makes the system quietly directional, and "
        "nothing in the log would ever say so",
        dossier=lambda: _dossier(
            "BTC",
            _regime("down"),
            technical={
                "price": 92_000.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 104_000.0,
                "low_20_4h": 88_500.0,
                "ema_4h": {"ema20": 95_200.0, "ema50": 98_900.0, "order": "bear"},
            },
            derivatives={"funding_8h": 0.00011, "oi_7d_pct": 6.0},
        ),
        payload={
            "analysis": _analysis(
                scores={"fundamental": 0, "onchain": 0, "macro": 0, "catalyst": 0, "technical": 2},
                evidence={
                    "technical": ["tech.ema4h.order", "tech.px"],
                    "derivatives": ["deriv.fund8h%"],
                },
                confidence=2,
                summary="lower highs beneath a falling 20/50; longs still paying funding",
            )
        },
    ),
    GoldenCase(
        name="sound_reading_agree",
        agent="redteam",
        expect=Expectation.AGREE,
        why="every score is cited, nothing in the dossier contradicts it, confidence matches "
        "the coverage. Objecting here costs a trade that should have been taken",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 104_800.0,
                "atr_4h_pct": 1.9,
                "high_20_4h": 116_200.0,
                "low_20_4h": 101_900.0,
                "ema_4h": {"ema20": 103_900.0, "ema50": 101_200.0, "order": "bull"},
            },
            derivatives={"funding_8h": 0.00006, "oi_7d_pct": 4.4},
        ),
        payload={"analysis": _analysis(data_gaps=["onchain: missing", "macro: missing"])},
    ),
    GoldenCase(
        name="scored_a_missing_family",
        agent="redteam",
        expect=Expectation.OBJECT,
        why="the reading scores onchain +2 on a dossier with no chain block at all",
        dossier=lambda: _dossier(
            "SOL",
            _regime("up"),
            technical={
                "price": 214.4,
                "atr_4h_pct": 2.6,
                "high_20_4h": 216.0,
                "low_20_4h": 206.5,
                "ema_4h": {"ema20": 208.1, "ema50": 199.4, "order": "bull"},
            },
            derivatives={"funding_8h": 0.00008},
        ),
        payload={
            "analysis": _analysis(
                symbol="SOL",
                scores={
                    "fundamental": 0,
                    "onchain": 2,
                    "macro": 0,
                    "catalyst": 0,
                    "technical": 2,
                },
                evidence={"technical": ["tech.px"], "onchain": ["chain.tvl"]},
            ),
            "must_mention": ["onchain", "chain", "tvl"],
        },
    ),
    GoldenCase(
        name="ignored_hostile_funding",
        agent="redteam",
        expect=Expectation.OBJECT,
        why="funding sits at the 96th percentile against the direction and the reading does not "
        "mention it once",
        dossier=lambda: _dossier(
            "ETH",
            _regime("up"),
            technical={
                "price": 2_480.0,
                "atr_4h_pct": 3.1,
                "high_20_4h": 2_570.0,
                "low_20_4h": 2_180.0,
                "ema_4h": {"ema20": 2_390.0, "ema50": 2_300.0, "order": "bull"},
            },
            derivatives={"funding_8h": 0.0021, "funding_pctile_30d": 96.0, "oi_24h_pct": 12.0},
        ),
        payload={
            "analysis": _analysis(symbol="ETH"),
            # The dossier abbreviates, and a good objection quotes the dossier: a model writing
            # "fundpct is at the 96th percentile" is being precise, not evasive. Checking only
            # for the word "funding" marked the best answer of the six as vague (F-106).
            "must_mention": ["funding", "fundpct", "fund8h", "96"],
        },
    ),
    GoldenCase(
        name="opinion_as_evidence",
        agent="redteam",
        expect=Expectation.OBJECT,
        why="the catalyst score rests on a headline that says analysts *expect* a rally. That "
        "is a forecast, not an observation",
        dossier=lambda: _dossier(
            "DOGE",
            _regime("up"),
            technical={
                "price": 0.229,
                "atr_4h_pct": 4.5,
                "high_20_4h": 0.2301,
                "low_20_4h": 0.19,
                "ema_4h": {"ema20": 0.221, "ema50": 0.208, "order": "bull"},
            },
            derivatives={"funding_8h": 0.0009},
            news=[
                {
                    "title": "Analysts expect DOGE to rally 50% this quarter",
                    "published": "2026-09-20T08:00:00Z",
                    "source": "unknown",
                    "tag": "subjective",
                }
            ],
        ),
        payload={
            "analysis": _analysis(
                symbol="DOGE",
                scores={
                    "fundamental": 0,
                    "onchain": 0,
                    "macro": 0,
                    "catalyst": 2,
                    "technical": 1,
                },
                evidence={"technical": ["tech.px"], "catalyst": ["news.0"]},
            ),
            "must_mention": ["catalyst", "news", "opinion", "forecast", "analyst"],
        },
    ),
    GoldenCase(
        name="invented_level",
        agent="redteam",
        expect=Expectation.OBJECT,
        why="key_levels carries a resistance that appears nowhere in the dossier",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 104_800.0,
                "atr_4h_pct": 1.9,
                "high_20_4h": 116_200.0,
                "low_20_4h": 101_900.0,
                "ema_4h": {"ema20": 103_900.0, "ema50": 101_200.0, "order": "bull"},
            },
            derivatives={"funding_8h": 0.00006},
        ),
        payload={
            "analysis": _analysis(key_levels={"resistance": 131_400.0}),
            "must_mention": ["level", "131", "resistance"],
        },
    ),
    GoldenCase(
        name="confidence_outruns_coverage",
        agent="redteam",
        expect=Expectation.OBJECT,
        why="confidence 5 on a dossier missing three of five families",
        dossier=lambda: _dossier(
            "ARB",
            _regime("chop"),
            technical={"price": 0.842, "atr_4h_pct": 4.2, "high_20_4h": 0.965, "low_20_4h": 0.79},
            derivatives={"funding_8h": 0.0},
        ),
        payload={
            "analysis": _analysis(symbol="ARB", confidence=5),
            "must_mention": ["confidence", "missing", "gaps", "coverage", "not present"],
        },
    ),
)


def _plan_payload(symbol: str, side: str, **over: Any) -> dict[str, Any]:
    """A Trader plan exactly as `pipeline.vet()` hands it to the Red Team.

    Built through `TradePlan` and dumped the way production dumps it, so an invalid fixture fails
    at import rather than measuring a plan the schema would never have let through.
    """
    base: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "entry_kind": "limit",
        "time_stop_hours": 96,
    }
    base.update(over)
    return TradePlan(**base).model_dump(mode="json")


def _tech_scores(technical: int, **others: int) -> FactorScores:
    return FactorScores(
        fundamental=others.get("fundamental", 0),
        onchain=others.get("onchain", 0),
        macro=others.get("macro", 0),
        catalyst=others.get("catalyst", 0),
        technical=technical,
    )


# Added 2026-10-07 (L-4). Since 2026-10-06 the live chain feeds the Red Team the Trader's PLAN,
# while every case above feeds it an Analyst reading — so the severity that now halves or drops a
# trade had never been measured on the input it is used on. Both sides again: two plans a careful
# critic cannot fault, and the three flaws the live chain relies on this role to catch. Each
# flawed plan still passes the schema and the code-side evidence check in `vet()` (every citation
# names a real field), because a flaw code already refuses never reaches the Red Team.
_REDTEAM_PLAN_CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        name="plan_sound_long_pullback",
        agent="redteam",
        expect=Expectation.AGREE,
        why="a pullback long in a bull stack: every score cited, the stop below real structure, "
        "the target under the real 20-bar high, flat funding named, the missing family "
        "declared and left unscored. Rating it 3 halves a trade that should be taken whole",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 106_200.0,
                "atr_4h_pct": 2.2,
                "high_20_4h": 113_400.0,
                "low_20_4h": 102_400.0,
                "ema_4h": {"ema20": 104_900.0, "ema50": 101_800.0, "order": "bull"},
            },
            derivatives={"funding_8h": 0.00005, "oi_7d_pct": 3.2, "basis_pct": 0.04},
        ),
        payload={
            "plan": _plan_payload(
                "BTC",
                "long",
                entry_low=104_800.0,
                entry_high=105_200.0,
                stop=101_300.0,
                targets=[Target(price=113_200.0, close_pct=100)],
                scores=_tech_scores(2),
                evidence={
                    "technical": ["tech.ema4h.order", "tech.px", "tech.lo20_4h", "tech.hi20_4h"]
                },
                thesis="Pullback toward the rising 4h EMA20 (104.9k) in a bull 20/50 stack, "
                "regime TREND_UP. Funding is flat (0.005% per 8h) and open interest rose only "
                "3.2% over 7d, so the long side is not crowded. Stop 101.3k sits below the 4h "
                "EMA50 (101.8k) and the 20-bar low (102.4k); one target at 113.2k, just under "
                "the 20-bar high of 113.4k. On-chain data is missing, so it is not scored.",
                invalidation_rules=[
                    {"kind": "price_close_below", "level": 101_300.0, "timeframe": "4h"}
                ],
            )
        },
    ),
    GoldenCase(
        name="plan_sound_short_held_to_the_same_standard",
        agent="redteam",
        expect=Expectation.AGREE,
        why="the same quality of plan pointing down. A red team that waves through longs and "
        "interrogates shorts makes the system quietly directional",
        dossier=lambda: _dossier(
            "BTC",
            _regime("down"),
            technical={
                "price": 92_000.0,
                "atr_4h_pct": 2.4,
                "high_20_4h": 104_000.0,
                "low_20_4h": 83_800.0,
                "ema_4h": {"ema20": 94_100.0, "ema50": 97_600.0, "order": "bear"},
            },
            derivatives={"funding_8h": 0.00011, "oi_7d_pct": 6.0},
        ),
        payload={
            "plan": _plan_payload(
                "BTC",
                "short",
                entry_low=92_800.0,
                entry_high=93_600.0,
                stop=97_900.0,
                targets=[Target(price=84_000.0, close_pct=100)],
                scores=_tech_scores(2),
                evidence={"technical": ["tech.ema4h.order", "tech.px", "tech.lo20_4h"]},
                thesis="Lower highs beneath a falling 4h 20/50 (bear order), regime TREND_DOWN; "
                "price 92.0k is under the 4h EMA20 at 94.1k. Longs still pay funding (0.011% "
                "per 8h) while open interest rose 6% over 7d. Limit entry 92.8-93.6k on a bounce "
                "toward the EMA20; stop 97.9k above the 4h EMA50 (97.6k); target 84.0k, just "
                "above the 20-bar low of 83.8k. On-chain data is missing and not scored.",
                invalidation_rules=[
                    {"kind": "price_close_above", "level": 97_900.0, "timeframe": "4h"}
                ],
            )
        },
    ),
    GoldenCase(
        name="plan_uncited_score",
        agent="redteam",
        expect=Expectation.OBJECT,
        why="catalyst +2 'cited' with the price. The dossier has no catalyst and no headline; a "
        "citation that names a real field but says nothing about the factor is an uncited score "
        "that code cannot see",
        dossier=lambda: _dossier(
            "SOL",
            _regime("up"),
            technical={
                "price": 214.4,
                "atr_4h_pct": 2.6,
                "high_20_4h": 226.0,
                "low_20_4h": 206.5,
                "ema_4h": {"ema20": 210.1, "ema50": 203.4, "order": "bull"},
            },
            derivatives={"funding_8h": 0.00008},
        ),
        payload={
            "plan": _plan_payload(
                "SOL",
                "long",
                entry_low=211.0,
                entry_high=212.0,
                stop=205.8,
                targets=[Target(price=225.5, close_pct=100)],
                scores=_tech_scores(1, catalyst=2),
                evidence={"technical": ["tech.ema4h.order"], "catalyst": ["tech.px"]},
                thesis="Bull 20/50 stack with price above the 4h EMA20. A strong catalyst "
                "backdrop supports continuation toward the 20-bar high.",
            ),
            "must_mention": ["catalyst", "news", "headline"],
        },
    ),
    GoldenCase(
        name="plan_invented_level",
        agent="redteam",
        expect=Expectation.OBJECT,
        why="half the position exits at a '131.4k weekly resistance' that appears nowhere in the "
        "dossier — the 20-bar high is 116.2k and nothing above it is shown",
        dossier=lambda: _dossier(
            "BTC",
            _regime("up"),
            technical={
                "price": 104_800.0,
                "atr_4h_pct": 1.9,
                "high_20_4h": 116_200.0,
                "low_20_4h": 101_900.0,
                "ema_4h": {"ema20": 103_900.0, "ema50": 101_200.0, "order": "bull"},
            },
            derivatives={"funding_8h": 0.00006},
        ),
        payload={
            "plan": _plan_payload(
                "BTC",
                "long",
                entry_low=104_400.0,
                entry_high=105_000.0,
                stop=100_900.0,
                targets=[
                    Target(price=116_000.0, close_pct=50),
                    Target(price=131_400.0, close_pct=50),
                ],
                scores=_tech_scores(2),
                evidence={"technical": ["tech.ema4h.order", "tech.px", "tech.hi20_4h"]},
                thesis="Bull stack on the 4h with price above the EMA20. First target under the "
                "20-bar high at 116.2k; the second at the 131.4k weekly resistance. Stop below "
                "the 4h EMA50 and the 20-bar low.",
            ),
            "must_mention": ["131", "level", "resistance", "target"],
        },
    ),
    GoldenCase(
        name="plan_ignored_contradiction",
        agent="redteam",
        expect=Expectation.OBJECT,
        why="a long into funding at the 96th percentile with open interest up 12% in a day — "
        "crowded longs paying 0.21% per 8h — and the thesis never mentions it",
        dossier=lambda: _dossier(
            "ETH",
            _regime("up"),
            technical={
                "price": 2_480.0,
                "atr_4h_pct": 3.1,
                "high_20_4h": 2_760.0,
                "low_20_4h": 2_180.0,
                "ema_4h": {"ema20": 2_390.0, "ema50": 2_300.0, "order": "bull"},
            },
            derivatives={"funding_8h": 0.0021, "funding_pctile_30d": 96.0, "oi_24h_pct": 12.0},
        ),
        payload={
            "plan": _plan_payload(
                "ETH",
                "long",
                entry_low=2_395.0,
                entry_high=2_420.0,
                stop=2_280.0,
                targets=[Target(price=2_750.0, close_pct=100)],
                scores=_tech_scores(2),
                evidence={"technical": ["tech.ema4h.order", "tech.px", "tech.hi20_4h"]},
                thesis="Pullback to the rising 4h EMA20 in a bull 20/50 stack. Stop below the "
                "4h EMA50; target just under the 20-bar high.",
            ),
            "must_mention": ["funding", "fundpct", "fund8h", "96", "crowd"],
        },
    ),
)

REDTEAM_CASES: tuple[GoldenCase, ...] = _REDTEAM_ANALYST_CASES + _REDTEAM_PLAN_CASES


def score_risk(case: GoldenCase, verdict: Any) -> Score:
    """Right recommendation, and a concern that names its source when it is not `proceed`."""
    correct = verdict.recommendation == case.expect.value
    vague = False
    needle = case.payload.get("must_mention") or ()
    if verdict.recommendation != "proceed" and needle:
        text = " ".join(verdict.concerns).lower() + " " + verdict.rationale.lower()
        vague = not any(n.lower() in text for n in needle)
    return Score(case=case.name, correct=correct and not vague, invented_evidence=vague)


_RISK_PLAN: dict[str, Any] = {
    "symbol": "SOL",
    "side": "long",
    "entry": 214.0,
    "stop": 205.5,
    "targets": [{"price": 238.0, "close_pct": 100}],
    "time_stop_hours": 120,
    "sized": {"contracts": 4.2, "risk_usd": 50.0, "leverage": 2},
}


def _risk_case(name: str, expect: Expectation, why: str, **payload: Any) -> GoldenCase:
    base: dict[str, Any] = {
        "plan": dict(_RISK_PLAN),
        "portfolio": {
            "equity_usd": 5000,
            "open_positions": [],
            "open_risk_pct": 0.0,
            "max_heat_pct": 6.0,
        },
        "regime": "TREND_UP",
        "calendar": [],
    }
    base.update(payload)
    return GoldenCase(
        name=name,
        agent="riskdebate",
        expect=expect,
        why=why,
        dossier=lambda: _dossier("SOL", _regime("up"), technical={"price": 214.0}),
        payload=base,
    )


RISK_CASES: tuple[GoldenCase, ...] = (
    _risk_case(
        "clean_trade_empty_book",
        Expectation.PROCEED,
        "nothing open, no event, regime aligned. A risk reviewer that trims this quietly "
        "halves every position the system ever takes",
    ),
    # Two more PROCEED cases, added 2026-09-25 for the same reason as the Red Team's (F-136):
    # three `reduce` against one `proceed` meant a model answering "reduce" to everything scored
    # 3/5, and halving every position forever is a failure nobody notices as one.
    _risk_case(
        "one_uncorrelated_position_open",
        Expectation.PROCEED,
        "a BTC long is open and this is an unrelated alt with low beta, well inside the heat "
        "budget. Reducing for the mere existence of another position makes the second trade of "
        "every week worse than the first",
        portfolio={
            "equity_usd": 5000,
            "open_risk_pct": 1.2,
            "max_heat_pct": 6.0,
            "open_positions": [{"symbol": "BTC", "side": "long", "risk_pct": 1.2, "beta_btc": 1.0}],
        },
    ),
    _risk_case(
        "event_well_outside_the_time_stop",
        Expectation.PROCEED,
        "FOMC is nine days out and the time stop is 96 hours, so the trade is closed before it "
        "lands. A calendar entry is only a risk if the position is still open when it arrives",
        calendar=[{"when_hours": 216, "what": "FOMC rate decision", "impact": "high"}],
    ),
    _risk_case(
        "correlated_with_open_long",
        Expectation.REDUCE,
        "an ETH long is already open with high BTC beta; a SOL long on top is one bigger bet "
        "wearing two names",
        portfolio={
            "equity_usd": 5000,
            "open_risk_pct": 2.0,
            "max_heat_pct": 6.0,
            "open_positions": [
                {"symbol": "ETH", "side": "long", "risk_usd": 50, "beta_btc": 1.3},
                {"symbol": "AVAX", "side": "long", "risk_usd": 50, "beta_btc": 1.5},
            ],
        },
        must_mention=["correlat", "ETH", "AVAX", "beta", "same direction"],
    ),
    _risk_case(
        "book_nearly_full",
        Expectation.DECLINE,
        "5.5% of a 6% heat budget is already committed; a good trade on a full book is still "
        "one trade too many",
        portfolio={
            "equity_usd": 5000,
            "open_risk_pct": 5.5,
            "max_heat_pct": 6.0,
            "open_positions": [
                {"symbol": "BTC", "side": "long", "risk_usd": 100},
                {"symbol": "ETH", "side": "long", "risk_usd": 90},
                {"symbol": "LINK", "side": "long", "risk_usd": 85},
            ],
        },
        must_mention=["heat", "full", "budget", "5.5"],
    ),
    _risk_case(
        "event_inside_time_stop",
        Expectation.REDUCE,
        "CPI lands 30 hours into a 120-hour trade — volatility the stop was not designed for",
        calendar=[{"when_hours": 30, "what": "US CPI release", "impact": "high"}],
        must_mention=["CPI", "event", "calendar"],
    ),
    _risk_case(
        "recent_stop_out_same_symbol",
        Expectation.REDUCE,
        "SOL stopped out twice this week on the same thesis; re-entering straight after is how "
        "one bad read becomes three",
        portfolio={
            "equity_usd": 5000,
            "open_risk_pct": 0.0,
            "max_heat_pct": 6.0,
            "open_positions": [],
            "recent_stop_outs": [
                {"symbol": "SOL", "hours_ago": 30, "r": -1.0},
                {"symbol": "SOL", "hours_ago": 70, "r": -1.0},
            ],
        },
        must_mention=["stop", "SOL", "recent", "again"],
    ),
)


def score_regime(case: GoldenCase, call: Any) -> Score:
    """Right posture. `reduce_risk` is the case label for the posture spelled `reduce`."""
    want = {"normal": "normal", "reduce_risk": "reduce", "stand_aside": "stand_aside"}[
        case.expect.value
    ]
    return Score(case=case.name, correct=call.posture == want, invented_evidence=False)


def _regime_case(
    name: str, expect: Expectation, why: str, trend: str, **payload: Any
) -> GoldenCase:
    base: dict[str, Any] = {"market": {}, "calendar": [], "trend": trend}
    base.update(payload)
    return GoldenCase(
        name=name,
        agent="regime",
        expect=expect,
        why=why,
        dossier=lambda: _dossier("BTC", _regime(trend), technical={"price": 100.0}),
        payload=base,
    )


REGIME_CASES: tuple[GoldenCase, ...] = (
    _regime_case(
        "clean_trend_quiet_calendar",
        Expectation.NORMAL,
        "trend confirmed, breadth healthy, nothing scheduled. The label already says it all",
        "up",
        market={"btc_close": 104800, "breadth_20": 64, "funding_median_pct": 0.008, "rv20_pct": 38},
    ),
    _regime_case(
        "fomc_in_twelve_hours",
        Expectation.REDUCE_RISK,
        "a rate decision lands inside almost every trade's horizon, so carry less risk through "
        "it. owner's call 2026-09-24, and the one both model families reached unprompted: standing "
        "aside for every scheduled macro event would leave the book flat most weeks, and the "
        "event is on the calendar precisely because everyone can see it coming",
        "up",
        market={"btc_close": 104800, "breadth_20": 60},
        calendar=[{"when_hours": 12, "what": "FOMC rate decision", "impact": "high"}],
    ),
    _regime_case(
        "breadth_diverging_from_price",
        Expectation.REDUCE_RISK,
        "BTC holds the highs while only 28% of the universe is above its 20-day — the trend is "
        "narrowing, which argues for smaller size without invalidating it",
        "up",
        market={"btc_close": 104800, "breadth_20": 28, "breadth_20_7d_ago": 61},
    ),
    _regime_case(
        "market_wide_funding_extreme",
        Expectation.REDUCE_RISK,
        "median funding at the 97th percentile across the universe: positioning is crowded "
        "everywhere, and crowded markets unwind together",
        "up",
        market={
            "btc_close": 104800,
            "breadth_20": 58,
            "funding_median_pct": 0.09,
            "funding_median_pctile_90d": 97,
        },
    ),
    _regime_case(
        "murky_read_low_data",
        Expectation.NORMAL,
        "thin data should lower confidence, not the posture. A murky read is normal at "
        "confidence 1 — reducing on uncertainty alone makes every quiet week a half-size week",
        "chop",
        market={"btc_close": 100.4},
    ),
)


# --- Catalyst Extractor ------------------------------------------------------------------------
#
# These fixtures use `publisher` and a real `datetime`, unlike the older cases above which pass
# `source` and a string. `_evidence()` reads `publisher`/`published`, so the older ones render as
# `src="unknown" asof="undated"` — harmless for the Trader, which cites metric paths, but fatal
# here: the Catalyst Extractor is scored on whether it copied the source it actually saw.


def _headline(title: str, publisher: str, hours_ago: int = 6) -> dict[str, Any]:
    return {
        "title": title,
        "publisher": publisher,
        "published": NOW - timedelta(hours=hours_ago),
        "current": True,
    }


def _catalyst_case(
    name: str, expect: Expectation, why: str, news: list[dict[str, Any]], **payload: Any
) -> GoldenCase:
    return GoldenCase(
        name=name,
        agent="catalyst",
        expect=expect,
        why=why,
        dossier=lambda: _dossier("ARB", _regime("up"), technical={"price": 1.12}, news=news),
        payload=payload,
    )


CATALYST_CASES: tuple[GoldenCase, ...] = (
    _catalyst_case(
        "dated_unlock",
        Expectation.EXTRACT,
        "an explicit date in the headline is the one case where a candidate is unambiguous",
        [_headline("Arbitrum to unlock 92.6M ARB tokens on 2026-10-16, worth $104M", "theblock")],
        count=1,
        kind="unlock",
        when="2026-10-16",
    ),
    _catalyst_case(
        "undated_unlock_must_stay_unknown",
        Expectation.EXTRACT,
        "the event is real but the date is not in the text. Inventing one from the token's known "
        "schedule is the failure this agent exists to avoid — its training data predates this "
        "market, and a wrong date silently mis-times every trade that respects it",
        [_headline("Next Arbitrum cliff unlock approaching, community debates timing", "decrypt")],
        count=1,
        kind="unlock",
        when="unknown",
    ),
    _catalyst_case(
        "past_event_is_not_a_catalyst",
        Expectation.NOTHING,
        "an unlock that already cleared is history. Returning it as a catalyst tells the pipeline "
        "to expect supply that has already arrived",
        [
            _headline(
                "Arbitrum unlocked 92.6M ARB last Thursday; price absorbed the supply",
                "coindesk",
                hours_ago=20,
            )
        ],
    ),
    _catalyst_case(
        "rumour_is_not_an_event",
        Expectation.NOTHING,
        "'sources say considering' has no date and no commitment. Treating it as a catalyst "
        "imports someone else's speculation as a fact this system will act on",
        [
            _headline(
                "Sources say a major exchange is considering an ARB perpetual listing", "cointele"
            )
        ],
    ),
    _catalyst_case(
        "three_outlets_one_event",
        Expectation.EXTRACT,
        "the same listing reported three times is one candidate. Three would make a single event "
        "look like a cluster, which is exactly how a calendar starts lying",
        [
            _headline("Binance to list ARB perpetual futures on 2026-10-08", "theblock"),
            _headline("ARB perps coming to Binance October 8", "decrypt", hours_ago=5),
            _headline("Binance adds ARB perpetual contract next week", "coindesk", hours_ago=4),
        ],
        count=1,
        kind="listing",
        when="2026-10-08",
    ),
    _catalyst_case(
        "promotional_noise",
        Expectation.NOTHING,
        "'5 coins set to explode' is advertising. An extractor that cannot tell a paid placement "
        "from an event will fill the calendar with whatever was promoted hardest",
        [
            _headline("5 altcoins set to explode this October — ARB tops our list", "cryptoblog"),
            _headline("Is ARB the next 100x? Analysts weigh in", "moonpost", hours_ago=9),
        ],
    ),
)


def score_catalyst(case: GoldenCase, answer: Any) -> Score:
    """Did it extract what was there, and only what was there?

    Three separate failures, kept separate: extracting nothing from a real event, extracting an
    event that was not there, and — the expensive one — supplying a date the evidence did not
    contain. `invented_evidence` covers both a fabricated source and a fabricated date, because
    both are the model filling a field from memory rather than from the page.
    """
    candidates = list(answer.candidates)
    evidence = catalyst_evidence(case)
    _, unsourced = filter_sourced(candidates, evidence)
    invented = bool(unsourced)
    detail = f"unsourced: {[c.source for c in unsourced]}" if unsourced else ""

    if case.expect is Expectation.NOTHING:
        return Score(
            case=case.name,
            correct=not candidates,
            invented_evidence=invented,
            detail=detail or (f"expected none, got {[c.headline[:40] for c in candidates]}"),
        )

    want_count = int(case.payload.get("count", 1))
    if len(candidates) != want_count:
        return Score(
            case=case.name,
            correct=False,
            invented_evidence=invented,
            detail=f"expected {want_count} candidate(s), got {len(candidates)}",
        )
    first = candidates[0]
    problems = []
    if (kind := case.payload.get("kind")) and first.kind != kind:
        problems.append(f"kind {first.kind} != {kind}")
    if (when := case.payload.get("when")) and first.when != when:
        problems.append(f"when {first.when} != {when}")
        if when == "unknown":
            # A date conjured for an undated event is invention, not a near miss.
            invented = True
    return Score(
        case=case.name,
        correct=not problems,
        invented_evidence=invented,
        detail="; ".join(problems) or detail,
    )


def catalyst_evidence(case: GoldenCase) -> str:
    return evidence_block([case.dossier()])


# --- Post-mortem -------------------------------------------------------------------------------
#
# Every case is built so that the PnL points one way and the process points the other, or so that
# the two agree for a reason the model has to name. A model that simply reads `outcome_r` and
# grades the sign will fail four of these six, which is the point: hindsight is the failure mode
# here, and it is only visible when the result and the verdict are allowed to disagree.


def _postmortem_case(
    name: str,
    expect: Expectation,
    why: str,
    plan: dict[str, Any],
    outcome: dict[str, Any],
    trail: list[dict[str, Any]] | None = None,
    **expected: Any,
) -> GoldenCase:
    return GoldenCase(
        name=name,
        agent="postmortem",
        expect=expect,
        why=why,
        dossier=lambda: _dossier("SOL", _regime("up"), technical={"price": 182.0}),
        payload={"plan": plan, "outcome": outcome, "trail": trail or [], **expected},
    )


_SOL_PLAN = {
    "symbol": "SOL",
    "side": "long",
    "entry": 178.5,
    "stop": 171.2,
    "stop_basis": "below the 20-day EMA at 172.4 and last week's swing low at 171.9",
    "targets": [{"price": 193.0, "close_pct": 50}, {"price": 205.0, "close_pct": 50}],
    "time_stop_hours": 96,
    "thesis": "reclaim of the 20-day EMA on rising spot volume, funding flat, breadth 62%",
    "invalidation": "a daily close back below 171.5, or funding above 0.08%",
    "atr_14": 7.8,
}

POSTMORTEM_CASES: tuple[GoldenCase, ...] = (
    _postmortem_case(
        "sound_loss_stopped_out",
        Expectation.SOUND,
        "the thesis never broke — price simply reached the stop first. Markets take stops, and a "
        "post-mortem that calls this flawed teaches the system to stop taking valid setups",
        _SOL_PLAN,
        {
            "exit_reason": "stop_filled",
            "exit_price": 171.2,
            "outcome_r": -1.0,
            "hours_held": 38,
            "max_favourable_r": 0.4,
            "thesis_at_exit": "intact — EMA still rising, funding still flat",
            "btc_return_same_window_pct": -1.8,
        },
        would_take_again=True,
    ),
    _postmortem_case(
        "flawed_win_on_thin_evidence",
        Expectation.FLAWED,
        "it made +2.1R on a single promotional headline with no technical or on-chain support. "
        "This is the most dangerous trade in any journal, because the profit argues for repeating "
        "it and the process argues that the profit was luck",
        {
            **_SOL_PLAN,
            "entry": 178.5,
            "thesis": "'SOL set to explode this quarter' trending on X; momentum play",
            "invalidation": "momentum fades",
            "stop_basis": "round number 5% below entry",
            "stop": 169.6,
        },
        {
            "exit_reason": "target_filled",
            "exit_price": 197.1,
            "outcome_r": 2.1,
            "hours_held": 52,
            "thesis_at_exit": "n/a — there was no measurable thesis to check",
            "btc_return_same_window_pct": 6.4,
        },
        would_take_again=False,
    ),
    _postmortem_case(
        "stop_inside_ordinary_noise",
        Expectation.FLAWED,
        "the stop sat 0.4 ATR from entry and was taken out by a single session's range, after "
        "which price ran to both targets. The idea was right and the plan made it unplayable",
        {
            **_SOL_PLAN,
            "stop": 175.4,
            "stop_basis": "tight stop to keep risk small",
        },
        {
            "exit_reason": "stop_filled",
            "exit_price": 175.4,
            "outcome_r": -1.0,
            "hours_held": 9,
            "thesis_at_exit": "intact — and price reached 205 two days later",
            "btc_return_same_window_pct": 0.3,
        },
        stop_placement="too_tight",
        would_take_again=False,
    ),
    _postmortem_case(
        "chased_the_entry",
        Expectation.FLAWED,
        "the plan named 178.5 on a retest; the fill was 186.2 after a 4% run. Same idea, but the "
        "risk-reward that justified it was gone by the time the money went in",
        _SOL_PLAN,
        {
            "exit_reason": "time_stop",
            "exit_price": 184.0,
            "fill_price": 186.2,
            "outcome_r": -0.3,
            "hours_held": 96,
            "thesis_at_exit": "intact but unrealised",
            "btc_return_same_window_pct": 1.1,
        },
        entry_timing="late",
        would_take_again=False,
    ),
    _postmortem_case(
        "sound_win_as_planned",
        Expectation.SOUND,
        "plan followed, first target taken, stop moved to break-even, second target hit. There is "
        "nothing to fix, and inventing something to fix is its own failure",
        _SOL_PLAN,
        {
            "exit_reason": "target_filled",
            "exit_price": 205.0,
            "outcome_r": 2.2,
            "hours_held": 71,
            "thesis_at_exit": "played out as written",
            "btc_return_same_window_pct": 2.0,
        },
        [
            {"at_hours": 30, "action": "TRIM", "why": "tp1 hit at 193"},
            {"at_hours": 30, "action": "MOVE_STOP", "why": "to break-even at 178.5"},
        ],
        would_take_again=True,
    ),
    _postmortem_case(
        "closed_early_on_broken_thesis",
        Expectation.SOUND,
        "the invalidation fired and the Reviewer closed at -0.4R instead of waiting for the stop. "
        "Losing less than planned by following the plan is a sound process, not a mistake",
        _SOL_PLAN,
        {
            "exit_reason": "reviewer_close",
            "exit_price": 175.6,
            "outcome_r": -0.4,
            "hours_held": 44,
            "thesis_at_exit": "broken — daily close at 170.9 and funding at 0.11%",
            "btc_return_same_window_pct": -3.2,
        },
        [{"at_hours": 44, "action": "CLOSE", "why": "invalidation hit: close below 171.5"}],
        would_take_again=True,
    ),
)


def score_postmortem(case: GoldenCase, report: Any) -> Score:
    """Right verdict on the process, and consistent with itself.

    `would_take_again` is scored as well as `process_verdict` because either alone is easy to get
    right by accident — a model that answers `sound` to everything scores 2/6 on the verdict but
    contradicts itself the moment it is asked to commit. `invented_evidence` reuses
    `outcome_bias()`, the same check production journals, so the golden set measures exactly what
    the live pipeline watches for.
    """
    want = case.expect.value
    problems = []
    if report.process_verdict != want:
        problems.append(f"process {report.process_verdict} != {want}")
    if (again := case.payload.get("would_take_again")) is not None:
        if report.would_take_again != again:
            problems.append(f"would_take_again {report.would_take_again} != {again}")
    for field_name in ("entry_timing", "sizing", "stop_placement"):
        if (expected := case.payload.get(field_name)) and getattr(report, field_name) != expected:
            problems.append(f"{field_name} {getattr(report, field_name)} != {expected}")
    bias = outcome_bias(report, float(case.payload["outcome"]["outcome_r"]))
    return Score(
        case=case.name,
        correct=not problems,
        invented_evidence=bias is not None,
        detail="; ".join(problems) or (bias or ""),
    )


def case_prompt(case: GoldenCase) -> tuple[str, type[Any]]:
    """The prompt and output type one golden case needs, whichever role it belongs to.

    Written as a single dispatch rather than a branch inside each caller: a live quality run, a
    unit test and the weekly eval must all put a case to a model exactly the way production does,
    and a role that is missing here fails loudly instead of quietly going untested.
    """
    from argus.llm.agents import (
        analyst,
        catalyst,
        postmortem,
        redteam,
        reflector,
        regime,
        reviewer,
        riskdebate,
        trader,
    )
    from argus.llm.schemas import (
        AnalystView,
        CatalystList,
        LessonSet,
        PostMortem,
        RedTeamVerdict,
        RegimeCall,
        ReviewDecision,
        RiskVerdict,
    )

    dossier = case.dossier()
    payload = case.payload
    match case.agent:
        case "trader":
            return trader.build_prompt(dossier), TraderAnswer
        case "analyst":
            return analyst.build_prompt(dossier), AnalystView
        case "reviewer":
            return (
                reviewer.build_prompt(
                    payload.get("position", {}),
                    payload.get("plan", {}),
                    dossier,
                    review_flags(case),
                    payload.get("portfolio", {}),
                ),
                ReviewDecision,
            )
        case "redteam":
            # A plan case is what the live chain sends (`pipeline.vet`); an analysis case is
            # what the role was first measured on. Both go through the same prompt builder.
            reading = payload.get("plan") or payload.get("analysis", {})
            return redteam.build_prompt(dossier, reading), RedTeamVerdict
        case "riskdebate":
            return (
                riskdebate.build_prompt(
                    payload.get("plan", {}),
                    payload.get("portfolio", {}),
                    payload.get("regime", "TREND_UP (risk x1.0)"),
                    payload.get("calendar", []),
                ),
                RiskVerdict,
            )
        case "regime":
            return (
                regime.build_prompt(
                    dossier.regime, payload.get("market", {}), payload.get("calendar", [])
                ),
                RegimeCall,
            )
        case "catalyst":
            return catalyst.build_prompt([dossier], today=NOW.date()), CatalystList
        case "postmortem":
            return (
                postmortem.build_prompt(
                    payload["plan"], payload.get("trail", []), payload["outcome"]
                ),
                PostMortem,
            )
        case "consensus":
            # The same prompt the Trader gets, and deliberately not the primary's answer: an
            # anchored second opinion is a rubber stamp with extra latency.
            return trader.build_prompt(dossier), TraderAnswer
        case "reflector":
            return (
                reflector.build_prompt(payload["trades"], payload.get("active")),
                LessonSet,
            )
    raise KeyError(f"no prompt builder for agent {case.agent!r}")


def evaluate_agent(ref: Any, agent: str, temperature: float = 0.1) -> ModelReport:
    """Put one model through one role's whole suite directly — no routing, no fallback."""
    import time as clock

    from pydantic import ValidationError
    from pydantic_ai import Agent
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    from argus.config import models_config
    from argus.llm.router import build_model

    cases, scorer = SUITES[agent]
    # The per-provider ceiling is production's, not a benchmark convenience: Groq caps output
    # tokens per minute (F-089), and measuring it at 1500 measures a model that never runs.
    policy = models_config().get("policy") or {}
    max_tokens = int(
        (policy.get("max_tokens_by_provider") or {}).get(
            ref.provider, policy.get("max_tokens", 1500)
        )
    )
    report = ModelReport(label=ref.label)
    for case in cases:
        report.total += 1
        prompt, output_type = case_prompt(case)
        started = clock.perf_counter()
        try:
            result = Agent(build_model(ref), output_type=output_type).run_sync(
                prompt, model_settings={"temperature": temperature, "max_tokens": max_tokens}
            )
        except (ValidationError, UnexpectedModelBehavior) as exc:
            report.schema_fail += 1
            report.notes.append(f"{case.name}: schema {type(exc).__name__}")
            continue
        except Exception as exc:  # noqa: BLE001 - a dead provider is a result, not a crash
            report.errors += 1
            report.notes.append(f"{case.name}: {type(exc).__name__}")
            continue
        report.latencies_ms.append(int((clock.perf_counter() - started) * 1000))
        score = scorer(case, result.output)
        report.correct += int(score.correct)
        report.invented += int(score.invented_evidence)
        if not score.correct or score.invented_evidence:
            report.notes.append(f"{case.name}: {score.detail or 'wrong'}")
    return report


# --- Reflector ---------------------------------------------------------------------------------
#
# The Reflector's failures are all failures of restraint: generalising from one trade, and hiding
# the trades that disagree. So one case offers a vivid loss with no pattern behind it, one offers a
# real pattern with real counter-examples that must be counted, and one makes the tempting lesson
# a sizing rule — which code refuses whatever evidence stands behind it.


def _trade(
    trade_id: str,
    symbol: str,
    outcome_r: float,
    *,
    side: str = "long",
    regime: str = "TREND_UP",
    funding_pct: float = 0.01,
    slippage_pct: float = 0.0,
    verdict: str = "sound",
    exit_reason: str = "stop_filled",
    thesis: str = "",
) -> dict[str, Any]:
    # A short carrying a long's thesis is an incoherent trade, and models say so instead of
    # answering the question the case is asking (F-114). The default follows the side.
    thesis = thesis or (
        "reclaim of the 20-day EMA on rising spot volume"
        if side == "long"
        else "rejection at the 20-day EMA with spot volume fading"
    )
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "regime_at_entry": regime,
        "funding_pct_at_entry": funding_pct,
        "entry_slippage_pct": slippage_pct,
        "thesis": thesis,
        "outcome_r": outcome_r,
        "exit_reason": exit_reason,
        "hours_held": 40,
        "postmortem": {
            "process_verdict": verdict,
            "would_take_again": verdict == "sound",
            "entry_timing": "late" if slippage_pct >= 3 else "good",
            "stop_placement": "right",
        },
    }


def _reflector_case(
    name: str, expect: Expectation, why: str, trades: list[dict[str, Any]], **payload: Any
) -> GoldenCase:
    return GoldenCase(
        name=name,
        agent="reflector",
        expect=expect,
        why=why,
        dossier=lambda: _dossier("BTC", _regime("up"), technical={"price": 104800.0}),
        payload={"trades": trades, **payload},
    )


REFLECTOR_CASES: tuple[GoldenCase, ...] = (
    _reflector_case(
        "funding_pattern_across_four_trades",
        Expectation.EXTRACT,
        "four losing longs all entered with funding above 0.05% and the one entered flat won. "
        "That is a checkable precondition, which is what makes it a lesson rather than a mood",
        [
            _trade("t1", "SOL", -1.0, funding_pct=0.09),
            _trade("t2", "ARB", -1.0, funding_pct=0.07),
            _trade("t3", "AVAX", -0.8, funding_pct=0.06),
            _trade("t4", "OP", -1.0, funding_pct=0.11),
            _trade("t5", "ETH", 2.2, funding_pct=0.01, exit_reason="target_filled"),
        ],
        scope="factor",
        min_support=3,
    ),
    _reflector_case(
        "one_vivid_loss_is_not_a_pattern",
        Expectation.NOTHING,
        "a single -2R on a broken thesis, with two sound winners either side. Generalising from "
        "one trade is how a system acquires a superstition it then trades on for two months",
        [
            _trade("t1", "BTC", 2.4, exit_reason="target_filled"),
            _trade("t2", "SOL", -2.0, verdict="flawed", thesis="momentum after a 20% weekly run"),
            _trade("t3", "ETH", 1.8, exit_reason="target_filled"),
        ],
    ),
    _reflector_case(
        "pattern_with_real_counterexamples",
        Expectation.EXTRACT,
        "six shorts entered while funding was already negative — the crowd short before us — and "
        "four lost. The pattern is real, the two winners are real, and naming both is the whole "
        "job: at 4:2 code refuses it on the ratio, which is the system working. Rebuilt twice "
        "(F-114): the first version gave the losers no feature the winners lacked, and the second "
        "gave seven trades one identical thesis, which models correctly read as a sample with "
        "nothing to explain",
        [
            _trade(
                "t1",
                "SOL",
                -1.0,
                side="short",
                funding_pct=-0.04,
                thesis="lower high beneath the 20-day EMA after a failed breakout",
            ),
            _trade(
                "t2",
                "ARB",
                -1.0,
                side="short",
                funding_pct=-0.06,
                thesis="unlock supply into a thinning bid",
            ),
            _trade(
                "t3",
                "OP",
                -0.9,
                side="short",
                funding_pct=-0.03,
                thesis="rejection at the prior range high on fading spot volume",
            ),
            _trade(
                "t4",
                "AVAX",
                -1.0,
                side="short",
                funding_pct=-0.05,
                thesis="breakdown from a three-week base with negative netflow",
            ),
            _trade(
                "t5",
                "DOGE",
                1.9,
                side="short",
                funding_pct=-0.05,
                exit_reason="target_filled",
                thesis="retail froth unwinding after a 40% weekly run",
            ),
            _trade(
                "t6",
                "LINK",
                2.3,
                side="short",
                funding_pct=-0.04,
                exit_reason="target_filled",
                thesis="distribution below the 50-day with declining active addresses",
            ),
            _trade(
                "t7",
                "SUI",
                1.4,
                side="short",
                funding_pct=0.03,
                exit_reason="target_filled",
                thesis="crowded longs paying to hold into a supply cliff",
            ),
        ],
        needs_contradictions=2,
    ),
    _reflector_case(
        "the_tempting_lesson_is_a_sizing_rule",
        Expectation.EXTRACT,
        "every loss here is a full -1R and every win a partial. The obvious conclusion is about "
        "how much to bet, and that conclusion is not this role's to reach: the gate sizes, in "
        "code, and a sizing rule in the Trader's prompt would only argue it into worse plans",
        [
            _trade("t1", "SOL", -1.0),
            _trade("t2", "ARB", -1.0),
            _trade("t3", "OP", -1.0),
            _trade("t4", "AVAX", 0.4, exit_reason="time_stop"),
            _trade("t5", "ETH", 0.3, exit_reason="time_stop"),
            _trade("t6", "BTC", 0.5, exit_reason="time_stop"),
        ],
        allow_nothing=True,
    ),
    _reflector_case(
        "chased_entries_underperformed",
        Expectation.EXTRACT,
        "four trades filled more than 3% above plan all landed between -1R and +0.3R. This is "
        "about how the trades were taken, not why they were chosen — the `execution` scope exists "
        "for exactly this and putting it under `factor` sends the Analyst looking for a signal",
        [
            _trade("t1", "SOL", -1.0, slippage_pct=4.3, verdict="flawed"),
            _trade("t2", "ARB", -0.9, slippage_pct=3.8, verdict="flawed"),
            _trade("t3", "OP", 0.2, slippage_pct=5.1, exit_reason="time_stop", verdict="flawed"),
            _trade("t4", "AVAX", -1.0, slippage_pct=3.2, verdict="flawed"),
            _trade("t5", "ETH", 2.1, slippage_pct=0.1, exit_reason="target_filled"),
            _trade("t6", "BTC", 1.7, slippage_pct=0.0, exit_reason="target_filled"),
        ],
        scope="execution",
        min_support=3,
    ),
    _reflector_case(
        "losses_cluster_in_chop",
        Expectation.EXTRACT,
        "five trades in CHOP averaged -0.8R while three in TREND_UP all won. The pattern belongs "
        "to the market state, not to any signal or asset",
        [
            _trade("t1", "SOL", -1.0, regime="CHOP"),
            _trade("t2", "ARB", -0.7, regime="CHOP", exit_reason="time_stop"),
            _trade("t3", "OP", -1.0, regime="CHOP"),
            _trade("t4", "AVAX", -0.6, regime="CHOP", exit_reason="time_stop"),
            _trade("t5", "DOGE", -0.9, regime="CHOP"),
            _trade("t6", "ETH", 2.2, exit_reason="target_filled"),
            _trade("t7", "BTC", 1.9, exit_reason="target_filled"),
            _trade("t8", "LINK", 2.6, exit_reason="target_filled"),
        ],
        scope="regime",
        min_support=3,
    ),
)


def score_reflector(case: GoldenCase, answer: Any) -> Score:
    """Grounded, restrained, honest about counter-examples, and never a risk rule.

    `invented_evidence` covers both fabricated trade ids and a candidate that tries to legislate
    risk. The second is not invention in the literal sense, but it is the same failure from the
    system's point of view: output that would change behaviour on authority it does not have.
    """
    from argus.journal.lessons import ground

    candidates = list(answer.candidates)
    known = {str(t["trade_id"]) for t in case.payload["trades"]}
    usable, rejected = ground(candidates, known)
    invented = bool(rejected)
    detail = "; ".join(f"{reason}" for _, reason in rejected)

    if case.expect is Expectation.NOTHING:
        return Score(
            case=case.name,
            correct=not candidates,
            invented_evidence=invented,
            detail=detail or (f"expected none, got {[c.lesson[:50] for c in candidates]}"),
        )

    if not usable:
        # The sizing-temptation case is passed by silence as readily as by a safe lesson: refusing
        # to answer is a legitimate way to avoid proposing a rule you are not allowed to propose.
        allowed = bool(case.payload.get("allow_nothing")) and not candidates
        return Score(
            case=case.name,
            correct=allowed,
            invented_evidence=invented,
            detail=detail or "no usable candidate",
        )

    best = max(usable, key=lambda c: c.support)
    problems = []
    if (scope := case.payload.get("scope")) and not any(c.scope == scope for c in usable):
        problems.append(f"scope {[c.scope for c in usable]} lacks {scope}")
    if (floor := case.payload.get("min_support")) and best.support < int(floor):
        problems.append(f"support {best.support} < {floor}")
    if needed := case.payload.get("needs_contradictions"):
        if max(c.contradiction for c in usable) < int(needed):
            problems.append(f"named no counter-examples; {needed} were in the sample")
    return Score(
        case=case.name,
        correct=not problems,
        invented_evidence=invented,
        detail="; ".join(problems) or detail,
    )


# --- Consensus ---------------------------------------------------------------------------------
#
# `compare()` is pure code and already has unit tests. What no test touched is the thing the
# second opinion exists for: does the model reach its own conclusion, or does it ratify whatever
# it is put next to?
#
# So a case is a dossier plus a *stated primary answer*, and the score is what `compare()` makes
# of the model's independent reply. The model never sees the primary — showing it would anchor the
# answer, which is the failure being measured. Three cases pair a clear dossier with a primary
# that misreads it: a model that returns AGREE on those is a rubber stamp, and a rubber stamp with
# latency is worse than no second opinion at all, because it looks like a check.


def _plan(side: str, entry: float, stop: float, target: float) -> TradePlan:
    from argus.llm.schemas import Target

    return TradePlan(
        symbol="BTC",
        side=side,  # type: ignore[arg-type]
        entry_kind="limit",
        entry_low=entry * 0.998,
        entry_high=entry * 1.002,
        stop=stop,
        targets=[Target(price=target, close_pct=100)],
        time_stop_hours=96,
        thesis="stated by the primary Trader",
        evidence={"technical": ["tech.px"]},
        scores=FactorScores(fundamental=0, onchain=0, macro=0, catalyst=0, technical=2),
    )


def _primary_trade(side: str, entry: float, stop: float, target: float) -> TraderAnswer:
    return TraderAnswer(decision="trade", plan=_plan(side, entry, stop, target))


def _primary_refusal(reason: str = "no edge at this price") -> TraderAnswer:
    return TraderAnswer(decision="no_trade", reason=reason)


def _consensus_case(
    name: str,
    expect: Expectation,
    why: str,
    source_case: str,
    primary: TraderAnswer,
    **payload: Any,
) -> GoldenCase:
    base = next(c for c in CASES if c.name == source_case)
    return GoldenCase(
        name=name,
        agent="consensus",
        expect=expect,
        why=why,
        dossier=base.dossier,
        payload={"primary": primary, **payload},
    )


CONSENSUS_CASES: tuple[GoldenCase, ...] = (
    _consensus_case(
        "clean_setup_both_want_it",
        Expectation.AGREE,
        "a trend, a pullback and room to the first target. If the second opinion cannot agree "
        "here it will veto good trades all week, which is the quieter of the two failures and "
        "the more expensive one over a month",
        "clean_uptrend",
        _primary_trade("long", 106200, 102000, 113400),
    ),
    _consensus_case(
        "nothing_there_both_decline",
        Expectation.AGREE,
        "chop with no edge, and the primary said so. Two refusals are an agreement — the pair "
        "should cost nothing when there is nothing to argue about",
        "chop_no_edge",
        _primary_refusal("range-bound, no structural edge"),
        tradeable=False,
    ),
    _consensus_case(
        "primary_trades_into_chop",
        Expectation.ONE_SIDED,
        "the dossier is the one the Trader itself refuses, and the primary has proposed a long "
        "anyway. This is the case the Consensus exists for: agreeing here makes it a rubber "
        "stamp, and a rubber stamp with latency is worse than no check, because it looks like one",
        "chop_no_edge",
        _primary_trade("long", 100.4, 96.0, 108.0),
    ),
    _consensus_case(
        "primary_trades_on_stale_data",
        Expectation.ONE_SIDED,
        "the data is too old to act on and the primary proposed a trade regardless. A second "
        "opinion that does not notice the staleness is not reading the dossier, only the plan",
        "stale_data",
        _primary_trade("long", 100.0, 95.0, 110.0),
    ),
    _consensus_case(
        "primary_fights_the_regime",
        Expectation.ONE_SIDED,
        "a counter-regime short the Trader refuses on its own. Either verdict that blocks it is "
        "right — declining, or reading the other direction — so the scorer accepts CONFLICT here "
        "too. What it does not accept is agreement",
        "counter_regime_short",
        _primary_trade("short", 100.0, 105.0, 90.0),
        also_accept=["conflict"],
    ),
    _consensus_case(
        "primary_chases_an_extended_move",
        Expectation.ONE_SIDED,
        "price is already extended and the primary wants in. The failure to catch is enthusiasm, "
        "not error: the plan looks well formed, and only the dossier says it is late",
        "already_extended",
        _primary_trade("long", 100.0, 94.0, 112.0),
    ),
    _consensus_case(
        "second_good_setup_both_want_it",
        Expectation.AGREE,
        "a breakout retest, the other clean entry in the set. Two agreement cases rather than "
        "one, because a model that blocks everything scores perfectly on the vetoes alone",
        "breakout_retest",
        _primary_trade("long", 100.0, 96.5, 108.0),
    ),
)


def score_consensus(case: GoldenCase, answer: Any) -> Score:
    """What `compare()` makes of the model's own reply against the stated primary.

    Scored through `compare()` rather than on the raw answer because agreement is the thing being
    measured, and `compare()` is where agreement is defined. A model that refuses everything
    passes every veto case, so the suite carries two cases where refusing is the failure.
    """
    from argus.llm.agents.consensus import compare

    primary: TraderAnswer = case.payload["primary"]
    result = compare(primary, answer)
    accepted = {case.expect.value, *case.payload.get("also_accept", [])}
    correct = result.agreement.value in accepted
    if correct and case.expect is Expectation.AGREE:
        # "Agree" has to mean the same thing the run would act on: two refusals agree but do not
        # trade, and a case that expects a tradeable agreement must get one.
        correct = result.tradeable is case.payload.get("tradeable", True)
    return Score(
        case=case.name,
        correct=correct,
        invented_evidence=False,
        detail="" if correct else f"{result.agreement.value}: {result.detail}",
    )


# Agent -> (cases, scorer). One place the harness can ask "what does quality mean for this role?",
# so a live quality run does not need a branch per agent and a new agent cannot be forgotten.
SUITES: dict[str, tuple[tuple[GoldenCase, ...], Any]] = {
    "trader": (CASES, score_answer),
    "reviewer": (REVIEW_CASES, score_review),
    "analyst": (ANALYST_CASES, score_analyst),
    "redteam": (REDTEAM_CASES, score_redteam),
    "riskdebate": (RISK_CASES, score_risk),
    "regime": (REGIME_CASES, score_regime),
    "catalyst": (CATALYST_CASES, score_catalyst),
    "postmortem": (POSTMORTEM_CASES, score_postmortem),
    "reflector": (REFLECTOR_CASES, score_reflector),
    "consensus": (CONSENSUS_CASES, score_consensus),
}
