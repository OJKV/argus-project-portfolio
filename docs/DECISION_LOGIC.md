# DECISION_LOGIC.md — Regime, scoring, sizing, gates, review

Every rule states *why* it exists and where the number came from. Numbers marked **[tune]** are initial values to be revised with journal evidence, never by feel.

---

## 1. Decision flow (Manager run)

```
regime_snapshot ──► candidate_screen ──► dossiers ──► [LLM] regime → analysts → red team → trader
                                                                                   │
                                                  TradePlan(s) | NO_TRADE ◄────────┘
                                                        │
                                          ┌─────────────▼──────────────┐
                                          │ RISK GATE (code)           │
                                          │ score → data-quality →     │
                                          │ structure → size → lever → │
                                          │ heat/corr → funding →      │
                                          │ events → protections →     │
                                          │ sanity                     │
                                          └───────┬────────┬───────────┘
                                         APPROVED │ ADJUSTED│ REJECTED (journaled, shadow-tracked)
                                                  ▼
                                          mode==approve ? Telegram queue : place_bracket()
```

## 2. Regime rules (code)

Inputs (all from the regime snapshot): `btc_ema_stack` (1d 20/50/200), `btc_rv20_pct` (20d realized vol percentile over 1y), `breadth_20` (% universe above 20d EMA), `funding_median`, `oi_7d_pct`, `stable_supply_7d_pct`, `etf_5d_usd`, `real_yield_10y`, `next_event_hours` (first high-impact macro event).

| Regime | Rule (evaluate top-down, first match wins) | risk_mult | limit_only |
|---|---|---|---|
| `EVENT_RISK` | `next_event_hours ≤ 24` (FOMC/CPI/NFP/PPI/major vote) **or** `btc_rv20_pct ≥ 90` | 0.4 | yes |
| `TREND_UP` | 1d close > EMA20 > EMA50 and EMA20 slope > 0 and `breadth_20 ≥ 55%` | 1.0 | no |
| `TREND_DOWN` | 1d close < EMA20 < EMA50 and slope < 0 and `breadth_20 ≤ 45%` | 1.0 | no |
| `CHOP` | otherwise | 0.5 | yes |

Modifiers (applied on top): `real_yield_10y > 2.50%` (neighbor tripwire) → longs' risk_mult × 0.5; `stable_supply_7d_pct < −1%` → longs × 0.7; `etf_5d_usd < −$500M` → BTC/ETH longs × 0.7. Posture from the Regime Analyst can only *reduce* risk_mult, never raise it.

*Why:* every loss in the Sep runs that wasn't a thesis error was a regime error (CPI flush stopping ETH/BTC by cents). Code decides the envelope; the LLM decides inside it.

## 3. Candidate screen (code)

```
universe  = OKX USDT perps ∩ {vol24h ≥ $30M, age ≥ 90d, maxLev ≥ 10, spread ≤ 0.06%} − blocklist − stables/wrapped/leveraged
exclude   = open positions ∪ cooldown ∪ pending plans
attention = z(|ret_24h|) + z(|ret_7d|) + z(rel_volume) + z(|oi_24h_pct|) + z(|funding_pctile−50|)
          + z(|tvl_7d_pct|) + z(news_count_24h) + 2·[catalyst ≤ 14d]
candidates = top 8 by attention + {BTC, ETH, SOL}      # 11 max → 3 analyst batches
```
*Why not CMC gainers:* the Sep runs showed it omits genuine large-cap movers and is dominated by micro-caps that OKX doesn't even list as swaps.

## 4. Five-factor scoring (LLM proposes, code validates)

