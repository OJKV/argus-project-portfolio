"""`argus doctor` — connectivity, keys presence, config sanity. Never prints secret values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from argus.config import (
    KNOWLEDGE_DIR,
    LIVE_VENUE_WORD,
    get_settings,
    load_yaml,
    models_config,
    risk_config,
    universe_config,
    unknown_env_keys,
)

# Endpoints that answer 401/403 without a key: reachability is still proven.
KEY_GATED = {"coinalyze", "cryptopanic"}

PUBLIC_CHECKS: dict[str, str] = {
    "okx.public": "{okx_host}/api/v5/public/time",
    "bybit.kz.public": "https://api.bybit.kz/v5/market/time",
    "binance.futures.public": "https://fapi.binance.com/fapi/v1/time",
    "coinalyze": "https://api.coinalyze.net/v1/exchanges",
    "defillama": "https://api.llama.fi/v2/chains",
    "coingecko": "https://api.coingecko.com/api/v3/ping",
    "fear_greed": "https://api.alternative.me/fng/?limit=1",
    "hyperliquid": "https://api.hyperliquid.xyz/info",
    "deribit": "https://www.deribit.com/api/v2/public/get_time",
    "forexfactory": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "polymarket": "https://gamma-api.polymarket.com/markets?limit=1",
    "kalshi": "https://api.elections.kalshi.com/trade-api/v2/markets?limit=1",
    "mempool": "https://mempool.space/api/v1/fees/recommended",
    "cryptopanic": "https://cryptopanic.com/api/v1/posts/",
    "scira": "https://api.scira.ai/docs",
}

KEY_CHECKS: dict[str, str] = {
    "OKX demo key": "okx_demo_api_key",
    "OKX demo secret": "okx_demo_api_secret",
    "OKX demo passphrase": "okx_demo_api_passphrase",
    "OKX live key": "okx_api_key",
    "OKX live secret": "okx_api_secret",
    "OKX live passphrase": "okx_api_passphrase",
    "Bybit KZ key": "bybit_kz_api_key",
    "OpenRouter": "openrouter_api_key",
    "Gemini": "gemini_api_key",
    "Groq": "groq_api_key",
    "Mistral": "mistral_api_key",
    "NVIDIA NIM": "nvidia_api_key",
    "Cloudflare": "cloudflare_api_token",
    "Zhipu": "zhipu_api_key",
    "DeepSeek (paid fallback 1)": "deepseek_api_key",
    "OpenAI (paid fallback 2)": "openai_api_key",
    "Coinalyze": "coinalyze_api_key",
    "CoinGecko demo": "coingecko_demo_api_key",
    "CMC": "cmc_api_key",
    "CryptoPanic": "cryptopanic_api_key",
    "FRED": "fred_api_key",
    "Etherscan": "etherscan_api_key",
    "Dune": "dune_api_key",
    "Scira": "scira_api_key",
    "EDGAR user agent": "edgar_user_agent",
    "Telegram token": "telegram_bot_token",
    "Telegram chat": "telegram_chat_id",
    "Logfire": "logfire_token",
    "Healthchecks": "healthchecks_url",
}


# Keys whose absence is a known, accepted state rather than a failure:
#   CryptoPanic — free Developer plan reportedly discontinued in 2026 (paid-only now);
#   CMC — cross-check source only; Dune — saved queries not written yet;
#   Telegram + Healthchecks — optional for research; Telegram is REQUIRED once armed (below);
#   Bybit KZ — public endpoints need no key; a key only matters for post-MVP execution (R2).
OPTIONAL_KEYS = {
    "CryptoPanic",
    # Parked by owner before it was ever configured, so requiring it made `doctor` exit non-zero
    # on a healthy box — every key check reported FAIL/missing while every other row said OK.
    "NVIDIA NIM",
    # Every provider below is one of several in its tier. The router demotes a family it cannot
    # reach and the run continues; a missing key is a smaller roster, not a broken system. What
    # must never be optional is the exchange, and that is checked separately.
    "Zhipu",
    "CMC",
    "Dune",
    "Telegram token",
    "Telegram chat",
    "Healthchecks",
    "Bybit KZ key",
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


# Optional for research, required once armed: every alert an armed run raises — repair failed,
# kill-switch, a failed unit — travels on Telegram, and an armed box with no channel fails
# silently (F-141: 364 manager_stale alerts into a dead channel; review 2026-10-07, J-N13).
REQUIRED_WHEN_ARMED = {"Telegram token", "Telegram chat"}


def check_keys() -> list[Check]:
    s = get_settings()
    out: list[Check] = []
    optional = OPTIONAL_KEYS - REQUIRED_WHEN_ARMED if s.is_live else OPTIONAL_KEYS
    for label, attr in KEY_CHECKS.items():
        if s.has(attr):
            out.append(Check(label, True, "set"))
        elif label in REQUIRED_WHEN_ARMED and s.is_live:
            out.append(Check(label, False, "missing — required while armed: alerts go nowhere"))
        elif label in optional:
            out.append(Check(label, True, "optional — not set"))
        else:
            out.append(Check(label, False, "missing"))
    return out


def check_public(timeout: float = 8.0) -> list[Check]:
    out: list[Check] = []
    headers = {"User-Agent": "argus-doctor/0.1 (+https://github.com)"}
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        okx_host = get_settings().okx_public_host.rstrip("/")
        for name, template in PUBLIC_CHECKS.items():
            url = template.format(okx_host=okx_host)
            try:
                if name == "hyperliquid":
                    r = client.post(url, json={"type": "meta"})
                else:
                    r = client.get(url)
                key_gated = name in KEY_GATED and r.status_code in (401, 403)
                ok = r.status_code < 400 or key_gated
                detail = f"HTTP {r.status_code}" + (" (reachable, needs key)" if key_gated else "")
                out.append(Check(name, ok, detail))
            except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
                out.append(Check(name, False, type(exc).__name__))
    return out


def logfire_base(token: str) -> str:
    """Logfire's API host for this token: EU tokens carry `_eu_`, everything else is US."""
    return (
        "https://logfire-eu.pydantic.dev" if "_eu_" in token else "https://logfire-us.pydantic.dev"
    )


