#!/usr/bin/env python3
"""End-to-end test harness for the SHORT monitor.
Stubs every external HTTP call with fixtures and runs the real script module,
then asserts the verdict engine fires / blocks correctly.
"""
import io, json, os, runpy, shutil, sys, tempfile, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "short_dashboard.py")

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def yahoo_daily(closes, vols=None, ts_start=1750000000):
    n = len(closes)
    return {"chart": {"result": [{
        "timestamp": [ts_start + i * 86400 for i in range(n)],
        "indicators": {"quote": [{
            "close": closes,
            "volume": vols if vols is not None else [80_000_000] * n,
        }]}}]}}

def fred(vals):
    return {"observations": [{"value": str(v)} for v in vals]}

def fred_dated(vals, end=None):
    """FRED-style observations WITH dates (newest-first, matches sort_order=desc),
    weekday-only spacing. Only _fred_series_dated() readers (the 30Y duration-
    stress tile) need dates -- other FRED fixtures use the plain fred() above."""
    import datetime as _dt
    end = end or _dt.date.today()
    out, d, i = [], end, 0
    while i < len(vals):
        if d.weekday() < 5:
            out.append({"date": d.isoformat(), "value": str(vals[i])})
            i += 1
        d -= _dt.timedelta(days=1)
    return {"observations": out}

def descending(a, b, n):
    step = (a - b) / (n - 1)
    return [round(a - step * i, 2) for i in range(n)]

def ascending(a, b, n):
    return list(reversed(descending(b, a, n)))

WSJ = {"data": {"instrumentSets": [{
    "headerFields": [{"label": "NYSE"}],
    "instruments": [
        {"id": "advances", "latestClose": "800", "previousClose": "900"},
        {"id": "declines", "latestClose": "2000", "previousClose": "1900"},
    ]}]}}

COT = [{
    "report_date_as_yyyy_mm_dd": "2026-06-30T00:00:00",
    "asset_mgr_positions_long": "400000", "asset_mgr_positions_short": "150000",
    "lev_money_positions_long": "200000", "lev_money_positions_short": "450000",
    "change_in_lev_money_long": "1000", "change_in_lev_money_short": "9000",
}]

def build_fixtures(spy_daily_closes, spy_px, vols=None, extra=None):
    """`vols` overrides SPY daily volumes (list, may contain None).
    `extra` is a list of (host, path, payload) PREPENDED so the entries
    shadow the matching defaults (first match wins in install_stub)."""
    if vols is None:
        vols = [80_000_000.0] * len(spy_daily_closes)
        vols[-1] = 120_000_000.0  # breakdown volume 1.5x
        vols[-2] = 120_000_000.0  # in case the partial-bar guard drops the last
    return list(extra or []) + [
        # (host_substr, path_substr, payload)
        ("financialmodelingprep", "quote?symbol=SPY", [{"price": spy_px, "changePercentage": -2.5}]),
        ("financialmodelingprep", "quote?symbol=%5EVIX", [{"price": 28.0}]),
        ("financialmodelingprep", "GCUSD", [{"price": 2400.0}]),
        ("financialmodelingprep", "treasury-rates", [{"year2": 4.0, "year10": 3.5, "year30": 4.7}]),
        # 90 flat sub-5.00% closes -> tile 19 GREEN by default so baseline
        # scenarios' n_red/n_stress counts are unaffected. Override via
        # `extra=` (see test_30y_duration_stress below) to exercise red paths.
        ("stlouisfed", "series_id=DGS30", fred_dated([4.70] * 90)),
        ("financialmodelingprep", "sector-performance-snapshot",
            [{"changesPercentage": -1.0}] * 8 + [{"changesPercentage": 0.5}] * 2),
        ("financialmodelingprep", "economic-calendar", []),
        ("stlouisfed", "series_id=WALCL", fred([6500000, 6600000])),
        ("stlouisfed", "series_id=WTREGEN", fred([700000, 700000])),
        ("stlouisfed", "series_id=RRPONTSYD", fred([500, 500])),
        ("stlouisfed", "series_id=BAMLH0A0HYM2", fred([4.5, 4.2])),
        ("stlouisfed", "series_id=DTWEXBGS", fred([120.0, 119.0])),
        ("stlouisfed", "series_id=MTSDS133FMS", fred([-200000] * 12)),
        ("stlouisfed", "series_id=MTSO133FMS", fred([590000] * 12 + [540000] * 12)),
        ("stlouisfed", "series_id=MTSR133FMS", fred([420000] * 12)),
        ("stlouisfed", "series_id=A091RC1Q027SBEA", fred([1100.0])),
        ("yahoo", "chart/SPY?range=1y", yahoo_daily(spy_daily_closes, vols)),
        ("yahoo", "chart/SPY?range=3y&interval=1mo",
            yahoo_daily(descending(520, 400, 36) if spy_px < 450 else ascending(350, 500, 36))),
        ("yahoo", "chart/SPY?range=6mo", yahoo_daily(descending(470, 400, 126))),
        ("yahoo", "chart/RSP?range=6mo", yahoo_daily(descending(130, 100, 126))),
        ("yahoo", "chart/%5EVIX3M?range=6mo", yahoo_daily(descending(24, 25, 126))),
        ("yahoo", "chart/%5EVIX?range=6mo", yahoo_daily(descending(20, 28, 126))),
        ("yahoo", "chart/%5EVVIX?range=10d", yahoo_daily([110.0, 118.0])),
        ("yahoo", "chart/%5EVIX?range=10d", yahoo_daily([26.0, 28.0])),
        ("cftc", "gpe5-46if", COT),
        ("wsj.com", "marketsdiary", WSJ),
        ("naaim", "exposure", "<tr><td>06/25/2026</td>\n<td>95.5</td></tr>"),
        ("aaii", "sent", "Bullish: 58.2%  Neutral: 21.7%  Bearish: 20.1%"),
        ("finviz", "", "Advancing (1200) Declining (1800)"),
    ]

