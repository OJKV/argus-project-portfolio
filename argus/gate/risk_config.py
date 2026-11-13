"""`config/risk_profiles.yaml`, typed and validated (review G-N5 / G-17 / G-5).

The file used to be loaded as a bare dict and almost none of it was read: `risk_pct: 35` was
accepted, `small_account_steps: []` meant "every step", a misspelt key vanished silently, and an
operator who "tightened" `max_positions` or `daily_loss_halt_pct` before going live changed nothing.

Every key that survives is one of two kinds:

* **read** — the small-account steps, each profile's step list and `max_positions`, the
  small-account thresholds that pick the profile, the resting-entry sweep's two numbers, and the
  leverage ceiling the steps are checked against;
* **pinned** — it mirrors a constant the gate enforces in code. A file whose value differs from the
  constant is refused at load, so editing the YAML can no longer look like a change it is not.
  Changing one of those numbers is a code change, made on evidence through the grilling gate.

Keys that were neither (per-profile loss limits that disagreed with the code, trailing-stop
parameters nothing implemented) were removed from the file rather than left to mislead.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from argus.features.regime import BASE as REGIME_BASE
from argus.gate import fees, killswitch, protections, rules, sizing

ProfileName = Literal["conservative", "aggressive"]

# The largest per-trade risk DECISION_LOGIC §5.1 defines (step 3). A bigger step is a code change,
# not a typo the loader should accept — `risk_pct: 35` was.
MAX_STEP_RISK_PCT = 5.0

_STRICT = ConfigDict(extra="forbid", frozen=True)


class StepConfig(BaseModel):
    model_config = _STRICT

    risk_pct: float = Field(gt=0, le=MAX_STEP_RISK_PCT)
    max_lev: int = Field(ge=1)
    min_lev: int = Field(ge=1)
    max_notional_frac: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> StepConfig:
        if self.min_lev > self.max_lev:
            raise ValueError(f"min_lev {self.min_lev} > max_lev {self.max_lev}")
        return self


class CommonConfig(BaseModel):
    model_config = _STRICT

    max_leverage: int = Field(ge=1)
    liq_buffer_multiple: float
    stop_atr_band: tuple[float, float]
    min_rr_tp1: float
    min_rr_blended: float
    entry_valid_hours: float = Field(gt=0)
    entry_drift_cancel_pct: float = Field(gt=0)
    fee_viability_max_ratio: float
    funding_cost_max_ratio: float
    event_blackout_hours: float
    max_entries_per_run: int
    max_entries_per_day: int
    time_stop_hours_max: int
    daily_loss_halt_pct: float
    weekly_loss_halt_pct: float
    portfolio_dd_kill_pct: float
    consecutive_loss_pause: int


class ProfileConfig(BaseModel):
    model_config = _STRICT

    max_positions: int = Field(ge=1)
    # Non-empty: an empty list used to mean "all steps" (`or steps.keys()`), the opposite of what
    # someone emptying it would intend.
    small_account_steps: list[int] = Field(min_length=1)


class RiskConfig(BaseModel):
    model_config = _STRICT

    common: CommonConfig
    conservative: ProfileConfig
    aggressive: ProfileConfig
    small_account_steps: dict[int, StepConfig] = Field(min_length=1)
    min_viable_notional_usd: float = Field(gt=0)
    small_account_equity_usd: float = Field(gt=0)
    small_account_exit_days: int = Field(ge=1)
    regime_multipliers: dict[str, float]

    def profile(self, name: ProfileName) -> ProfileConfig:
        return self.conservative if name == "conservative" else self.aggressive

    @model_validator(mode="after")
    def _consistent(self) -> RiskConfig:
        for name in ("conservative", "aggressive"):
            unknown = set(getattr(self, name).small_account_steps) - set(self.small_account_steps)
            if unknown:
                raise ValueError(f"{name} lists undefined small-account steps {sorted(unknown)}")
        for number, step in self.small_account_steps.items():
            if step.max_lev > self.common.max_leverage:
                raise ValueError(
                    f"step {number} max_lev {step.max_lev} > common.max_leverage "
                    f"{self.common.max_leverage}"
                )
        drift = [
            f"{key}: file says {found!r}, the gate enforces {enforced!r}"
            for key, found, enforced in _pins(self)
            if found != enforced
        ]
        if drift:
            raise ValueError(
                "risk_profiles.yaml disagrees with the numbers the code enforces — editing the "
                "file would change nothing. Change the code (with evidence) or restore the file: "
                + "; ".join(drift)
            )
        return self


def _pins(c: RiskConfig) -> list[tuple[str, Any, Any]]:
    """(key, value in the file, constant the code enforces) for every pinned key."""
    k = c.common
    return [
        ("common.liq_buffer_multiple", k.liq_buffer_multiple, rules.LIQ_BUFFER_MULTIPLE),
        ("common.stop_atr_band", tuple(k.stop_atr_band), rules.STOP_ATR_BAND),
        ("common.min_rr_tp1", k.min_rr_tp1, rules.MIN_RR_TP1),
        ("common.min_rr_blended", k.min_rr_blended, rules.MIN_RR_BLENDED),
        ("common.fee_viability_max_ratio", k.fee_viability_max_ratio, fees.MAX_COST_SHARE_OF_TP1),
        (
            "common.funding_cost_max_ratio",
            k.funding_cost_max_ratio,
            rules.MAX_FUNDING_SHARE_OF_TP1,
        ),
        ("common.event_blackout_hours", k.event_blackout_hours, rules.EVENT_BLACKOUT_HOURS),
        ("common.max_entries_per_run", k.max_entries_per_run, protections.MAX_ENTRIES_PER_RUN),
        ("common.max_entries_per_day", k.max_entries_per_day, protections.MAX_ENTRIES_PER_DAY),
        ("common.time_stop_hours_max", k.time_stop_hours_max, rules.MAX_TIME_STOP_HOURS),
        (
            "common.daily_loss_halt_pct",
            k.daily_loss_halt_pct,
            -protections.DAILY_LOSS_LIMIT_PCT,
        ),
        (
            "common.weekly_loss_halt_pct",
            k.weekly_loss_halt_pct,
            -protections.WEEKLY_LOSS_LIMIT_PCT,
        ),
        (
            "common.portfolio_dd_kill_pct",
            k.portfolio_dd_kill_pct,
            killswitch.PORTFOLIO_DD_LIMIT_PCT,
        ),
        (
            "common.consecutive_loss_pause",
            k.consecutive_loss_pause,
            protections.CONSECUTIVE_LOSS_LIMIT,
        ),
        ("min_viable_notional_usd", c.min_viable_notional_usd, sizing.MIN_VIABLE_NOTIONAL_USD),
        (
            "regime_multipliers",
            c.regime_multipliers,
            {label: mult for label, (mult, _limit_only) in REGIME_BASE.items()},
        ),
    ]


def parse(raw: dict[str, Any]) -> RiskConfig:
    """Validate a loaded YAML mapping. Raises `pydantic.ValidationError` on anything wrong."""
    return RiskConfig.model_validate(raw)


def select_profile(
    config: RiskConfig,
    equity: float,
    override: str = "",
    lowest_recent_equity: float | None = None,
) -> ProfileName:
    """Which profile applies (DECISION_LOGIC §5.1; review G-5).

    `_default_profile()` returned aggressive at any equity. Now: an explicit RISK_PROFILE wins;
    otherwise aggressive below the small-account threshold, and conservative only once equity has
    stayed at or above it for `small_account_exit_days` — `lowest_recent_equity` is the lowest
    equity over that window, or None when the journal cannot show the whole window.
    """
    if override in ("conservative", "aggressive"):
        return "conservative" if override == "conservative" else "aggressive"
    if override:
        raise ValueError(f"unknown risk profile {override!r}")
    threshold = config.small_account_equity_usd
    if equity < threshold or lowest_recent_equity is None or lowest_recent_equity < threshold:
        return "aggressive"
    return "conservative"


def step_policies(
    config: RiskConfig, profile: ProfileName, equity: float
) -> tuple[sizing.StepPolicy, ...]:
    """The profile's small-account steps, in order.

    Steps only the aggressive profile has (step 3, 5 % risk) are allowed only while equity is under
    the small-account threshold — even when RISK_PROFILE=aggressive is forced (§5.1: "step 3 &
    aggressive only < $500").
    """
    allowed = set(config.profile(profile).small_account_steps)
    if equity >= config.small_account_equity_usd:
        allowed &= set(config.conservative.small_account_steps)
    return tuple(
        sizing.StepPolicy(
            step=number,
            risk_pct=step.risk_pct,
            max_lev=step.max_lev,
            min_lev=step.min_lev,
            max_notional_frac=step.max_notional_frac,
        )
        for number, step in sorted(config.small_account_steps.items())
        if number in allowed
    )


def max_positions(config: RiskConfig, profile: ProfileName, equity: float) -> int:
    """The profile's position cap, never above aggressive's while equity is under the threshold.

    §5.1: "<= 2 open positions while equity < $500" never relaxes. `step_policies` already guards
    step 3 by equity; this is the matching guard for the cap, because `/risk conservative` is the
    documented operator action under $500 and conservative's own cap is larger.
    """
    cap = config.profile(profile).max_positions
    if equity < config.small_account_equity_usd:
        cap = min(cap, config.aggressive.max_positions)
    return cap