| Factor | Question it answers | Primary evidence fields in dossier | +2 example | −2 example |
|---|---|---|---|---|
| **Fundamental** | Does the asset earn, and does any of it reach the token? | `onchain_fundamental.fees_7d_pct`, `revenue_7d_pct`, `tvl_30d_pct`, `token_revenue_share` | Fees +40% w/w, revenue reaches holders, FDV/fees below peers | Revenue up 22× on a subsidy that expires; 0% reaches token; top-100 hold 77% |
| **On-chain** | What are holders, whales, exchanges actually doing? | `exchange_netflow`, `whale_events`, `stable_flow_chain`, `unlock_claim_rate` | Sustained exchange outflows + whale accumulation | Team/investor unlock 2% circ in 5 days with sell precedent |
| **Macro** | What regime, and what's the transmission channel? | `macro.*` (regime, real yield, DXY, Fed odds, ETF flows) | Risk-on trend, real yields falling, ETF inflows | Real yield > 2.5% rising, 86% hike priced, ETF outflows accelerating |
| **Catalyst** | What is *scheduled* that forces a repricing? | `catalysts[]` with dates & sources | Two dated catalysts inside 18 days aligned with side | Only catalyst is the opposite side's |
| **Technical** | Where are the levels, and how is positioning skewed? | `technical.*`, `derivatives.*` | Trend + structure + OI rising with price + funding not crowded | Shorting the low after −20% OI capitulation; crowded same-side funding |

Rules (code-enforced):
- Score ∈ {−2,−1,0,+1,+2}; each non-zero score must cite ≥1 dossier field path; uncited ⇒ 0.
- Missing/stale *trade-critical* family ⇒ |score| ≤ 1 for that factor.
- **Take-trade threshold: total ≥ +4 and no factor = −2.** Short in `TREND_UP` (or long in `TREND_DOWN`) requires ≥ +6.
- Red Team may adjust each score by ±1 with a cited reason; final = analyst + red-team delta.
- Confidence % is *tracked*, not used for sizing (LLM confidence is miscalibrated until the calibration table says otherwise).

Calibration loop: `calibration` table stores (bucket, side, regime) → n, win rate, avg R. After n ≥ 20 in a bucket: if avg R < 0 → threshold for that bucket +1; if win rate ≥ 60% and avg R ≥ 0.5 over n ≥ 40 → risk_per_trade may unlock to 1.5–2% for that bucket **[tune]**.

## 5. Sizing & leverage (code only)

```
equity            = account equity (USDT) at run start
risk_per_trade    = 1.0%  [tune: 2.0% after 30 closed trades with PF ≥ 1.3]
risk_usd          = equity × risk_per_trade × regime.risk_mult × protection_mult
stop_dist_pct     = |entry − stop| / entry            # must be within [1.0, 3.5] × ATR14% on plan TF
notional          = risk_usd / stop_dist_pct
notional          = min(notional, 0.25 × equity × regime.risk_mult)     # per-position cap
leverage          = ceil(notional / max_margin_per_position)             # start from margin budget
leverage          = clamp(leverage, 2, 10)
liq_dist_pct      ≈ (1/leverage) − maint_margin_rate − fees_buffer      # isolated, approx; use OKX tier table
require liq_dist_pct ≥ 2.5 × stop_dist_pct else leverage -= 1 (repeat) ; if leverage < 2 → REJECT
margin            = notional / leverage
contracts         = floor(notional / (contract_value × mark_price)) respecting lot size
```

**Worked example (from the 11 Sep ARB plan):** equity $2,000; risk 1% = $20; entry 0.158, stop 0.170 → stop_dist 7.6% (ATR14% on 4h ≈ 3.1% → 2.45×ATR ✓). notional = 20 / 0.076 = **$263** (13% of equity ✓ cap $500). Margin budget 8% equity = $160 → leverage = ceil(263/160) = 2 → 3x chosen for rounding headroom; liq_dist at 3x ≈ 31% ≥ 2.5×7.6% = 19% ✓. Margin $88. If stopped: −$20 (−1%), not −7.6% of anything you'd notice. TP1 0.125 → +$55 (2.75R on TP1 alone).

*Why risk-based, not %-of-balance:* the prototype sized by `POSITION_SIZE_PERCENT` of balance regardless of stop distance, so a wide-stop trade risked 3–4× a tight-stop one. Risk-based sizing makes every trade a ~1R bet and makes the calibration table meaningful.
*Why leverage is derived:* leverage only sets margin efficiency and liquidation distance; it is not "conviction". Your prior manual practice (5–20x, 20–50% of capital per plan) is far more aggressive than this; you accepted the conservative envelope for the first 60 days (Q2), with the mechanism below for when the account is too small for it.