class FakeResp:
    def __init__(self, payload):
        self._b = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False

def install_stub(fixtures):
    def fake_urlopen(req, timeout=None, context=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for host, path, payload in fixtures:
            if host in url and path in url:
                return FakeResp(payload)
        raise urllib.error.URLError("no fixture for: " + url)
    urllib.request.urlopen = fake_urlopen
    time.sleep = lambda s: None

def run_scenario(name, spy_closes, spy_px, seed_state, vols=None, extra=None):
    d = tempfile.mkdtemp(prefix="short_%s_" % name)
    script = os.path.join(d, "short.py")
    shutil.copy(SRC, script)
    with open(os.path.join(d, "state.json"), "w") as f:
        json.dump(seed_state, f)
    os.environ["FMP_API_KEY"] = "test"
    os.environ["FRED"] = "test"
    os.environ.pop("GMAIL_USER", None)
    install_stub(build_fixtures(spy_closes, spy_px, vols=vols, extra=extra))
    g = runpy.run_path(script)  # run_name != __main__ -> no email/exit
    return g

def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        global FAILED
        FAILED = True

FAILED = False

print("=" * 70)
print("SCENARIO A: all rules satisfied -> must fire INITIATE SHORT")
print("=" * 70)
gA = run_scenario("fire", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}})
check(gA["initiate_short"] is True, "initiate_short is True")
check(gA["primary"].startswith("INITIATE SHORT"), "primary starts with INITIATE SHORT")
check("Size:" in gA["primary"], "sizing ladder present in verdict")
# ---- sizing de-dup + vol-edge weighting (roadmap #5, 2026-08-07) ----
check(gA["n_stress"] == gA["_n_stress_raw"] - gA["_cluster_dupes"] + gA["_vol_edge_bonus"],
      "n_stress identity holds: raw - cluster dupes + vol-edge bonus")
check(gA["_cluster_dupes"] >= 2,
      "correlated pairs (breadth 7/18, sentiment 15/16 both red here) each count once (%d dupes)"
      % gA["_cluster_dupes"])
check(gA["_vol_edge_bonus"] == 0, "no vol-edge bonus without vol expansion")
check(gA["netliq_13w_delta"] is None,
      "13w netliq RoC fails safe to None on undated fixtures (no crash, no fabrication)")