def check_logfire(timeout: float = 8.0) -> list[Check]:
    """Ask Logfire which project the token writes to (`GET /v1/info`), so a token for the wrong
    project — spans landing somewhere nobody looks — shows up here.

    The token goes in the Authorization header only. It is never part of a URL, a detail string
    or a log line: failures report the HTTP status or the exception class, nothing else.
    """
    token = get_settings().logfire_token.get_secret_value()
    if not token:
        return []  # check_keys already says it is missing
    name = "Logfire project"
    try:
        r = httpx.get(
            f"{logfire_base(token)}/v1/info", headers={"Authorization": token}, timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        return [Check(name, False, type(exc).__name__)]
    if r.status_code != 200:
        return [Check(name, False, f"HTTP {r.status_code}")]
    try:
        info = r.json()
        url = str(info["project_url"])
    except Exception:  # noqa: BLE001 - a non-JSON 200 is a failed check, not a crash
        return [Check(name, False, "HTTP 200 but no project_url in the reply")]
    project = info.get("project_name")
    return [Check(name, True, f"{project} — {url}" if project else url)]


def check_config() -> list[Check]:
    out: list[Check] = []
    typos = unknown_env_keys()
    out.append(
        Check(
            ".env keys",
            not typos,
            "no misspelt ARGUS keys"
            if not typos
            else "ignored, looks like a typo: "
            + ", ".join(f"{k} (meant {v}?)" for k, v in typos.items()),
        )
    )
    try:
        # The same strict parse every manager run does (review G-N5): a file that only has the
        # right top-level keys can still close every run FAILED, and doctor must say so first.
        from argus.gate.risk_config import parse as parse_risk_config

        parsed = parse_risk_config(risk_config())
        out.append(
            Check(
                "risk_profiles.yaml",
                True,
                f"valid — {len(parsed.small_account_steps)} small-account steps",
            )
        )
    except Exception as exc:  # noqa: BLE001
        out.append(Check("risk_profiles.yaml", False, f"{type(exc).__name__}: {exc}"[:500]))
    try:
        mc = models_config()
        n = sum(len(v) for v in mc.get("tiers", {}).values())
        out.append(Check("models.yaml", n > 0, f"{n} tier entries"))
    except Exception as exc:  # noqa: BLE001
        out.append(Check("models.yaml", False, str(exc)))
    try:
        uc = universe_config()
        core = uc.get("core", [])
        # Shape, not a head count: `== 12` failed the moment the list was edited (J-N13). What
        # matters is that there is a core and that no asset is listed twice.
        symbols = [str(a.get("symbol", "")) for a in core]
        ok = bool(symbols) and all(symbols) and len(set(symbols)) == len(symbols)
        detail = f"{len(core)} core assets" + ("" if ok else " — empty, unnamed or duplicated")
        out.append(Check("universe.yaml", ok, detail))
    except Exception as exc:  # noqa: BLE001
        out.append(Check("universe.yaml", False, str(exc)))
    for f in ("whitelist.yaml", "blocklist.yaml", "catalysts.yaml", "tokenomics.yaml"):
        try:
            load_yaml(f, KNOWLEDGE_DIR)
            out.append(Check(f, True, "loaded"))
        except Exception as exc:  # noqa: BLE001
            out.append(Check(f, False, str(exc)))
    s = get_settings()
    armed = "ARMED" if s.is_live else "not armed"
    # P-N2: `approve` reads like a mode that waits for a yes. It does not, so the row says so.
    same = " (approve == auto: no approval step yet)" if s.mode == "approve" else ""
    out.append(Check("mode", True, f"{s.mode}{same} — {armed}, {s.venue} account"))
    # Visible on purpose: LIVE_TRADING is one of the two interlocks guarding real money, and
    # leaving it armed "ready for later" quietly removes one of them.
    armed = s.live_trading == "I_UNDERSTAND"
    out.append(
        Check(
            "live-trading interlock",
            True,
            "ARMED (I_UNDERSTAND)" if armed else "not armed — safe",
        )
    )
    # The second interlock, for real money only (P-N5). Shown even on demo, so a word left set
    # "ready for later" is visible before anyone flips OKX_DEMO.
    money = s.live_venue == LIVE_VENUE_WORD
    out.append(
        Check(
            "live-venue interlock",
            True,
            "SET — the live account may be armed" if money else "not set — live account refused",
        )
    )
    out.append(
        Check(
            "HALT file",
            not s.halt_file.exists(),
            "absent" if not s.halt_file.exists() else "PRESENT — runs will refuse to trade",
        )
    )
    out.extend(check_runs())
    return out


def check_runs(now: datetime | None = None) -> list[Check]:
    """Is the scheduled manager actually running? (review 2026-10-07, J-N13; F-141)

    Read-only, and it never creates a journal (`open_journal_readonly`). A gap only fails when
    armed — with MODE=demo the scheduled manager exits 2 by design, so silence there is expected.

    The same read and threshold as the watchdog's manager_stale alert — `alerts.last_run_at`, so
    only this account's OK runs count. Its own SQL counted a demo run, or a FAILED one, and passed
    while the live alert fired (F-153).
    """
    from sqlalchemy.exc import SQLAlchemyError

    from argus.journal.db import open_journal_readonly
    from argus.observability.alerts import last_run_at, stale_manager

    s = get_settings()
    path = s.journal_path
    if not path.exists():
        return [Check("last manager run", True, "no journal yet")]
    try:
        engine = open_journal_readonly(path)
        try:
            last = last_run_at(engine, "manager", venue=s.venue)
        finally:
            engine.dispose()
    except (SQLAlchemyError, OSError) as exc:  # OSError: not a file, gone, or no permission
        return [Check("last manager run", False, f"journal unreadable: {exc}")]
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    alert = stale_manager(last, now or datetime.now(UTC), s.manager_max_gap_hours)
    if alert is None:
        return [Check("last manager run", True, f"{last:%Y-%m-%d %H:%M} UTC")]
    if not s.is_live:
        return [Check("last manager run", True, f"{alert.message} — not armed, so expected")]
    return [Check("last manager run", False, alert.message)]


def median_daily_turnover(rows: list[list[str]], days: int = 7) -> float | None:
    """Median USDT turnover over the last `days` *closed* daily candles.

    OKX candle rows are newest-first: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm];
    confirm == "0" marks the still-forming candle, which is excluded.
    """
    closed = [float(row[7]) for row in rows if len(row) > 8 and row[8] == "1"][:days]
    if not closed:
        return None
    ordered = sorted(closed)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def check_universe_okx(timeout: float = 10.0) -> list[Check]:
    """Verify every core symbol has an OKX USDT swap meeting the core liquidity floor."""
    out: list[Check] = []
    host = get_settings().okx_public_host.rstrip("/")
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{host}/api/v5/public/instruments", params={"instType": "SWAP"})
            r.raise_for_status()
            data = {row["instId"]: row for row in r.json().get("data", [])}
    except Exception as exc:  # noqa: BLE001
        return [Check("okx.instruments", False, type(exc).__name__)]
    universe = universe_config()
    floor = universe.get("core_liquidity_floor", {})
    min_turnover = float(floor.get("min_turnover_24h_usd", 0))
    min_lev = float(floor.get("min_max_leverage", 10))
    max_spread = float(floor.get("max_spread_pct", 100))
    tickers: dict[str, dict[str, str]] = {}
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{host}/api/v5/market/tickers", params={"instType": "SWAP"})
            r.raise_for_status()
            tickers = {row["instId"]: row for row in r.json().get("data", [])}
    except Exception as exc:  # noqa: BLE001
        out.append(Check("okx.tickers", False, type(exc).__name__))
    wl_floor = universe.get("whitelist_liquidity_floor", {})
    whitelist = load_yaml("whitelist.yaml", KNOWLEDGE_DIR).get("entries") or []
    groups = (
        ("core", universe.get("core", []), min_turnover, min_lev, max_spread),
        (
            "whitelist",
            whitelist,
            float(wl_floor.get("min_turnover_24h_usd", 0)),
            float(wl_floor.get("min_max_leverage", 10)),
            float(wl_floor.get("max_spread_pct", 100)),
        ),
    )
    for group, assets, floor_turnover, floor_lev, cap_spread in groups:
        for asset in assets:
            label = asset["symbol"] if group == "core" else f"{asset['symbol']} (wl)"
            inst = data.get(asset["okx"])
            if inst is None:
                out.append(Check(label, False, f"{asset['okx']} not listed"))
                continue
            lev = float(inst.get("lever", "0"))
            tick = tickers.get(asset["okx"], {})
            last = float(tick.get("last") or 0)
            turnover_24h = float(tick.get("volCcy24h") or 0) * last
            try:
                with httpx.Client(timeout=timeout) as client:
                    r = client.get(
                        f"{host}/api/v5/market/candles",
                        params={"instId": asset["okx"], "bar": "1D", "limit": 8},
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    r.raise_for_status()
                    median = median_daily_turnover(r.json().get("data", []))
            except Exception:  # noqa: BLE001 - fall back to the 24h figure, and say so
                median = None
            turnover = median if median is not None else turnover_24h
            basis = "7d median" if median is not None else "24h (7d unavailable)"
            ask, bid = float(tick.get("askPx") or 0), float(tick.get("bidPx") or 0)
            spread = (ask - bid) / ask * 100 if ask else 100.0
            ok = lev >= floor_lev and turnover >= floor_turnover and spread <= cap_spread
            out.append(
                Check(
                    label,
                    ok,
                    f"${turnover / 1e6:,.0f}M {basis} (24h ${turnover_24h / 1e6:,.0f}M), "
                    f"lev {lev:.0f}, spread {spread:.3f}%",
                )
            )
    return out


# ── LLM model reachability (`argus doctor --models`) ─────────────────────────

MODEL_PING_PROMPT = "Reply with the single word: ok"
MODEL_PING_GAP_S = 2.0
# A reasoning model spends its budget thinking before it emits a single visible token, so a
# 5-token ceiling made gemini-3.5-flash and gpt-5-mini look broken when they were only
# mid-thought ("token limit exceeded before any response was generated"). 256 is still a
# rounding error in cost and leaves room for the thinking block.
MODEL_PING_MAX_TOKENS = 256


def _iter_model_entries() -> list[tuple[str, str]]:
    """(provider, model_name) for every entry in config/models.yaml tiers, de-duplicated."""
    seen: set[tuple[str, str]] = set()
    entries: list[tuple[str, str]] = []
    for tier_entries in models_config().get("tiers", {}).values():
        for entry in tier_entries:
            provider, _, model_name = str(entry).partition("/")
            if not model_name:
                continue
            key = (provider, model_name)
            if key not in seen:
                seen.add(key)
                entries.append(key)
    return entries


def _build_model(provider: str, model_name: str) -> Any:
    """Model for a provider entry: native for Gemini/Mistral, OpenAI-compatible otherwise."""
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.models.mistral import MistralModel
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.google import GoogleProvider
    from pydantic_ai.providers.mistral import MistralProvider
    from pydantic_ai.providers.openai import OpenAIProvider

    s = get_settings()
    cfg = models_config()["providers"][provider]
    api_key = getattr(s, cfg["env"].lower()).get_secret_value()
    if provider == "gemini":
        return GoogleModel(model_name, provider=GoogleProvider(api_key=api_key))
    if provider == "mistral":
        return MistralModel(model_name, provider=MistralProvider(api_key=api_key))
    base_url = str(cfg["base_url"])
    if "{account}" in base_url:
        base_url = base_url.format(account=s.cloudflare_account_id)
    return OpenAIChatModel(model_name, provider=OpenAIProvider(base_url=base_url, api_key=api_key))


def _classify(exc: Exception) -> str:
    """Map a provider exception to reachable / model-not-found / 429 / auth error."""
    from pydantic_ai.exceptions import ModelHTTPError

    if isinstance(exc, ModelHTTPError):
        status = exc.status_code
        if status == 429:
            return "429 rate-limited"
        if status in (401, 403):
            return "auth error"
        if status == 404:
            return "model-not-found"
        body = str(exc.body or "")[:80].replace("\n", " ")
        return f"HTTP {status} {body}".strip()
    text = str(exc)[:80].replace("\n", " ")
    return f"{type(exc).__name__}: {text}"


def check_models(timeout: float = 20.0, gap_s: float = MODEL_PING_GAP_S) -> list[Check]:
    """Send a short ping to every configured model. Never prints a key."""
    import time

    from pydantic_ai import Agent

    s = get_settings()
    providers = models_config().get("providers", {})
    out: list[Check] = []
    first = True
    for provider, model_name in _iter_model_entries():
        label = f"{provider}/{model_name}"
        cfg = providers.get(provider)
        if cfg is None:
            out.append(Check(label, False, "provider missing from models.yaml"))
            continue
        if not s.has(cfg["env"].lower()):
            out.append(Check(label, False, f"no key ({cfg['env']} unset)"))
            continue
        if provider == "cloudflare" and not s.cloudflare_account_id:
            out.append(Check(label, False, "no key (CLOUDFLARE_ACCOUNT_ID unset)"))
            continue
        if not first:
            time.sleep(gap_s)
        first = False
        try:
            model = _build_model(provider, model_name)
            result = Agent(model).run_sync(
                MODEL_PING_PROMPT,
                model_settings={
                    "max_tokens": MODEL_PING_MAX_TOKENS,
                    "temperature": 0.0,
                    "timeout": timeout,
                },
            )
            out.append(Check(label, True, f"reachable ({len(result.output.strip())} chars)"))
        except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
            detail = _classify(exc)
            # A busy free tier is the normal weather here, not a broken model. The router demotes
            # a provider that says 429 or 503 and the run carries on, so reporting these as
            # failures made a healthy box exit non-zero and put alarming red rows on the screen.
            # Anything else — auth, model-not-found, a dead endpoint — is still a failure.
            transient = detail.startswith("429") or detail.startswith("HTTP 503")
            out.append(
                Check(
                    label,
                    transient,
                    f"{detail} (transient — router demotes)" if transient else detail,
                )
            )
    return out


# ── Authenticated OKX check (`argus doctor --auth`) ──────────────────────────
# READ-ONLY. Never places, amends or cancels an order (AGENTS.md rule 1).


ACCOUNT_LEVELS = {
    "1": "Simple (spot only)",
    "2": "Single-currency margin",
    "3": "Multi-currency margin",
    "4": "Portfolio margin",
}


def account_mode_check(env: str, acct_lv: str | None) -> Check:
    """acctLv=1 cannot trade perpetuals: every order fails with sCode 51010."""
    label = ACCOUNT_LEVELS.get(str(acct_lv), "unknown")
    ok = str(acct_lv) in ("2", "3", "4")
    detail = f"acctLv={acct_lv} {label}"
    if not ok:
        detail += " — switch to Single-currency margin (OKX: Settings → Account mode)"
    return Check(f"okx.{env}.account mode", ok, detail)


def ip_binding_check(env: str, ips: str | None) -> Check:
    """OKX deletes a trade key that is NOT bound to an IP after 14 days without use (F-037).

    `ip` in /account/config lists the addresses bound to the calling key, "" when none. A key
    that silently lost its binding — re-cut in a hurry, edited in the console — would vanish two
    weeks into a quiet spell with nothing to say why. The addresses themselves are not printed:
    the count is the signal, and the addresses stay out of logs and terminals.
    """
    bound = [ip for ip in (ips or "").split(",") if ip.strip()]
    if bound:
        return Check(f"okx.{env}.ip binding", True, f"bound to {len(bound)} address(es)")
    return Check(
        f"okx.{env}.ip binding",
        False,
        "NOT IP-bound — OKX deletes an unbound trade key after 14 days idle; bind it to the VPS",
    )


# Algo order types a stop, take-profit or trail can rest as. OKX's pending-algo query takes one
# ordType per call, so each is read on its own.
ALGO_ORDER_TYPES = ("conditional", "oco", "trigger", "move_order_stop")


def flat_check(env: str, positions: int, orders: int, algos: int, armed_on_it: bool) -> Check:
    """The live account must start flat (P-10): ARGUS reconciles whatever it finds as its own.

    A position opened by hand before the flip would be stop-amended, trimmed and flattened by a
    system that never planned it, and counted in its risk as if it had. Before the box is armed on
    the account, anything there fails the row; once armed on it, positions are ARGUS's and
    expected. Counts only — no sizes, no prices.
    """
    name = f"okx.{env}.flat"
    if not (positions or orders or algos):
        return Check(name, True, "flat: no positions, open orders or algo orders")
    found = f"{positions} position(s), {orders} open order(s), {algos} algo order(s)"
    if armed_on_it:
        return Check(name, True, f"{found} — armed on {env}, so these are ARGUS's own")
    return Check(
        name,
        False,
        f"{found} on the {env} account before it is armed — it must start flat "
        "(safety review); close them in the OKX app",
    )


def _live_flat(client: Any, env: str) -> Check:
    """Read positions, resting orders and algo orders; any read error fails the row."""
    s = get_settings()
    try:
        rows = client.private_get_account_positions({})["data"]
        positions = sum(1 for p in rows if float(p.get("pos") or 0) != 0)
        orders = len(client.private_get_trade_orders_pending({})["data"])
        algos = sum(
            len(client.private_get_trade_orders_algo_pending({"ordType": kind})["data"])
            for kind in ALGO_ORDER_TYPES
        )
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        return Check(f"okx.{env}.flat", False, _auth_detail(exc))
    return flat_check(env, positions, orders, algos, s.is_live and s.venue == env)


def check_okx_auth(timeout_ms: int = 15000, env: str | None = None) -> list[Check]:
    """Account config, balance and fee rates. Read-only: nothing here places or cancels anything.

    `env` names the environment explicitly ("live" for `--live-keys`); the default follows
    OKX_DEMO. Naming it lets the live keys be verified — including their IP binding, which fails
    with 50110 — without touching MODE and arming order placement.
    """
    import ccxt

    s = get_settings()
    env = env or ("demo" if s.okx_demo else "live")
    key, secret, passphrase = s.okx_credentials_for(env)
    if not all((key, secret, passphrase)):
        return [Check(f"okx.{env}.auth", False, f"{env} key/secret/passphrase incomplete")]
    out: list[Check] = []
    client = ccxt.okx(
        {
            "apiKey": key,
            "secret": secret,
            "password": passphrase,
            "timeout": timeout_ms,
            "enableRateLimit": True,
            "hostname": s.okx_host.removeprefix("https://"),
            "options": {"defaultType": "swap"},
        }
    )
    if env == "demo":
        client.set_sandbox_mode(True)
    try:
        cfg = client.private_get_account_config()["data"][0]
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        return [Check(f"okx.{env}.auth", False, _auth_detail(exc))]
    acct_lv, pos_mode = cfg.get("acctLv"), cfg.get("posMode")
    perms = cfg.get("perm", "")
    out.append(Check(f"okx.{env}.auth", True, f"posMode={pos_mode}"))
    out.append(account_mode_check(env, acct_lv))
    out.append(ip_binding_check(env, cfg.get("ip")))
    out.append(
        Check(
            f"okx.{env}.permissions",
            "withdraw" not in perms and "trade" in perms,
            f"perm={perms or 'unknown'}",
        )
    )
    out.append(
        Check(
            f"okx.{env}.position mode",
            pos_mode == "net_mode",
            f"{pos_mode} (ARCHITECTURE.md §8 requires net_mode)",
        )
    )
    if env == "live":
        out.append(_live_flat(client, env))
    try:
        from argus.exchange.okx import SETTLEMENT_CCY, settlement_balance

        data = client.private_get_account_balance()["data"][0]
        pool = settlement_balance(data, SETTLEMENT_CCY)
        total = pool["account_total_usd"] or 0
        out.append(
            Check(
                f"okx.{env}.equity",
                pool["equity"] > 0,
                f"{pool['equity']:,.2f} {SETTLEMENT_CCY} tradable "
                f"(free {pool['free']:,.2f}; whole account ${float(total):,.2f})",
            )
        )
    except Exception as exc:  # noqa: BLE001
        # An account with no settlement row is unfunded, not broken. `settlement_balance`
        # raises either way — deliberately, because a silent 0 reads as a 100% drawdown to
        # the kill-switch (F-073) — but the operator deserves the difference in words.
        detail = _auth_detail(exc)
        if f"no {SETTLEMENT_CCY} row" in detail:
            detail = f"no {SETTLEMENT_CCY} balance — fund the account before trading it"
        out.append(Check(f"okx.{env}.equity", False, detail))
    try:
        from argus.exchange.okx import parse_fee_rates

        row = client.private_get_account_trade_fee({"instType": "SWAP", "instFamily": "BTC-USDT"})[
            "data"
        ][0]
        rates = parse_fee_rates(row, settle="USDT")
        maker, taker = float(rates["maker"]), float(rates["taker"])
        # OKX reports fees as negative numbers when charged; a positive value is a rebate.
        out.append(
            Check(
                f"okx.{env}.fees",
                taker < 0,
                f"maker {maker * 100:.4f}% taker {taker * 100:.4f}% (negative = charged)",
            )
        )
    except Exception as exc:  # noqa: BLE001
        out.append(Check(f"okx.{env}.fees", False, _auth_detail(exc)))
    return out


def _auth_detail(exc: Exception) -> str:
    """Readable reason for an auth failure. Never echoes a key."""
    text = str(exc)
    if "50119" in text:
        return "401 code=50119 API key doesn't exist (wrong host, or demo key used on live)"
    if "50102" in text or "Timestamp" in text:
        return "clock skew — system time is off"
    if "50113" in text:
        return "invalid signature (secret/passphrase mismatch)"
    if "50110" in text:
        return "IP not whitelisted for this key"
    return f"{type(exc).__name__}: {text[:100]}"
