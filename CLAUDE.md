# MacroSage — SHORT / Market Crash Monitor

Project brain for `short-dashboard`. If you're an AI assistant opening this
folder, read this first: it's the durable context so nobody has to re-explain
the project each session.

## What it is
A daily macro **crash-monitor** dashboard. `short_dashboard.py` pulls free
market data, scores **18 indicator tiles**, runs a rules-based verdict engine,
and emails a colour-coded HTML dashboard to the recipients. It is decision
support for a discretionary short/crash call — NOT an auto-trader. It never
places trades or moves money.

## How it runs
- **GitHub Actions cron** (`.github/workflows/dashboard.yml`): `cron: '17 22 * * 1-5'`
  = 22:17 UTC, Monday–Friday (~5:17pm ET), `ubuntu-latest`, `timeout-minutes: 10`.
- The job runs the script, which emails the dashboard, then commits `state.json`
  back with `[skip ci]` (so the state write never re-triggers the workflow).
- Also runnable by hand from the Actions tab ("Run workflow" / `workflow_dispatch`).
- Repo is **private**; local clone lives at `C:\Users\WOLFG\Projects\short-dashboard`.

## Files
- `short_dashboard.py` — the whole engine (data fetch → scoring → verdict → email). ~2100 lines, module-level pipeline that runs on import.
- `backtest.py` — standalone backtest of the trend-regime core (not part of the daily run). Run with `py backtest.py`.
- `test_harness.py` — verdict-engine test suite (fixtures, no network). Run with `py test_harness.py`; expect `RESULT: ALL CHECKS PASSED`.
- `sim_drill.py` — escalation-drill simulator. `fmp_client.py` / `run_fmp.py` — FMP helpers/snapshot.
- `state.json` — last-known-good cache + the signal ledger; committed back each run.
- `.github/workflows/` — `dashboard.yml` (the daily job), `escalation-drill.yml` (manual), `claude.yml` (PR-comment automation, unrelated to scanning).
- `reports/` — saved HTML dashboards.

## Data sources (all free; FMP still primary but now has fallbacks)
- SPY / VIX / gold spot: **FMP → Yahoo → Stooq**.
- 2y / 10y Treasury: **FMP → FRED** (`DGS2` / `DGS10`).
- Breadth %: FMP sector snapshot → **WSJ NYSE advance/decline** scrape.
- NYMO (McClellan): WSJ A/D → Finviz. NAAIM / AAII: site scrapes.
- VIX term structure (VIX/VIX3M), VIX9D: Yahoo. VVIX: Yahoo.
- Net liquidity, credit (HY OAS), USD, fiscal (MTS): FRED. COT: CFTC → Tradingster.
- **HARD RULE: a missing value renders "unavailable" or uses a labelled last-known cache — NEVER a fabricated number.**

## Verdict engine
`initiate_short` (a real boolean) fires **INITIATE SHORT** only when EVERY gate
is positively confirmed (fail-closed: unknown data blocks it, it can never fire
on missing inputs):
- **Dual-red streak ≥ 3** — breadth < 50% AND net liquidity declining, for 3
  consecutive trading sessions (session-guarded so weekend/repeat runs can't inflate it).
- **SPX below 200-day MA** and **SPX below 10-month EMA** (the trend regime).
- **Volume ≥ 1.2×** the 20-day average on the breakdown session.
- **Reward:Risk ≥ 5.0**.
- **FOMC not within 2 days** — NOTE this gate is deliberately **fail-OPEN**: an
  empty calendar is the normal case, so an unknown FOMC date does NOT block.
- Sizing ladder by red-tile count; catalyst auto-confirm + post-loss de-size flags.
- **Layer-2 ENTRY SIGNAL** (2-of-3): vol-expansion/VIX9D/GEX, VIX backwardation,
  McClellan divergence. **PRE-ALERT** composite is an early-warning tier below Layer-2.

## Email
- Header: **MACROSAGE / MARKET CRASH MONITOR** in calm steel-blue; the red
  **CRASH ALERT** pill shows ONLY when `initiate_short` is true. Otherwise WATCHING.
- Recipients: `DEFAULT_RECIPIENTS = ["wolfgangduke@gmail.com"]` is the **To**;
  everything in the `MAIL_TO` secret is added as **Cc** (Richard).
- Includes a plain-English "what to do" summary, a "why these metrics" legend,
  and the interactive HTML report as an attachment.
- Exit code is red on email-send failure so Actions marks the run failed.

## KNOWN ISSUE — flagged 2026-07-22, needs fix
Manually triggered `workflow_dispatch` run #106 (commit d93259e, branch
`feat/30y-duration-stress`) completed **Status: Success** in 22s. No email
arrived (checked immediately after, inbox only).

**Why this points to delivery, not send failure:** `send_email()` explicitly
`sys.exit(1)`s on every failure path (missing creds, SMTP auth error, refused
recipients, any other exception), and the `Run dashboard` step in
`dashboard.yml` has NO `continue-on-error` — so a genuine send failure should
have shown red/failed, not green. This strongly suggests SMTP accepted and
sent the message, and the miss is downstream.

**Checklist for the fix session:**
1. Check spam/junk AND "All Mail" (not just Inbox) on both recipient
   addresses for a message dated 2026-07-22, subject starting "MacroSage —
   Daily Risk Report" or "⚠ CRASH ALERT — MacroSage".
2. Check the **Sent** folder on the `GMAIL_USER` sending account for an
   outbound message at the run timestamp — confirms SMTP accepted it either way.