check("Exit rule" in gA["primary"], "2% exit rule stated")
html = gA["build_html"]()
check(">CRASH ALERT<" in html, "email banner shows CRASH ALERT (post-#21 red pill)")
check(gA["vol_ratio"] is not None and gA["vol_ratio"] >= 1.2, "volume gate computed & passed (%.2fx)" % gA["vol_ratio"])
check(gA["rr_value"] is not None and gA["rr_value"] >= 5.0, "R:R gate computed & passed (%.1f)" % gA["rr_value"])
check(gA["spx_above_200dma"] is False, "SPX below 200DMA confirmed")
check(gA["spx_above_10mema"] is False, "SPX below 10M EMA confirmed")
check("TRANSITION WINDOW" in gA["cal_sub"] or gA["cal_sub"] == "clear",
      "calendar tile valid (%s)" % gA["cal_sub"])
check("[Deficit $" in gA["fisc_sub"] and "Outlays YoY" in gA["fisc_sub"],
      "fiscal Point-19 format: %s" % gA["fisc_sub"])
check(gA["fisc_col"] == "red", "fiscal red (deficit>2T AND outlays>8%)")
check(gA["y30ds_col"] == "green", "tile 19 green on flat sub-5% 30Y fixture")
# Tile 17 reclassification (2026-08-03): fixtures put VIX 28 / VIX3M 25 =
# ratio 1.12 BACKWARDATION -- must render amber + contrarian note, never red.
check(gA["vix_ts_col"] == "amber", "tile 17 backwardation renders AMBER (contrarian-bullish), not red")
check("contrarian-BULLISH" in gA["vix_ts_sub"], "tile 17 carries the bounce/exit-cue note")
# Tile 4 fallback path (2-obs fixture, widening at 4.5% >= 4.0 level) -> red.
check(gA["credit_col"] == "red", "credit red: widening at stress level (4.5%) via short-history fallback")
check("Streak" in gA["y30ds_sub"] and "YTD days>5%" in gA["y30ds_sub"],
      "tile 19 sub-note format: %s" % gA["y30ds_sub"])

print("=" * 70)
print("SCENARIO A2: 30Y duration stress -> persistent >5% run fires tile 19 red")
print("=" * 70)
gA2 = run_scenario("dur30y", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                  extra=[("stlouisfed", "series_id=DGS30",
                          fred_dated([5.10] * 20 + [4.80] * 70))])
check(gA2["y30ds_col"] == "red", "tile 19 red: 20/60 sessions >5% clears the 25% threshold")
check(gA2["y30_hist"][0] == 5.10, "most-recent close read correctly (newest-first)")

print("=" * 70)
print("SCENARIO A3: 30Y hard override -> single close >5.50% forces red regardless of streak")
print("=" * 70)
gA3 = run_scenario("dur30yhard", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                  extra=[("stlouisfed", "series_id=DGS30",
                          fred_dated([5.55] + [4.60] * 89))])
check(gA3["y30ds_col"] == "red", "tile 19 red: hard override fires even with only 1/60 above 5.00%")

print("=" * 70)
print("SCENARIO A4: 30Y data unavailable -> tile 19 degrades gray, never crashes the run")
print("=" * 70)
gA4 = run_scenario("dur30ymissing", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                  extra=[("stlouisfed", "series_id=DGS30", {"observations": []}),
                         ("financialmodelingprep", "treasury-rates",
                          [{"year2": 4.0, "year10": 3.5}])])  # no year30 key
check(gA4["y30ds_col"] == "gray", "tile 19 gray when both FMP year30 and FRED DGS30 are empty")
check(gA4["y30ds_sub"] == "unavailable", "tile 19 sub-note reads 'unavailable', never fabricated")
check(gA4["initiate_short"] is True, "missing tile 19 data does NOT block other tiles' verdict")

print("=" * 70)
print("SCENARIO A5: 30Y dated fetch fails, but a cached verdict from a PRIOR run exists -> falls back to it, not gray")
print("=" * 70)
gA5 = run_scenario("dur30ylastknown", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"},
                   "y30ds_sub": {"value": "[30Y 5.20% | Streak 18/60 sessions (30%) | YTD days>5%: 25]",
                                 "ts": "2026-07-20T00:00:00"},
                   "y30ds_col": {"value": "red", "ts": "2026-07-20T00:00:00"}},
                  extra=[("stlouisfed", "series_id=DGS30", {"observations": []}),
                         ("financialmodelingprep", "treasury-rates",
                          [{"year2": 4.0, "year10": 3.5}])])  # no year30 key either