### 5.1 Small-account mode (live-small at $100–200; your Q2 mechanism)

At $150 equity, 1% risk = $1.50 → a 5% stop implies $30 notional, which is at or below many OKX minimum contract sizes and gets eaten by fees. Rather than silently skipping every plan, the gate escalates through fixed steps and journals which step was used:

```
step 0 (normal):        risk 1.0%, lev ≤ 10, notional ≤ 25% equity
step 1 (small-account): risk 2.0%, lev ≤ 10, notional ≤ 40% equity        → if step 0 notional < max(min_notional × 1.2, $25 viable floor [tune])
step 2 (small-account): risk 3.5%, lev ≤ 10, notional ≤ 50% equity        → if step 1 still < min_notional × 1.2
step 3 (aggressive):    risk 5.0%, lev 5–10, notional ≤ 60% equity        → only while equity < $500 (profile `aggressive` is the DEFAULT below $500 — your N3; `/risk conservative` disables step 3)
else → REJECTED: INSUFFICIENT_BALANCE (journal dossier + math + shortfall; Telegram alert; shadow-track)
```
Invariants that never relax: liquidation distance ≥ 2.5× stop distance; ≤ 2 open positions while equity < $500 (fees and correlation dominate otherwise); expected round-trip fees ≤ 20% of expected TP1 profit (else reject — the trade cannot pay for itself); daily/weekly loss limits scale with the step (step 3 ⇒ daily 6%, weekly 12%). The step used is a column in `plans`, so calibration can be read per step. Exit small-account mode automatically once equity ≥ $500 for 7 consecutive days.

*Why steps, not a free hand:* a $150 account can be a learning account or a lottery ticket; steps keep the per-trade risk bounded (max 5%) while letting the bot actually trade. Expect the calibration table to be noisy until equity grows — that is fine, the point of live-small is plumbing under real fills.

## 6. Risk Gate — full rule list

| # | Gate | Rule | On fail |
|---|---|---|---|
| 1 | Score | total ≥ +4, no −2 (≥ +6 counter-regime) | REJECT |
| 2 | Data quality | price consensus ok (≥3 sources, majority within 1%, direction agrees; the OKX mark must be inside that majority, read fresh with the mark at gate time from OKX/Bybit/Binance/Hyperliquid; fewer than 3 readable venues ⇒ REJECT, review D-2); derivatives family present & ≤2h old; news ≤48h present; no `suspect` on cited evidence | REJECT (or ADJUST size ×0.5 if only news missing) |
| 3 | Structure | stop_dist ∈ [1.0, 3.5]×ATR14; stop beyond nearest swing level; RR(TP1) ≥ 1.5; RR(blended) ≥ 2.0; targets ordered; entry zone within ±3% of price (limit); a limit zone may not lie wholly on the far side of price (long zone above the mark, short zone below — F-166) | REJECT; may ADJUST stop to nearest structure if within band |
| 4 | Size | §5 | ADJUST |
| 5 | Leverage | §5; ≤10x; liq ≥ 2.5×stop | ADJUST / REJECT |
| 6 | Portfolio heat | open risk ≤ 5% equity; gross ≤ 150%; beta-adj net ≤ 100%; ≤4 positions; ≤2 same-direction high-beta alts (beta>1.3 to BTC) | ADJUST size to fit or REJECT |
| 7 | Funding | `funding_rate_8h × (time_stop_h/8) × notional ≤ 0.25 × expected_profit_TP1` | REJECT or ADJUST time stop |
| 8 | Events | no new entry within 6h before high-impact event unless `event_thesis`; `EVENT_RISK` → limit-only, ×0.5 | REJECT / ADJUST |
| 9 | Protections | daily loss ≥3% → no entries today; weekly ≥6% → HALT; 2 stop-outs same symbol/5d → cooldown 5d; 3 consecutive losses → 24h pause then ×0.5 for 3 trades; ≤2 entries/run, ≤3/day | REJECT |
| 10 | Sanity | symbol in market list, contracts ≥ min size, no opposite/duplicate position, side consistent with entry/stop/targets, entry not `market` unless `urgent` & spread ≤0.03% | REJECT |
| 11 | Kill-switch conflict | plan direction contradicts an *active* kill-switch — an open position's own invalidation rule that fired on a closed candle (e.g., an open long's `price_close_below` → no new longs); any new entry while a §9 portfolio switch is active (equity DD ≥ 15% from the 30d high → also HALT; margin ratio < 200% → reduce; a margin ratio the venue does not report for any open position → no new entries). Blocking follows the side of the position that owns the fired rule, not the rule kind | REJECT |
| 12 | Balance & min size | `required_margin ≤ free_margin × 0.9` (buffer for fees/slippage) and `notional ≥ instrument min notional × 1.2`; small-account steps §5.1 tried first | REJECT `INSUFFICIENT_BALANCE` (journaled, alerted, shadow-tracked) |
| 13 | Fee viability | expected round-trip fees (taker unless post-only fill assumed) + expected funding ≤ 20% of expected TP1 profit; fee rates from the account fee-rate endpoint, reconciled weekly against fills | REJECT |
| 14 | Downside-miss monitor | rolling 20 closed trades: if realised loss > 1.3R on ≥ 25% of losers (stop slippage/gaps), cap leverage at 5x and widen liquidation buffer to 3× until the rate normalises | ADJUST (portfolio-wide) |

