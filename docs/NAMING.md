# NAMING.md — why "ARGUS", and alternatives

## ARGUS
Argus Panoptes ("all-seeing") is the giant in Greek myth with a hundred eyes, some of which stayed open while the others slept — Hera set him to watch Io. The fit:
- **Many eyes:** the bot's whole edge is watching many independent sources (derivatives, on-chain, macro, catalysts, news) and refusing to trust any one of them.
- **Some eyes always open:** the exchange-side stops + the no-LLM watchdog keep watching while the LLM layer sleeps between runs.
- **Watchman, not warrior:** Argus guards; he doesn't attack. The LLM proposes, code guards — that is the design thesis.
- Practical: short, pronounceable, unused as a Python package name on PyPI in the sense that matters (`argus` exists as an unrelated old package; `argus-trader` / `argusbot` are free), and it reads well in a repo name and a Telegram bot handle.
- Caveat: Argus was eventually lulled to sleep by Hermes and killed — a reminder that a watchman who stops watching is worse than none. The watchdog exists because of that.

## Alternatives (ranked by fit)

| Name | Meaning | Why it fits | Why it might not |
|---|---|---|---|
| **HELM** | The steering gear of a ship | You "take the helm"; a portfolio manager steers, doesn't row; short, calm, brand-safe | Generic; several finance products use it |
| **PANOPTES** | Argus's epithet: "all-seeing" | Same myth, more distinctive, no PyPI clash | Longer; "panopticon" surveillance connotation |
| **SENTINEL** | A guard who stands watch | Emphasises the safety layer (watchdog, kill-switches) | Very common in security software |
| **KESTREL** | A small falcon that hovers motionless, watching, then strikes once | Patience + selectivity — the Alpha Arena lesson (fewer trades won) | Less obvious link to "many sources" |
| **LOOKOUT** | Ship's watch-keeper who reports what is seen, doesn't steer | Reporting evidence with provenance is the core; humble, non-hype | Plain; hard to trademark |
| **TALLY** | A running count / the ledger notches | The journal and calibration table *are* the product; honest accounting | Sounds like a bookkeeping app |
| **BEACON** | Warning light for ships | Regime + kill-switch layer; guides rather than trades | Overused in crypto (Beacon Chain) |
| **QUORUM** | The minimum number of members needed to decide | Consensus mode: two model families must agree, gate must pass — nothing trades on one voice | Governance connotation; harder to say warmly |
| **ORRERY** | A mechanical model of the planets' motions | A mechanism that models many moving parts and their alignment; evocative, unique | Obscure; hard to spell |
| **VIGIL** | A period of keeping awake to watch | Watchdog cadence; short | Funereal connotation |

Recommendation: keep **ARGUS** unless you want a single-syllable brand (HELM) or a unique one (ORRERY, PANOPTES). Whatever you choose, the repo is `<name>-trader` and the Python package `<name>`.