check(gA5["y30ds_col"] == "red", "tile 19 falls back to the cached RED verdict, not gray")
check(gA5["y30ds_sub"].endswith("(last known)"),
      "tile 19 sub-note tagged '(last known)': %s" % gA5["y30ds_sub"])
check("Streak 18/60" in gA5["y30ds_sub"], "cached streak numbers carried through, not fabricated fresh")

print("=" * 70)
print("SCENARIO B: SPX ABOVE 200DMA -> must stay WATCHING")
print("=" * 70)
gB = run_scenario("block200", ascending(350, 500.5, 251), 500.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}})
check(gB["initiate_short"] is False, "initiate_short is False")
check("INITIATE blocked by" in gB["primary"], "blockers listed")
check("SPX above 200DMA" in gB["primary"], "200DMA named as blocker")
htmlB = gB["build_html"]()
check(">WATCHING<" in htmlB, "email banner shows WATCHING")
check(">CRASH ALERT<" not in htmlB, "banner does NOT show the CRASH ALERT red pill")
check(gB["catalyst_auto"] is False, "catalyst auto off at a fresh high (not a breakdown)")
# Trend-regime gate (2026-08-04): in a confirmed uptrend Layer-2 must never call
# for a starter short, however many of its 2-of-3 inputs are lit. Guards the
# real 2026-08-03/04 regression where ENTRY fired with SPX ripping to new highs.
if gB["spx_above_200dma"] is True and gB["spx_above_10mema"] is True:
    check(not gB["layer2"].startswith("ENTRY SIGNAL"),
          "Layer-2 does NOT fire ENTRY in a confirmed uptrend (got: %s)" % gB["layer2"][:60])

print("=" * 70)
print("SCENARIO B2: 2-of-3 Layer-2 inputs LIT but SPX in a confirmed uptrend")
print("             -> ENTRY must be HELD (the 2026-08-03/04 regression)")
print("=" * 70)
# Ascending price (above 200DMA and 10M EMA) while VIX9D inverts (gamma_flip)
# and the VIX/VIX3M curve races toward inversion (ts_accelerating) = 2 lit
# inputs that, before this gate, produced "ENTRY SIGNAL - probe size" at a
# fresh market high. Must now render WAIT.
gB2 = run_scenario("uptrend_l2", ascending(350, 500.5, 251), 500.0,
                   {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                   extra=[("yahoo", "chart/%5EVIX9D?range=6mo",
                           yahoo_daily([30.0] * 126)),
                          ("yahoo", "chart/%5EVIX?range=6mo",
                           yahoo_daily([20.0] * 120 + [21.0, 23.0, 25.0, 27.0, 29.0])),
                          ("yahoo", "chart/%5EVIX3M?range=6mo",
                           yahoo_daily([25.0] * 125))])
check(gB2["spx_above_200dma"] is True, "SPX confirmed ABOVE 200DMA")
check(gB2["spx_above_10mema"] is True, "SPX confirmed ABOVE 10M EMA")
check(gB2["_l2_signals"] >= 2,
      "2-of-3 Layer-2 inputs genuinely lit (%d/3)" % gB2["_l2_signals"])
check(not gB2["layer2"].startswith("ENTRY SIGNAL"),
      "no starter short called in an uptrend (got: %s)" % gB2["layer2"][:70])
check(gB2["layer2"].startswith("WAIT - Layer-2"),
      "held verdict names what fired: %s" % gB2["layer2"][:70])
check("HELD" in gB2["layer2"], "verdict text explains the hold")
# The plain-English summary must not tell Bryan to open a starter position.
check("starter" not in gB2["layman"].lower(),
      "plain-English summary does NOT recommend a starter short")
check(gB2["_vol_edge_bonus"] == 0,
      "vol-edge sizing bonus never granted in a confirmed uptrend")

print("=" * 70)
print("SCENARIO C: streak only 1/3 -> must stay WATCHING with streak blocker")
print("=" * 70)
gC = run_scenario("streak", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 1, "ts": "2026-07-01T00:00:00"}})
check(gC["initiate_short"] is False, "initiate_short is False")
# Robust to the session guard: a streak seeded on a prior date legitimately
# advances by one on a new trading session, so assert the blocker is NAMED and
# still <3 rather than hardcoding the (date-sensitive) count.
import re as _reC
_mC = _reC.search(r"dual-red streak (\d+)/3", gC["primary"])
check(bool(_mC) and int(_mC.group(1)) < 3,
      "streak blocker named & <3: %s" % gC["primary"].split(" | ")[0])