All ADJUSTs are journaled with before/after; if cumulative adjustment shrinks size below 50% of the LLM's implied size, the plan is downgraded to REJECT (a heavily-adjusted plan is a different trade than the one scored).

## 7. Order construction

```
entry: limit @ zone_mid (post_only if regime.limit_only); ttl = plan.entry_valid_hours (default 12)
attachAlgoOrds: [ {sl_trigger: stop, sl_type: mark, sl_ord_px: -1 (market)},
                  {tp_trigger: tp1, tp_ord_px: -1, size: tp1_close_pct} ]
trailing (NON-NEGOTIABLE): every position gets a trailing stop.
  plan.trail = {activation: price | "+3%" | "tp1", callback_pct}      # Manager chooses
  default when omitted or rejected by gate:
      activation = min(+3.0% in favour, TP1)          [tune]
      callback   = max(1.5 × ATR14%, 2.0%)             [tune; clamp 1–4%]
      floor      = breakeven + round-trip fees once active (never trails back through BE)
  placement: attached trailing algo at order time when OKX supports it for the instrument;
             else the first Review after fill places move_order_stop; watchdog confirms within 20 min.
after TP1 fill (Review): move SL → breakeven + fees if plan.be_after_tp1 (default true); trail activates if not yet
tp2..n: algo orders placed after entry fill (Review run confirms)
time stop: plan.time_stop_hours (default 120h; max 168h) → Review closes unless PnL > +1R and trail active
```
*Why attach at placement:* the bot is asleep 23h/day; a position must never exist without an exchange-side stop. Watchdog verifies this every 20 min.

## 8. Review rules (Review run)

Deterministic first (no LLM):
1. Reconcile; any position without live SL → repair (plan stop, else 2×ATR) + alert.
2. `TP1 hit` → SL to BE, activate trail (if plan says).
3. `time_stop` breached → close unless PnL > +1R with trail.
4. Plan `invalidation_rules` breached on **closed** candles (e.g., `close_above 1270 4h`) → close.
5. Portfolio kill-switch state changed (BTC daily close vs stored levels) → execute mapped actions.
6. Unfilled entry older than `entry_valid_hours` → cancel.

7. For every position closed since the last run: compute realised R, fees, funding, BTC return over the same window; run the **Post-mortem** call (batched) and store `postmortems`; send the closed-position Telegram event.

Then LLM Reviewer (one call, all positions) with fresh dossiers:

