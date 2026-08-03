# MacroSage — SHORT / Market Crash Monitor

Project brain for `short-dashboard`. If you're an AI assistant opening this
folder, read this first: it's the durable context so nobody has to re-explain
the project each session.

## What it is
A daily macro **crash-monitor** dashboard. `short_dashboard.py` pulls free
market data, scores **19 indicator tiles**, runs a rules-based verdict engine,
and emails a colour-coded HTML dashboard to the recipients. It is decision
support for a discretionary short/crash call — NOT an auto-trader. It never
places trades or moves money.

## How it runs
- **GitHub Actions cron** (`.github/workflows/dashboard.yml`): `cron: '17 22 * * 1-5'`
  = 22:17 UTC, Monday–Friday (~5:17pm ET), `ubuntu-latest`, `timeout-minutes: 10`.
  Also runnable by hand from the Actions tab ("Run workflow" / `workflow_dispatch`).
- The job runs the script, which emails the dashboard, then persists `state.json`
  (last-known-value cache + signal ledger) to a **dedicated `state` branch** via
  raw git plumbing (`hash-object` / `mktree` / `commit-tree` / push) — **main is
  never written by the workflow**, so main stays code-only and branch-protectable.
  The restore step at the top of the job pulls `state.json` back off the `state`
  branch before the run (fail-safe: missing branch/file falls back to the
  committed seed).
- Repo is **private**; local clone lives at `C:\Users\WOLFG\Projects\short-dashboard`.
- **GitHub API write access 403's** for the connector used in some Claude
  sessions against this private repo — branch pushes and PR merges are done via
  **local git from the terminal clone**, not the API, for that reason.

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
- 2y / 10y / 30y Treasury: **FMP → FRED** (`DGS2` / `DGS10` / `DGS30`). Tile 19
  (30Y duration stress) additionally pulls a dated 60-session window via
  `_fred_series_dated()` to score persistence above 5.00%, not just the level.
- Breadth %: FMP sector snapshot → **WSJ NYSE advance/decline** scrape.
- NYMO (McClellan): WSJ A/D → Finviz. NAAIM / AAII: site scrapes.
- VIX term structure (VIX/VIX3M), VIX9D: Yahoo. VVIX: Yahoo.
- Net liquidity, credit (HY OAS), USD, fiscal (MTS): FRED. COT: CFTC → Tradingster.
- **HARD RULE: a missing value renders "unavailable" or uses a labelled last-known cache — NEVER a fabricated number.**

## Verdict engine — canonical spec

This section is the single source of truth for tile numbering, gates, overrides,
and sizing. It was reconstructed 2026-07-25 by reading `short_dashboard.py`
directly (not from prior chat notes), so treat it as authoritative over anything
said in past sessions. **Any future change to gate logic, tile thresholds, or
override behavior must be documented in this section in the SAME commit/PR
that makes the change — no exceptions.**

### Tile map (1–19)
All tiles live in the `p = [...]` list (`short_dashboard.py:1509-1603`), each a
`(label, sub-text, color)` tuple. Color is `gray` whenever the metric is fully
unavailable (no live value AND no cache).