print("=" * 70)
print("SCENARIO D: keyless replacements -> vol-expansion + catalyst auto-confirm")
print("=" * 70)
gD = run_scenario("volexp", descending(470, 452, 246) + [445, 435, 422, 410, 400], 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}})
check(gD["vol_expansion"] is True, "realized-vol expansion fires on a 5-day vol burst")
check(gD["gamma_flip"] is True, "Layer-2 vol-regime input on (replaces manual GEX)")
check(gD["_vol_edge_bonus"] == 1,
      "vol expansion in a confirmed downtrend earns the +1 sizing weight (backtest edge)")
check(gD["catalyst_auto"] is True, "catalyst auto-confirmed from price/volume (not manual)")
check(gD["catalyst_on"] is True, "catalyst_on True via auto path")

print("=" * 70)
print("SCENARIO D2: netliq w/w BOUNCE mid-drain -> 13w sustained-drain OR-leg")
print("             keeps dual-red input #2 red (2026-08-07 miss-mode fix)")
print("=" * 70)
# Dated WALCL fixture: last ~50 weekdays at 6,350,000 ($6.35T), older 50 at
# 6,500,000 -> week-over-week reads FLAT ("rising", the bounce) while the
# 13-week delta is -150 $bn, past the -100 $bn sustained-drain floor. The
# same dated payload serves both fred_series (w/w) and _fred_series_dated
# (13w) since the parsers share the URL substring match.
gD2 = run_scenario("nl13w", descending(520, 400.5, 251), 400.0,
                   {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                   extra=[("stlouisfed", "series_id=WALCL",
                           fred_dated([6350000] * 50 + [6500000] * 50)),
                          ("stlouisfed", "series_id=WTREGEN",
                           fred_dated([700000] * 100)),
                          ("stlouisfed", "series_id=RRPONTSYD",
                           fred_dated([500] * 100))])
check(gD2["netliq_dir"] == "rising",
      "w/w alone reads NOT declining (the mid-drain bounce)")
check(gD2["netliq_13w_delta"] is not None and gD2["netliq_13w_delta"] <= -100.0,
      "13w delta computed and past the -100 $bn drain floor (%s)"
      % gD2["netliq_13w_delta"])
check(gD2["netliq_decl"] is True,
      "dual-red input #2 stays red via the 13w OR-leg despite the w/w bounce")
check(gD2["p"][7][2] == "red", "tile 8 renders red on the sustained-drain leg")
check("SUSTAINED DRAIN" in gD2["p"][7][1],
      "tile 8 sub-text names the sustained drain as a gate input")

print("=" * 70)
print("SCENARIO E: 10M-EMA gate blocks + banner regression (INITIATE SHORT")
print("            blocked text in primary must still render WATCHING)")
print("=" * 70)
gE = run_scenario("block10m", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                  extra=[("yahoo", "chart/SPY?range=3y&interval=1mo",
                          yahoo_daily(descending(520, 300, 36)))])
check(gE["spx_above_10mema"] is True, "SPX confirmed ABOVE 10M EMA")
check(gE["initiate_short"] is False, "initiate_short is False")
check("SPX above 10M EMA" in gE["primary"], "10M-EMA named as blocker")
check("INITIATE SHORT blocked" in gE["primary"],
      "primary carries the 'INITIATE SHORT blocked' gate note")
htmlE = gE["build_html"]()
check(">WATCHING<" in htmlE, "banner regression: renders WATCHING")
check(">CRASH ALERT<" not in htmlE,
      "banner regression: red CRASH ALERT pill NOT shown despite 'INITIATE SHORT' in text")

print("=" * 70)
print("SCENARIO F: breakdown volume below 1.2x -> volume gate blocks")
print("=" * 70)
gF = run_scenario("blockvol", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                  vols=[80_000_000.0] * 251)
check(gF["initiate_short"] is False, "initiate_short is False")
check(gF["vol_ratio"] is not None and gF["vol_ratio"] < 1.2,
      "volume ratio computed but sub-threshold (%.2fx)" % gF["vol_ratio"])
check("< 1.2x" in gF["primary"], "volume named as blocker with ratio")
check(gF["catalyst_auto"] is False, "catalyst auto stays off without breakdown volume")

print("=" * 70)
print("SCENARIO G: R:R below 5.0 (small existing drawdown, risk capped at 2%) -> R:R gate blocks")
print("=" * 70)
# Risk is capped at 2% of entry (see the 2026-07-30 R:R fix): a distant 5-day
# swing high no longer inflates the denominator, so blocking R:R now requires
# a genuinely SMALL reward (existing drawdown from the 52wk high), not just a
# wide stop. hi52 stays modest (430, from the start of the series) and the
# 5-day swing high (412) sits comfortably above the 2% floor -> reward=30,
# risk=capped 8.0 (2% of 400) -> R:R 3.75, still < 5.0.
gG = run_scenario("blockrr",
                  descending(430, 402, 246) + [412.0, 410.0, 408.0, 405.0, 400.5],
                  400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}})