| Action | Allowed when | Gate |
|---|---|---|
| `HOLD` | thesis intact/weakened and PnL > −0.5R or thesis intact | — |
| `CLOSE` | thesis broken; or momentum gone with PnL > +1R and TP2 far | mandatory if `thesis_status=broken` and PnL ≤ −0.5R |
| `PARTIAL_CLOSE(pct)` | pct ∈ {25,50,75}; PnL > 0 | — |
| `MOVE_SL(px)` | only toward profit; must stay ≥ 0.5×ATR from price | reject if widening |
| `ACTIVATE_TRAIL(callback)` | any time (the default trail exists regardless; this tightens or activates early) | callback within [1%, 4%]; may not loosen an active trail |

The Reviewer sees the *original thesis and invalidation rules* so it judges "is the reason for this trade still true?" rather than re-deciding from scratch (the prototype reviewer only saw PnL + technicals — it would exit winners on any dip and hold losers whose thesis died).

## 9. Kill-switches (stored, evaluated by Watchdog + Review)

Plan-level (LLM proposes in `invalidation_rules`, gate normalises): `price_close_above/below(level, tf)`, `funding_flip(sign)`, `event(kind, outcome)`, `real_yield_above(2.50)`.
Portfolio-level (config): equity DD from the account's own 30d high ≥ 15% → close all + HALT (demo and live never share a peak — one journal holds both, F-149); watchdog can't reach exchange for 3 checks → alert; margin ratio < 200% on any position → reduce.

## 10. Core schemas (Pydantic v2; full versions in `LLM_AGENTS.md`)

```python
class FactorScores(BaseModel):
    fundamental: conint(ge=-2, le=2); onchain: conint(ge=-2, le=2); macro: conint(ge=-2, le=2)
    catalyst: conint(ge=-2, le=2);   technical: conint(ge=-2, le=2)
    evidence: dict[str, list[str]]     # factor -> dossier field paths cited
    @property
    def total(self): ...

class TradePlan(BaseModel):
    symbol: str; side: Literal["long","short"]
    thesis: constr(max_length=400); factor_scores: FactorScores
    entry: Entry(kind: Literal["limit","market"], zone_low: float, zone_high: float, valid_hours: int=12)
    stop: Stop(price: float, basis: Literal["structure","atr"], note: str)
    targets: list[Target(price: float, close_pct: int)]     # sum close_pct == 100
    invalidation_rules: list[Rule]; time_stop_hours: conint(le=168)
    suggested_leverage: conint(ge=2, le=10); confidence_pct: conint(ge=0, le=100)
    risks: list[str]; data_gaps: list[str]; event_thesis: bool=False; urgent: bool=False

class GateResult(BaseModel):
    status: Literal["APPROVED","ADJUSTED","REJECTED"]; reasons: list[str]
    sized: SizedOrder | None    # notional, leverage, margin, contracts, sl, tps, clOrdId

class ReviewAction(BaseModel):
    position_id: str; action: Literal["HOLD","CLOSE","PARTIAL_CLOSE","MOVE_SL","ACTIVATE_TRAIL"]
    pct: int | None; new_sl: float | None; callback_pct: float | None
    thesis_status: Literal["intact","weakened","broken"]; reason: constr(max_length=300)
```

## 11. Demo exploration profile (2026-10-26, owner's grilling decisions)

**What and why.** On the **demo account only** (`OKX_DEMO=true`, i.e. `settings.venue == "demo"`), the
decision rules are relaxed so the AI opens as many demo trades as the fixed safety rails allow. The
purpose is to show AI-opened demo trades to colleagues, and to collect demo evidence that will set
future values — these numbers are not a claim about what is right for real money. The profile stays
constant until owner changes it. **The live account is unchanged**: every live number below is the
constant the gate enforced before this section existed, and a test per rule proves a plan that
passes on demo still fails on live (`tests/runs/test_demo_exploration.py`).