| # | Tile | Data source | Threshold / color rule |
|---|------|-------------|-------------------------|
| 1 | Equities (SPY) | FMP quote → Yahoo → Stooq | Informational only; amber if data present, gray if not. |
| 2 | Volatility (VIX) | FMP quote → Yahoo → Stooq | Informational only; amber/gray. |
| 3 | Rates / yield curve | FMP `treasury-rates` → FRED `DGS2`/`DGS10` | 2s10s spread in bps; green if ≥0, red if inverted, gray unknown. |
| 4 | Credit spreads | FRED `BAMLH0A0HYM2` (HY OAS, daily, 25-obs pull) | **Recalibrated 2026-08-03:** color = LEVEL + 21-session rate-of-change, not the raw day tick (old rule went red on a 2bp uptick at a calm 2.8% level). red if OAS ≥4.0% OR Δ21d ≥ +0.75pp (blowout); amber if Δ21d ≥ +0.25pp; green otherwise. Short-history fallback keeps the directional rule but level-gates red (widening only reds at ≥4.0%, else amber). amber if last-known cache, gray if no data. |
| 5 | Commodities (Gold) | FMP quote → Yahoo → Stooq | Informational only, no directional threshold. **green** when `gold_px` is available, **gray** only on genuine fetch failure (no live value and no cache) — fixed 2026-07-31; previously hardcoded gray unconditionally, so it sat in the "No Data" bucket (see Sizing tiers) even on days it had a live price. |
| 6 | Dollar / FX | FRED `DTWEXBGS` (broad $ index) | red if rising w/w, green if falling, amber cached, gray no data. |
| 7 | Market breadth | FMP sector snapshot % advancing → WSJ NYSE A/D fallback | **red if <50%** (dual-red input #1), green if ≥50%, gray unavailable. |
| 8 | Net liquidity | FRED `WALCL` − `WTREGEN` (TGA) − `RRPONTSYD` (RRP), in $T | **red/"declining"** if current < previous (dual-red input #2), green/"rising" else, gray unknown. |
| 9 | Positioning (COT) | CFTC E-mini S&P COT → Tradingster fallback (if CFTC >10d stale) | green if asset-mgr net long, red if net short. |
| 10 | VVIX divergence | Yahoo `^VVIX` vs `^VIX` daily % change | red if VVIX +3%+ while VIX ≤+1%, green if VVIX ≤−2%, amber otherwise. |
| 11 | Sector rotation | **Derived from tile 7**, not independent data | red ("defensive tilt") if breadth red, green ("broad") else. Excluded from the sizing tally for exactly this reason (see Sizing below). One of the two **MAX CONVICTION** legs. |
| 12 | Calendar gate | FMP `economic-calendar` (FOMC) + computed 3rd-Friday OpEx | Display flags: `FOMC in Xd` if FOMC ≤5 days out; `CALENDAR GATE — TRANSITION WINDOW (OpEx in Xd)` if OpEx ≤10 days out. red if either flag present. **Note:** this tile's display thresholds (5d / 10d) are wider than the actual PRIMARY-verdict FOMC gate (≤2 days — see Hard gates). |
| 13 | Fiscal impulse | Treasury MTS via FRED (`MTSDS133FMS` deficit, `MTSO133FMS` outlays, `MTSR133FMS` receipts, `A091RC1Q027SBEA` interest) | red if rolling-12M deficit >$2.0T AND outlays YoY >+8%; amber if deficit $1.5–2.0T or outlays YoY +5–8%; green below both. Sub-note ⚠ if interest/receipts >13%. |
| 14 | McClellan / NYMO | WSJ NYSE A/D → Finviz fallback; 19/39-day EMA oscillator, session-guarded | green if NYMO ≥0, red if <0. |
| 15 | NAAIM Exposure | naaim.org scrape (weekly) | red if >90 (managers all-in, contrarian-bearish), green if <40, amber between, gray if no data. |
| 16 | AAII Sentiment | aaii.com scrape (weekly) | red if bull >55%, green if bear >45%, amber between. The other **MAX CONVICTION** leg. |
| 17 | VIX Term Structure (VIX/VIX3M) | Yahoo `^VIX` vs `^VIX3M` | **Reclassified 2026-08-03 (roadmap #5):** confirmed BACKWARDATION now renders **amber** with an explicit "contrarian-BULLISH (panic regime — bounce/exit cue, not short confirmation)" note — never red — because per `backtest.py` backwardation fires near panic bottoms and must not inflate the `n_stress` short-sizing tally. CONTANGO stays green, upgraded green→amber when the term-structure velocity input is accelerating (≥+0.08/5d with ratio ≥0.95) — the 2026-07-30 fix. Streak is date-guarded. `_vix_backwardation` no longer feeds Layer-2 (see Layer-2 input #2 below); it remains a PRE-ALERT informational input only. |
| 18 | Breadth proxy (RSP/SPY) | Yahoo → Stooq, RSP÷SPY ratio vs 50d MA + 5-session slope | BROADENING vs NARROWING, session-guarded streak. red if divergence confirmed (narrowing + SPX within 2% of 52wk high), green if broadening, amber if stale cache. |
| 19 | Long-End Duration Stress (30Y) | FRED `DGS30`, dated 220-calendar-day pull (`_fred_series_dated`) scored over the trailing 60 sessions | red if latest print >5.50% (hard override — "uncharted since 2007") OR ≥25% of the last 60 sessions closed >5.00%; amber if 10–25%; green below 10%. Also reports a YTD day-count >5%. On a failed dated fetch, falls back to the **last cached verdict string**, not gray (2026-07-25 fix — see `299d7f1`). |

### Hard gates — `initiate_short` (PRIMARY / INITIATE SHORT)
`short_dashboard.py:1650-1675`. `initiate_short` is a real boolean that fires
only when **every** gate below is positively confirmed — fail-closed by
default (unknown data blocks; INITIATE can never fire on missing inputs),
**except the FOMC gate, which is deliberately fail-open**:
1. **Dual-red streak ≥ 3** — tile 7 breadth <50% AND tile 8 net liquidity
   declining, for 3 consecutive *trading* sessions (session-guarded: weekend/
   holiday/repeat-same-day runs cannot inflate the streak).
2. **SPX confirmed below its 200-day MA** (`spx_above_200dma is False`; `None`
   = unknown = blocks).
3. **SPX confirmed below its monthly 10-month EMA** (`spx_above_10mema is
   False`; `None` blocks).
4. **Breakdown-session volume ≥ 1.2×** the 20-day average (SPY volume,
   partial intraday bar dropped pre-16:00 ET). Unknown volume fails closed.
5. **Reward:Risk ≥ 5.0** — entry = SPY price; stop = 5-session high (floor
   entry+0.5%, override `RR_STOP`); target = measured-move projection
   (existing drawdown doubled, override `RR_TARGET`). **Risk is capped at 2%
   of entry** (`short_dashboard.py:1411`) — matching the Exit rule below, since
   realized risk can never exceed a 2% adverse move before the position is
   closed. Before the 2026-07-30 fix, risk used the raw (uncapped) stop
   distance, which overstated risk and suppressed R:R whenever the 5-session
   high sat further than 2% from entry. Unknown inputs fail closed.
6. **FOMC not within 2 days** (`fomc_days is not None and fomc_days <= 2`
   blocks) — **fail-OPEN**: an empty/unknown calendar does NOT block, because
   no-FOMC-this-week is the normal case and a flaky calendar API must not
   permanently suppress INITIATE.

**Important:** tile 12's OpEx "TRANSITION WINDOW" (≤10 days) does **not** gate
`initiate_short` directly — only the FOMC ≤2-day check does. OpEx proximity
does affect the Layer-2 entry check (see below).

### Layer-2 — 2-of-3 ENTRY SIGNAL
`short_dashboard.py:1727-1745`. Three Layer-2 inputs, computed independently
of the 19 numbered tiles:
1. **`gamma_flip`** (GEX proxy) — true if realized-vol expansion (5-day
   realized vol ≥1.30× its 20-day baseline) OR VIX9D/VIX inversion (≥1.0) OR
   manual `GEX_FLIP=1`/`gex_flip_manual`. The real GEX/dealer-gamma flip
   (spotgamma.com) is JS/Cloudflare-walled and is **not** scraped — this is a
   keyless proxy, with the manual override as the intended path for checking
   it by hand.
2. **Term-structure velocity (`ts_accelerating`)** — VIX/VIX3M ratio rising
   ≥+0.08 per 5 sessions with the ratio already ≥0.95 (racing toward
   inversion). **Swapped in 2026-08-03 (roadmap #5):** this input was
   previously `_vix_backwardation` (tile 17's regime), but per `backtest.py`
   confirmed backwardation is contrarian-BULLISH — a panic-bottom marker
   that must not help trigger a short entry. Velocity is the pre-breakdown
   version of the same curve information.
3. **McClellan divergence** — tile 14 (NYMO) red AND SPX within 2% of its
   52-week high.

`layer2` fires `"ENTRY SIGNAL - early/low-conviction (...)"` when **≥2 of the
3** are true **and** the calendar is clear — the clear-check is a substring
test on tile 12's combined flag text for `"in 0d"`/`"in 1d"`/`"in 2d"`, so it
covers **both** an imminent FOMC and an imminent OpEx (only at the 0–2 day
edge, not the full 10-day OpEx window). Otherwise `layer2` stays `"WAIT"`.

**Discrepancy flagged, not silently resolved:** the code has exactly **two**
literal `layer2` states — `"WAIT"` (default) and `"ENTRY SIGNAL - ..."`. There
is **no** distinct `"CALENDAR GATE"` verdict string for Layer-2 in this
codebase — a near-term FOMC/OpEx event just leaves `layer2` at `"WAIT"`
without a separate label. The three-verdict framing (ENTRY SIGNAL / WAIT /
CALENDAR GATE) described in the global `macro-dashboard` skill belongs to a
*different* system — the ad-hoc 18-point "SHORT" chat trigger — not to this
repo's coded verdict engine. Don't conflate the two when documenting or
extending either one.

**PRE-ALERT** (`short_dashboard.py:1483-1505`) is a separate, informational-only
early-warning tier *below* Layer-2 — it never gates INITIATE and never affects
sizing. Fires when the breadth proxy (tile 18) has been narrowing ≥3 sessions
AND at least one of {VIX9D inversion, term-structure velocity acceleration,
VIX backwardation, vol expansion} is on AND SPX is within 2% of its 52-week high.

### MAX CONVICTION — resolved
`short_dashboard.py:1704`: `max_conviction = initiate_short and tile11_red and tile16_red`.

**MAX CONVICTION requires `initiate_short` to already be true, PLUS tile 11
(Sector rotation) AND tile 16 (AAII Sentiment) both red.** This is the Pt11 +
Pt16 pairing — confirmed directly from the code, not the Pt11+Pt15 variant nor
any RUT Canary pairing floated in earlier sessions.

**RUT Canary and Crude Cluster 16A/16B do not exist anywhere in
`short_dashboard.py`** — a full-file grep for `rut canary`, `crude cluster`,
`16A`, `16B` returns zero matches. They are **retired / never implemented in
this codebase**, full stop; do not reference them as live tiles or as inputs
to MAX CONVICTION going forward.

### Override flags
| Flag | Effect |
|------|--------|
| `GEX_FLIP=1` (env) or cache `gex_flip_manual` | Forces the Layer-2 `gamma_flip` proxy true (see Layer-2 input #1 above). |
| `CATALYST_ON=1` (env) or cache `catalyst_on` | Forces `catalyst_on` true, avoiding the 0.5× no-catalyst size halving. Also auto-confirms (`catalyst_auto`) when SPY prints a fresh 20-day low AND breakdown volume ≥1.2×. |
| `POST_LOSS_DESIZE=1` (env) or cache `post_loss_desize` | Halves size again (×0.5) after a realized loss. **Not auto-set by any code** — there is no position/P&L tracking in this repo, so this is a purely manual flag set/cleared by hand after a real trade. |
| `RR_STOP` / `RR_TARGET` (env or cache `rr_stop_manual`/`rr_target_manual`) | Override the mechanical R:R stop (5-session high) / target (measured-move projection) with a level-based value. |
| `FMP_FORCE_FAIL` | Test seam only (`workflow_dispatch` input) — forces the FMP path dead to exercise the Yahoo/Stooq fallback tier. Not a trading override. |

### Sizing tiers
Sizing uses `n_stress` (`short_dashboard.py:1621-1635`), **not** the raw
all-tiles red count (`n_red`, still shown in the email). `n_stress` excludes
tile 11 (Sector rotation — derived from tile 7, would double-count breadth)
and tile 12 (Calendar gate — timing, not stress):
- `n_stress ≥ 14` → **2.0×** (cap)
- `n_stress ≥ 10` → **1.5×**
- `n_stress ≥ 6` → **1.0×** (standard)
- `n_stress < 6` → **0.5×** (probe only)
- then ×0.5 again if `catalyst_on` is false (no active catalyst)
- then ×0.5 again if `post_loss` de-sizing is active

**Emailed color tally (separate from `n_stress`, informational only):** the
summary strip in `build_html()` (`short_dashboard.py:1826-1829`) shows four
buckets — Bearish (red), Watch (amber), Neutral (green), **No Data** (gray) —
that sum to `len(p)` (19). Before a 2026-07-30 fix it only summed
red+amber+green, silently dropping gray tiles (e.g. tile 5 Gold, always gray
by design at the time), so the strip showed 18 even on a 19/19-retrieved day.
This tally does **not** feed sizing — `n_stress` above is the only sizing
input and was correct throughout. Tile 5 is no longer always gray as of the
2026-07-31 fix — see the Tile map above.

### Exit rule
**2% adverse move within 3 sessions = full exit, no averaging down.** This is
stated in the email verdict text and legend only — there is **no** position/P&L
state in the code that tracks or enforces it programmatically. It's a
discretionary rule for Bryan to apply by hand, not a coded gate.

## Email
- Header: **MACROSAGE / MARKET CRASH MONITOR** in calm steel-blue; the red
  **CRASH ALERT** pill shows ONLY when `initiate_short` is true. Otherwise WATCHING.
- Recipients: `DEFAULT_RECIPIENTS = ["wolfgangduke@gmail.com"]` is the **To**;
  everything in the `MAIL_TO` secret is added as **Cc** (Richard).
- Includes a plain-English "what to do" summary, a "why these metrics" legend,
  and the interactive HTML report as an attachment.
- Exit code is red on email-send failure so Actions marks the run failed.

## KNOWN ISSUE — flagged 2026-07-22, RESOLVED same day (root cause: Gmail categorization, not code)
Manually triggered `workflow_dispatch` run #106 (commit d93259e, branch
`feat/30y-duration-stress`) completed **Status: Success** in 22s. Bryan
reported no email visible in his inbox.

**Confirmed via direct Gmail search (not the Actions log, which the automated
viewer couldn't extract):** the message exists — sent 2026-07-22T19:06:32Z,
subject "MacroSage — Daily Risk Report — July 22, 2026", To
wolfgangduke@gmail.com, Cc richard.macrae.gordon@gmail.com, labels
`SENT` + `INBOX`. SMTP genuinely delivered it and Gmail filed it into the
inbox at the label level.

**Conclusion: not a send failure, not a code bug.** The `send_email()`
fail-closed design (sys.exit(1) on every failure path, no
`continue-on-error` on that step) was correct all along — a real failure
would have shown red, and it didn't. Most likely explanation for "not
visible": Gmail's tabbed inbox (Promotions/Updates) miscategorizing an
automated self-sent HTML email with an attachment, landing it outside the
Primary tab view even though it's technically in INBOX. Confirm by checking
those tabs or searching "MacroSage" directly in Gmail.

**Unrelated doc-accuracy note found while investigating — RESOLVED 2026-08-03:**
`dashboard.yml` mapped `FRED_API_KEY: ${{ secrets.FRED }}` while the script read
`cfg("FRED_API_KEY")` — the real GitHub secret is named `FRED`, not
`FRED_API_KEY`. Never broken (live FRED data fetched fine in run #106), just a
naming mismatch. Fixed by renaming the workflow's env var and the script's
`cfg()` key to `FRED` throughout (rather than renaming the GitHub secret
itself) — no GitHub secret rotation needed. **If you have a local `.env` with
`FRED_API_KEY=...`, rename that key to `FRED` too** — `.env` is gitignored so
this doc change doesn't touch it for you.

Not related to the tile 19 (Long-End Duration Stress) change on
`feat/30y-duration-stress` — filed separately on its own branch rather than
bundled into that PR.

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
- `FRED` in the script / `cfg("FRED")` — Treasury yields, net liquidity,
  credit, fiscal. GitHub secret name and code now match (fixed 2026-08-03 —
  see the KNOWN ISSUE note above).
- `GMAIL_USER`, `GMAIL_APP_PASSWORD` — Gmail SMTP send (app password, 2FA on).
- `MAIL_TO` — Cc recipients (Richard).
- `HEALTHCHECK_URL` — OPTIONAL heartbeat (see below). Not yet set.
- Optional manual overrides: `GEX_FLIP`, `CATALYST_ON`, `POST_LOSS_DESIZE`,
  `RR_STOP`, `RR_TARGET`, `FMP_FORCE_FAIL` (test-forces the FMP fallback path).

## Known issues
- **Layer-2 has no coded `"CALENDAR GATE"` verdict** — see the discrepancy
  note under Layer-2 above. Don't assume this repo's Layer-2 has a third
  verdict state; it doesn't, today.

## Reliability / heartbeat
- Silent-failure coverage is two-part: exit-code-red catches a failed email
  WITHIN a run; a **heartbeat** catches the run not happening at all.
- The heartbeat code already exists (fires only if `HEALTHCHECK_URL` is set;
  pings the URL on success, URL/`/fail` on email failure; fail-safe), and
  `dashboard.yml` now passes `HEALTHCHECK_URL: ${{ secrets.HEALTHCHECK_URL }}`
  through to the run step (fixed 2026-08-03 — previously the workflow's `env:`
  block didn't map this secret at all, so `cfg("HEALTHCHECK_URL")` would
  always read empty in Actions even after the secret was added; local `.env`
  fallback was unaffected). To activate: create a check at healthchecks.io
  (cron `17 22 * * 1-5`, UTC, ~1h grace), add an alert channel, and put its
  ping URL in the `HEALTHCHECK_URL` secret — the workflow wiring is done.

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
5. Signal rework — **PARTIALLY DONE 2026-08-03** (user-directed "optimise for
   correctness"): VIX backwardation reclassified as a bounce/exit cue (tile 17
   amber not red; Layer-2 input #2 swapped to `ts_accelerating`) and the credit
   tile level+RoC recalibration shipped. STILL OPEN: weighting vol-expansion in
   sizing, de-duplicating correlated tile clusters in `n_stress` (breadth 7/18,
   sentiment 15/16, rates 3/19), and the dual-red net-liquidity 13-week RoC
   recalibration (needs dated WALCL/TGA/RRP alignment + ledger calibration).

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