check(gG["initiate_short"] is False, "initiate_short is False")
check(gG["rr_value"] is not None and gG["rr_value"] < 5.0,
      "R:R computed but sub-threshold (%.1f)" % gG["rr_value"])
check("< 5.0" in gG["primary"], "R:R named as blocker with value")

print("=" * 70)
print("SCENARIO H: FOMC within 2 days -> calendar/event-risk gate blocks")
print("=" * 70)
import datetime as _dt
_fomc_date = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
gH = run_scenario("blockfomc", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                  extra=[("financialmodelingprep", "economic-calendar",
                          [{"event": "FOMC - Fed Interest Rate Decision",
                            "country": "US", "date": _fomc_date}])])
check(gH["fomc_days"] is not None and gH["fomc_days"] <= 2,
      "FOMC detected inside 2-day window (%sd)" % gH["fomc_days"])
check(gH["initiate_short"] is False, "initiate_short is False")
check("FOMC within 2 days" in gH["primary"], "FOMC named as blocker")
check("FOMC in" in gH["cal_sub"], "calendar tile flags the FOMC date")

print("=" * 70)
print("SCENARIO I: NO monthly closes -> 10M-EMA gate UNKNOWN, fails CLOSED")
print("=" * 70)
gI = run_scenario("nomonthly", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                  extra=[("yahoo", "chart/SPY?range=3y&interval=1mo",
                          yahoo_daily([]))])
check(gI["spx_10mema"] is None, "10M EMA never fabricated (None)")
check(gI["spx_above_10mema"] is None, "10M-EMA gate is UNKNOWN (None)")
check(gI["initiate_short"] is False, "initiate_short is False (fail-closed)")
check("10M-EMA gate unknown" in gI["primary"], "unknown gate named as blocker")

print("=" * 70)
print("SCENARIO J: NO volume data -> volume gate UNKNOWN, fails CLOSED")
print("=" * 70)
gJ = run_scenario("novol", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                  vols=[None] * 251)
check(gJ["vol_ratio"] is None, "volume ratio never fabricated (None)")
check(gJ["initiate_short"] is False, "initiate_short is False (fail-closed)")
check("volume unknown" in gJ["primary"], "unknown volume named as blocker")
check(gJ["catalyst_auto"] is False, "catalyst auto off without volume confirmation")

print("=" * 70)
print("SCENARIO K: NO daily history -> 200DMA/volume/R:R all UNKNOWN, fail CLOSED")
print("=" * 70)
gK = run_scenario("nohist", descending(470, 455, 25), 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}})
check(gK["rr_value"] is None, "R:R never fabricated (None)")
check(gK["vol_ratio"] is None, "volume ratio never fabricated (None)")
check(gK["spx_above_200dma"] is None, "200DMA gate is UNKNOWN (None)")
check(gK["initiate_short"] is False, "initiate_short is False (fail-closed)")
check("R:R unknown" in gK["primary"], "R:R unknown named as blocker")
check("volume unknown" in gK["primary"], "volume unknown named as blocker")
check("200DMA gate unknown" in gK["primary"], "200DMA unknown named as blocker")
check(gK["vol_expansion"] is False, "vol-expansion stays off without history")
htmlK = gK["build_html"]()
check(">WATCHING<" in htmlK and ">CRASH ALERT<" not in htmlK,
      "banner shows WATCHING on missing data, never the red pill")