**One place decides.** `argus/gate/limits.py` holds a frozen `DecisionLimits` with two instances,
`LIVE_CONSERVATIVE` and `DEMO_EXPLORATION`; `limits_for(venue)` returns the demo profile only for
the exact string `"demo"` (anything else fails safe to live), and `Settings.limits` calls it with
the venue from `OKX_DEMO`. No LLM output reaches it (non-negotiable #3): the plan schemas forbid
extra keys, and every function that takes `limits` defaults to `LIVE_CONSERVATIVE`. The old
constants (`rules.MIN_SCORE`, `protections.MAX_ENTRIES_PER_RUN`, …) keep their names and values and
are bound to the live instance, so the `risk_profiles.yaml` pins still hold.

| # | Rule | Live (unchanged) | Demo |
|---|---|---|---|
| Q1 | gate 1 score | MIN_SCORE 4, counter-regime 6 | 2, counter-regime 4 |
| Q2 | gate 4 R:R | TP1 ≥ 1.5R, blended ≥ 2.0R | TP1 ≥ **0.5R**, blended ≥ **0.8R** (lowered from 1.0R / 1.3R the same day: the first demo plans were rejected at TP1 0.80R and 0.42R) |
| Q3 | entries per run / per day | 2 / 3 | 3 / 8 (Trader batch may return 3 plans) |
| Q3 | max open positions | 4 (aggressive profile 2) | 6 (flat; replaces the profile cap) |
| Q3 | MAX_OPEN_RISK_PCT | 5% | 8% (also the Risk Debate's heat budget) |
| Q4 | CHOP entries | limit-only | market entries allowed; the CHOP 0.5x size multiplier stays |
| Q5 | Red Team | drops a plan at severity ≥ 4 | drops only at 5; severity 3–4 = ×0.5 |
| Q5 | Consensus unavailable (every model failed) | plan dropped | plan proceeds; a real disagreement (side mismatch, stop divergence, the second opinion declining) still vetoes |
| Q5 | Risk Debate "decline" | drops the plan | ×0.5, like "reduce"; "reduce" stays ×0.5 |
| Q6 | manager slots (UTC) | 00:30, 12:30 | 00:30, 04:30, 09:30, 12:30, 16:30, 20:30 |
| Q7 | consecutive-loss pause | 3 | 5 |

**Risk Debate on demo.** `riskdebate.md` still tells the debater that overflowing the heat budget
means "decline"; on demo that "decline" becomes ×0.5, and the trade stays bounded by gate 6's 8%
open-risk cap, which the debater's heat flags also use as their budget.

**The Trader prompts follow the profile.** `trader.md` (also the consensus second opinion) and
`trader_batch.md` name the R:R bars from the active profile: live renders exactly the old text
(1.5R / 2R / 2.0), demo renders 0.5R / 0.8R. The numbers stay in the prompts on purpose — F-091 and
F-099 showed that removing them brings back plans the gate can never approve; only *which* numbers
is venue-specific. The ATR band text is the same on both.

**Q6, the schedule.** `argus-manager.timer` fires at 00:30, 04:30, 09:30, 12:30, 16:30 and 20:30 UTC
and the unit runs `argus manager --llm --scheduled`. The fourth demo slot is 09:30, not 08:30, to
stay clear of the daily `argus-eval-models` run at 08:30, which shares the provider limits. A
scheduled run on the **live** account that starts inside a demo-only slot's window (10 min before to
10 min after 04:30/09:30/16:30/20:30, read at process start, before any lock wait) prints "the live
account runs only at 00:30/12:30 UTC" and exits 0 before any lock or venue call
(`argus/runs/schedule.py`). A hand-run `argus manager` (no `--scheduled`) is never checked. A
`Persistent=true` catch-up after downtime (a reboot at 04:40, say) lands outside the window and
runs on live as it always did.

**What stays fixed on demo too (Q7)** — none of these is a field of `DecisionLimits`:
- an exchange-side stop on every position, and stops only tighten;
- the liquidation buffer (2.5× stop) and the leverage cap;
- the 15% portfolio kill switch;
- the 3% daily and 6% weekly loss halts;
- the 6 h event blackout (and EVENT_RISK stays limit-only);
- the stop ATR band (1.0–3.5× ATR14);
- per-trade risk sizing (the small-account steps) and the 50% size floor for gate adjustments;
- the 168 h time-stop maximum;
- the gross-exposure cap (150%) and the two-same-side-high-beta rule.

**Extension, same day — the Trader's no-trade stance (`explore`).** With the demo numbers live,
the first two demo cycles still returned 0 plans: the Trader prompts closed with "if nothing clears
the bar, return no plans — that is a good outcome", and the model cited CHOP multipliers and "risk
capacity" as reasons to decline. Under owner's "reasonably relax" instruction, `DEMO_EXPLORATION.explore`
replaces that paragraph on demo only: return the best candidates whenever net evidence is positive
and there is a structural stop, express doubt in the scores rather than by withholding the plan,
regime multipliers size a trade rather than veto it, and capacity is the Risk Gate's job. Live
renders the old paragraph word for word (tested). Stop-placement and ATR-band text are unchanged
(F-091). It reverts with the same switch below (`explore=False` in the live profile).

**Extension, same day — a one-sided consensus halves on demo (owner, "reasonably relax").** The first
demo plans to reach consensus (ETH, BNB, ARB) were all vetoed as `one_sided`: the second opinion
returned no trade. On demo (`consensus_one_sided_factor = 0.5`) that halves the size, like a
risk-debate "decline"; a disagreement on side or stop (`conflict`, `divergent`) still vetoes. Live
keeps the veto (factor 0.0). Same revert switch.

**Extension, same day — demo target repair (`repair_targets_to_min_rr`, `argus/gate/repair.py`).**
With the bar at 0.5R the Trader still put TP1 at the nearest level (ENA 0.15R at 16:30 UTC, after
passing every agent). On demo the manager now moves targets that sit inside the floor out to it
(plus 0.01R) before the gate; stop, entry, side, close percentages and size are unchanged, so the
risk is unchanged and only the reward waited for grows. The journalled plan carries the repaired
targets and `manager.targets_repaired` logs the before/after R. Live never repairs.

**2026-10-29 (owner) — demo stop repair (`repair_stops_to_atr_band`, `argus/gate/repair.py`).**
After #43, 5 of 6 demo plans died at gate 3 with stops 4.9–6.6% from entry against a ~4.5–5%
ceiling (3.5×ATR). The ATR band itself stays fixed (grilling Q7); what changes is that on demo a
stop *wider* than the band moves in to 3.45×ATR, but only if it is then still beyond the 20-bar
swing — otherwise the gate rejects as before. A stop is never widened, and a too-tight stop is not
touched. It runs before the target repair, so targets are measured in the new R; sizing then works
from the tighter stop (same dollar risk budget, more contracts, a stop hit more easily).
`manager.stop_repaired` logs before/after. Live never repairs. Revert: same switch.

**2026-10-27 (owner) — both accounts run every 4 hours.** "Keep 4h AI runs going forward for both
demo and live": `DEMO_ONLY_SLOTS` is now empty (`argus/runs/schedule.py`), so the live account also
runs at 00:30, 04:30, 09:30, 12:30, 16:30 and 20:30 UTC. The live *rules* are unchanged — only the
cadence. Restore the old live cadence by putting the four extra times back in `DEMO_ONLY_SLOTS`.
Same day, demo only: the Trader prompt says code repairs a too-close target, after the overnight
runs still dropped every setup as "not meeting 0.8R" (trader v11, trader_batch v4).

**2026-10-27 (owner) — red team severity 5 on demo shrinks to ×0.25** (`redteam_top_severity_factor`)
instead of dropping the plan; severity 3–4 stays ×0.5. Live still drops at severity 4.

**How to revert — the single switch.** In `argus/gate/limits.py` set
`DEMO_PROFILE = LIVE_CONSERVATIVE`. Every demo rule, both prompts and the Trader schema go back to
the live values; nothing else needs to change. The four demo-only manager slots keep running on
demo (conservatively) until the timer line is put back to `00,12:30`.