3. Verify the `MAIL_TO` secret has no typo (Richard's address).
4. Re-run `workflow_dispatch` once the above is checked, and read the
   `log.info("EMAIL SENT to %s", ...)` line directly in the Actions log this
   time (the automated log viewer wasn't cooperating when this was filed).

**Unrelated doc-accuracy note found while investigating:** `dashboard.yml`
maps `FRED_API_KEY: ${{ secrets.FRED }}` — the real GitHub secret is named
`FRED`, not `FRED_API_KEY` as the Secrets section below implies. Confirmed
this isn't broken (live FRED data fetched fine in run #106) — just a stale
doc vs. the real secret name. Low priority, fix opportunistically.

Not related to the tile 19 (Long-End Duration Stress) change on
`feat/30y-duration-stress` — that's a pre-existing email-delivery issue,
filed separately on its own branch rather than bundled into that PR.

## Signal ledger (track record)
Each trading day the run appends a row to `state.json` (`signal_ledger` key):
date, SPY level, verdict state, and gate flags. It backfills 5/10/20-trading-day
forward SPY returns onto older rows and logs a running hit-rate line. The live
track record began 2026-07-08. Fully fail-safe (wrapped in try/except).

## Backtest finding (important, read before claiming edge)
`backtest.py` replays the reconstructable trend-regime core over ~10y (SPY,
2017–2026). Key results:
- The core (below 200DMA AND 10M-EMA) has **no standalone predictive edge** —
  regime-ON forward returns run slightly ABOVE base (mean-reversion); on the 35
  regime entries the market fell over the next 20d **51% of the time (coin flip)**.
- **VIX backwardation is contrarian-BULLISH** (fires near panic bottoms → bounces),
  so it points the WRONG way for a short thesis.
- The ONE component with genuine short-side edge is **vol-expansion within a
  downtrend**: near-term (5–10d) returns negative, ~61% right on entry — but it
  fades by 20d and the sample is small (69 days / 23 episodes).
- Caveat: still a SUBSET (omits breadth %, net liquidity, COT, NAAIM, AAII — no
  free multi-year history). **Do not market MacroSage as a proven "crash caller"**;
  the honest positioning is a disciplined regime filter. The live ledger will
  settle whether the full stacked signal has edge.

## Secrets (GitHub → Settings → Secrets and variables → Actions)
- `FMP_API_KEY` — still primary for spot quotes (free fallbacks behind it).
- `FRED_API_KEY` — Treasury yields, net liquidity, credit, fiscal.
- `GMAIL_USER`, `GMAIL_APP_PASSWORD` — Gmail SMTP send (app password, 2FA on).
- `MAIL_TO` — Cc recipients (Richard).
- `HEALTHCHECK_URL` — OPTIONAL heartbeat (see below). Not yet set.
- Optional manual overrides: `GEX_FLIP`, `CATALYST_ON`, `POST_LOSS_DESIZE`,
  `RR_STOP`, `RR_TARGET`, `FMP_FORCE_FAIL` (test-forces the FMP fallback path).

## Reliability / heartbeat
- Silent-failure coverage is two-part: exit-code-red catches a failed email
  WITHIN a run; a **heartbeat** catches the run not happening at all.
- The heartbeat code already exists (fires only if `HEALTHCHECK_URL` is set;
  pings the URL on success, URL/`/fail` on email failure; fail-safe). To activate:
  create a check at healthchecks.io (cron `17 22 * * 1-5`, UTC, ~1h grace), add
  an alert channel, and put its ping URL in the `HEALTHCHECK_URL` secret.

## Conventions (Bryan's coding rules — follow these)
- **Zero third-party deps** — stdlib/urllib only.
- **Surgical edits**; flag dead code with a comment, don't silently delete.
- **Fail-safe always**: never fabricate a value, never let a helper break the run.
- **Branch → PR → review before merging to main**; NEVER touch secret handling.
- Verify every change: `py -m py_compile short_dashboard.py` and `py test_harness.py`
  (expect ALL CHECKS PASSED), then a real `workflow_dispatch` run before merge.

## Open roadmap
Prioritised improvements (do as small, independently-verified PRs, never one big-bang):
1. **Done** — perf HTTP cache; signal ledger; backtest (+ confirmation filters).
2. **#3 Distance-to-trigger** — show how close each gate is to firing
   ("dual-red 1/3 · SPX 4% above 200DMA · R:R 2.1 vs 5.0"), turning binary gates
   into a gradient. Rendering change → update `test_harness.py`.
3. **#4 Email cadence** — send the full email only on a state change / threshold
   cross, weekly digest otherwise, plus a "what changed since yesterday" line.
   Needs Bryan's rules on what counts as a state change.
4. **Heartbeat activation** — Bryan adds the healthchecks.io check + secret.
5. Possible signal rework: weight vol-expansion, reclassify VIX backwardation as
   a bounce/exit cue. DEFERRED — small sample; let the ledger accumulate first.

## Monetization goal (Bryan)
Not yet monetized; the aim is cash flow. A credible track record (ledger +
backtest) is the gate to any product claim. Given the backtest, position it as a
disciplined risk/regime tool, not a crash predictor. Candidate path once a real
track record exists: public track-record page (Vercel already hosts the
crash-monitor) + a paid tier (Substack/Ghost + Stripe), free = weekly digest.

## Working notes
- The GitHub connector in Cowork/Dispatch is public-scope only → 404s this private
  repo. Repo work is done from the LOCAL clone (git via terminal) or via a
  browser Claude session with full GitHub access. Merges to main go through PRs.
- `[skip ci]` "chore: update last-known market values" commits are the Actions
  bot writing `state.json` back — expected, additive, ignore them.