print("=" * 70)
print("SCENARIO L: VIX9D/VIX front-of-curve inversion (issue #17)")
print("=" * 70)
gL1 = run_scenario("v9don", descending(520, 400.5, 251), 400.0,
                   {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                   extra=[("yahoo", "chart/%5EVIX9D?range=6mo",
                           yahoo_daily([30.0] * 126))])
check(gL1["vix9d_inversion"] is True, "inversion fires when VIX9D > VIX (%.3f)" % gL1["vix9d_ratio"])
check(gL1["gamma_flip"] is True, "Layer-2 vol-regime input lights via VIX9D path")
check("FRONT-OF-CURVE INVERTED" in gL1["vix_ts_sub"], "tile 17 flags the inversion")
check(gL1["ts_accelerating"] is False, "TS velocity stays off on a stable ratio series")
gL2 = run_scenario("v9doff", descending(520, 400.5, 251), 400.0,
                   {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                   extra=[("yahoo", "chart/%5EVIX9D?range=6mo",
                           yahoo_daily([20.0] * 126))])
check(gL2["vix9d_inversion"] is False, "no inversion when VIX9D < VIX (%.3f)" % gL2["vix9d_ratio"])
check(gL2["gamma_flip"] is False, "vol-regime input off (no expansion, no inversion, no manual)")
gL3 = run_scenario("v9dmiss", descending(520, 400.5, 251), 400.0,
                   {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                   extra=[("yahoo", "chart/%5EVIX9D?range=6mo", yahoo_daily([]))])
check(gL3["vix9d_ratio"] is None, "ratio never fabricated when ^VIX9D missing (None)")
check(gL3["vix9d_inversion"] is False, "inversion off fail-safe when ^VIX9D missing")

print("=" * 70)
print("SCENARIO M: term-structure VELOCITY - fast flattening (issue #17)")
print("=" * 70)
gM = run_scenario("tsvel", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                  extra=[("yahoo", "chart/%5EVIX?range=6mo",
                          yahoo_daily([20.0] * 120 + [21.0, 23.0, 25.0, 27.0, 29.0])),
                         ("yahoo", "chart/%5EVIX3M?range=6mo",
                          yahoo_daily([25.0] * 125))])
check(gM["ts_velocity"] is not None and gM["ts_velocity"] >= 0.08,
      "velocity computed on a 5-session vol ramp (%+.3f)" % gM["ts_velocity"])
check(gM["ts_accelerating"] is True, "ACCELERATING flag fires (fast flattening near inversion)")
check("ACCELERATING" in gM["vix_ts_sub"], "tile 17 shows the flattening rate")

print("=" * 70)
print("SCENARIO M2: term-structure VELOCITY accelerating while still CONTANGO")
print("             -> tile 17 must render at least amber, not green")
print("=" * 70)
# Unlike Scenario M (the ramp there ends in BACKWARDATION, ratio 1.16), this
# fixture keeps the final ratio just under 1.0 so the regime stays CONTANGO
# while the curve is still racing toward inversion (velocity +0.095/5d,
# ratio 0.965 >= the 0.95 floor). Regression test for the 2026-07-30 fix:
# the sub-text flagged ACCELERATING but vix_ts_col stayed green regardless.
gM2 = run_scenario("tsvelcontango", descending(520, 400.5, 251), 400.0,
                   {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                   extra=[("yahoo", "chart/%5EVIX?range=6mo",
                           yahoo_daily([17.0] * 120 + [17.4, 17.8, 18.2, 18.6, 19.0, 19.3])),
                          ("yahoo", "chart/%5EVIX3M?range=6mo",
                           yahoo_daily([20.0] * 126))])
check(gM2["_vts"]["regime"] == "CONTANGO", "regime is still CONTANGO (ratio < 1.0)")
check(gM2["ts_accelerating"] is True, "ACCELERATING flag fires under contango")
check(gM2["vix_ts_col"] == "amber",
      "tile 17 upgraded to amber despite CONTANGO (was green pre-fix)")

print("=" * 70)
print("SCENARIO N: PRE-ALERT composite - top-proximity divergence (issue #17)")
print("=" * 70)
_pa_seed = {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"},
            "breadth_proxy_dir": {"value": "NARROWING", "ts": "2026-07-01T00:00:00"},
            "breadth_proxy_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}}
gN = run_scenario("prealert", ascending(350, 500.5, 251), 500.0, dict(_pa_seed))
check(gN["pre_alert"] is True, "PRE-ALERT fires: narrowing streak + backwardation + near 52wk high")
check("PRE-ALERT" in gN["pre_alert_txt"] and "narrowing" in gN["pre_alert_txt"],
      "pre-alert text names the legs: %s" % gN["pre_alert_txt"][:60])
check(gN["initiate_short"] is False, "PRE-ALERT never gates: INITIATE still blocked (200DMA above)")
htmlN = gN["build_html"]()
check("PRE-ALERT" in htmlN, "amber PRE-ALERT strip rendered in email")
check(">WATCHING<" in htmlN and ">CRASH ALERT<" not in htmlN,
      "banner stays WATCHING - PRE-ALERT never shows the red pill")
check("PRE-ALERT" in gN["plain"], "plain-text email carries the PRE-ALERT line")
gN2 = run_scenario("panohigh", descending(520, 400.5, 251), 400.0, dict(_pa_seed))
check(gN2["pre_alert"] is False, "leg check: no PRE-ALERT away from the highs")
gN3 = run_scenario("pashort", ascending(350, 500.5, 251), 500.0,
                   {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"},
                    "breadth_proxy_dir": {"value": "NARROWING", "ts": "2026-07-01T00:00:00"},
                    "breadth_proxy_streak": {"value": 1, "ts": "2026-07-01T00:00:00"}})
check(gN3["pre_alert"] is False, "leg check: narrowing streak < 3 blocks PRE-ALERT")
gN4 = run_scenario("panovol", ascending(350, 500.5, 251), 500.0, dict(_pa_seed),
                   extra=[("yahoo", "chart/%5EVIX?range=6mo",
                           yahoo_daily(descending(20, 18, 126)))])
check(gN4["pre_alert"] is False, "leg check: no vol input (contango, no expansion) blocks PRE-ALERT")

print("=" * 70)
print("SCENARIO O: credit tile level+RoC recalibration (2026-08-03)")
print("=" * 70)
# Calm regime: 25 obs flat at 2.84% -> Δ21d = 0, level < 4.0 -> GREEN even
# though the last day ticked up 1bp (the old rule would have gone red).
gO1 = run_scenario("creditcalm", descending(520, 400.5, 251), 400.0,
                   {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                   extra=[("stlouisfed", "series_id=BAMLH0A0HYM2",
                           fred([2.84, 2.83] + [2.84] * 23))])
check(gO1["credit_col"] == "green",
      "calm spreads (2.84%%, flat 21d) render GREEN despite a 1bp daily uptick (got %s)" % gO1["credit_col"])
check("+0.00pp/21d" in gO1["credit_sub"] or "-0.00pp/21d" in gO1["credit_sub"],
      "sub-text shows the 21-session RoC: %s" % gO1["credit_sub"])
# Blowout regime: level still sub-4.0 but +0.80pp over 21 sessions -> RED
# (rate-of-change stress fires before the level threshold is reached).
gO2 = run_scenario("creditblow", descending(520, 400.5, 251), 400.0,
                   {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                   extra=[("stlouisfed", "series_id=BAMLH0A0HYM2",
                           fred([3.60, 3.55] + [3.30] * 19 + [2.80] * 4))])
check(gO2["credit_col"] == "red",
      "1-month blowout (+0.80pp/21d) renders RED below the 4.0%% level (got %s)" % gO2["credit_col"])
# Material-but-subcritical widening: +0.30pp/21d at a calm level -> AMBER.
gO3 = run_scenario("creditwatch", descending(520, 400.5, 251), 400.0,
                   {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
                   extra=[("stlouisfed", "series_id=BAMLH0A0HYM2",
                           fred([3.10, 3.08] + [2.95] * 19 + [2.80] * 4))])
check(gO3["credit_col"] == "amber",
      "material widening (+0.30pp/21d, calm level) renders AMBER (got %s)" % gO3["credit_col"])

print("=" * 70)
print("RESULT: " + ("*** FAILURES ***" if FAILED else "ALL CHECKS PASSED"))
sys.exit(1 if FAILED else 0)
