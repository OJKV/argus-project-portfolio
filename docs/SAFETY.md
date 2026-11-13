# Safety contracts

ARGUS uses deterministic risk controls and separates demo and live venue configuration. Review the implemented risk limits before using any execution mode.

An empty risk profile selects `aggressive` below $500 and `conservative` once equity has remained at or above that threshold for seven days.

`MODE=approve` behaves exactly like `MODE=auto`; it does not wait for approval. Both require explicit arming.

`argus doctor` checks configuration and reports available diagnostics.

## Risk limits in force

| Limit | In force | Code |
|---|---|---|
| Portfolio drawdown kill | 15% from the 30-day equity high, every profile | `gate/killswitch.py` `PORTFOLIO_DD_LIMIT_PCT`, `runs/watchdog.py` `DD_HALT_PCT` |
| Daily loss halt | 3% of equity, every profile and step | `gate/protections.py` `DAILY_LOSS_LIMIT_PCT` |
| Weekly loss halt | 6% of equity, every profile and step | `gate/protections.py` `WEEKLY_LOSS_LIMIT_PCT` |
| Max open positions | 4, at any equity | `gate/rules.py` `MAX_POSITIONS` |
| Default profile | `aggressive` below $500 equity; `conservative` once equity has stayed ≥ $500 for 7 days (`RISK_PROFILE` empty — set it to force one; step 3 stays below $500 even when forced) | `gate/risk_config.py` `select_profile()` |
| What a profile changes | only the small-account steps allowed: `conservative` 0–2, `aggressive` 0–3 | `runs/manager.py` `step_policies()` |

Live venue access additionally requires `LIVE_VENUE=I_UNDERSTAND_REAL_MONEY`; the code rejects a live venue without this explicit interlock.

## Live-readiness gate

Treat every item as unverified until supported by evidence. Never infer readiness from a green unit test alone.

Fix-first controls to verify with regression evidence:

- `R-1`: venue stops cannot be widened and unknown stops are refused.
- `P-N2`: approval mode is either genuinely queued or its automatic behavior is explicitly accepted.
- `P-N1`: privileged installer grants are narrowly scoped and demo helper refuses live execution.
- `P-N3`: deployment holds the run lock during the swap.
- `G-N1`, `G-15`, `G-N4`: gate reads the complete book and fails closed on missing data.
- `D-1`, `D-3`, `D-10`: price and spread come from the ticker and required missing data is rejected.
- `G-1`: risk reductions are not undone by sizing escalation.
- `X-N3`, `X-N4`, `X-N5`: protection is side-specific, flatten cancels open orders, and uncertain orders are reconciled before rejection.
- `L-2`, `L-5`: event theses and suspect sources are supported by evidence and reach the gate.
- `J-N1`: ledger tables resist updates and verify successfully.
- `P-2`: halt, resume and flatten commands work; `O-1`: alerts escape and cap untrusted text.
- `P-N5`: the live venue requires the explicit interlock in addition to execution arming.

The Phase-4 evidence gate requires at least 50 shadow decisions by real agents, 15 closed demo trades, zero missing-stop watchdog repairs and no unhandled errors in the last 14 days, calibration buckets that are not inverted, measured fee+funding drag, reviewed consensus veto statistics, and review of the last ten `/why` records with at least seven accepted. Missing measurements fail the gate. These are design criteria, not a claim they have been met.

The incident procedures below must be rehearsed safely in demo and the evidence recorded before a live-readiness decision. Independently stress-test ("grilling") the evidence and risks.

## Incident playbook

### Emergency flatten
Halt new activity, cancel open orders and close positions with the documented flatten command; verify the venue is flat.

### Manual halt / resume
Use `argus halt` to stop new actions and investigate before `argus resume`. If the CLI is unavailable, `touch /var/lib/argus/HALT` is the default-home fallback; use the configured ARGUS_HOME when different.

### OKX stop engine outage
Halt new entries and independently verify protection and venue status. Do not assume an unconfirmed stop exists.

### VPS lost
Verify the venue directly, stop further activity where possible, and restore the journal and service from a verified backup.

### Key compromise
Revoke affected credentials at the provider, halt activity, review access and rotate credentials before resuming.

### Stuck or partially filled order
Reconcile order status and fills at the venue before cancellation or retry; verify the resulting position is protected.

### Orphan algo order / double stop
Reconcile position and protection state at the venue before cancelling duplicate or orphaned protection.

### Ledger mismatch
Halt automated actions, preserve evidence and backup, then investigate ledger verification before any repair.

### Disk full
Halt new activity, preserve and back up the journal, recover storage and verify journal integrity.

### Bad deploy
Halt affected actions, preserve the journal and restore the last verified version and compatible backup before resuming.

Deployment diagnostics distinguish `deploy.lock`, `deploy-inflight`, `recovering-interrupted-deploy`, `stamp-mismatch`, `no-green-ref`, `paged-recently`, `units-pending`, `autodeploy.paused` and service `ActiveState`.

### Rehearsals

| Playbook | Evidence status |
|---|---|
| Emergency flatten | unverified |
| Manual halt / resume | unverified |
| OKX stop engine outage | unverified |
| VPS lost | unverified |
| Key compromise | unverified |
| Stuck or partially filled order | unverified |
| Orphan algo order / double stop | unverified |
| Ledger mismatch | unverified |
| Disk full | unverified |
| Bad deploy | unverified |

## Timer configuration

The shipped `argus-eval-models.timer` (**daily** 08:30 UTC), `argus-reflect.timer` (Sun 09:30) and `argus-autodeploy.timer` are described by the unit files; check the unit files when configuration changes.
