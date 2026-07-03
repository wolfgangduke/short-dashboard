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

def build_fixtures(spy_daily_closes, spy_px):
    vols = [80_000_000.0] * len(spy_daily_closes)
    vols[-1] = 120_000_000.0  # breakdown volume 1.5x
    vols[-2] = 120_000_000.0  # in case the partial-bar guard drops the last
    return [
        # (host_substr, path_substr, payload)
        ("financialmodelingprep", "quote?symbol=SPY", [{"price": spy_px, "changePercentage": -2.5}]),
        ("financialmodelingprep", "quote?symbol=%5EVIX", [{"price": 28.0}]),
        ("financialmodelingprep", "GCUSD", [{"price": 2400.0}]),
        ("financialmodelingprep", "treasury-rates", [{"year2": 4.0, "year10": 3.5}]),
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

def run_scenario(name, spy_closes, spy_px, seed_state):
    d = tempfile.mkdtemp(prefix="short_%s_" % name)
    script = os.path.join(d, "short.py")
    shutil.copy(SRC, script)
    with open(os.path.join(d, "state.json"), "w") as f:
        json.dump(seed_state, f)
    os.environ["FMP_API_KEY"] = "test"
    os.environ["FRED_API_KEY"] = "test"
    os.environ.pop("GMAIL_USER", None)
    install_stub(build_fixtures(spy_closes, spy_px))
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
check("Exit rule" in gA["primary"], "2% exit rule stated")
html = gA["build_html"]()
check(">INITIATE SHORT<" in html, "email banner shows INITIATE SHORT")
check(gA["vol_ratio"] is not None and gA["vol_ratio"] >= 1.2, "volume gate computed & passed (%.2fx)" % gA["vol_ratio"])
check(gA["rr_value"] is not None and gA["rr_value"] >= 5.0, "R:R gate computed & passed (%.1f)" % gA["rr_value"])
check(gA["spx_above_200dma"] is False, "SPX below 200DMA confirmed")
check(gA["spx_above_10mema"] is False, "SPX below 10M EMA confirmed")
check("TRANSITION WINDOW" in gA["cal_sub"] or gA["cal_sub"] == "clear",
      "calendar tile valid (%s)" % gA["cal_sub"])
check("[Deficit $" in gA["fisc_sub"] and "Outlays YoY" in gA["fisc_sub"],
      "fiscal Point-19 format: %s" % gA["fisc_sub"])
check(gA["fisc_col"] == "red", "fiscal red (deficit>2T AND outlays>8%)")

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
check(">INITIATE SHORT<" not in htmlB, "banner does NOT show INITIATE SHORT")

print("=" * 70)
print("SCENARIO C: streak only 1/3 -> must stay WATCHING with streak blocker")
print("=" * 70)
gC = run_scenario("streak", descending(520, 400.5, 251), 400.0,
                  {"dual_red_streak": {"value": 1, "ts": "2026-07-01T00:00:00"}})
check(gC["initiate_short"] is False, "initiate_short is False")
check("dual-red streak 1/3" in gC["primary"], "streak blocker named: %s" % gC["primary"].split(" | ")[0])

print("=" * 70)
print("RESULT: " + ("*** FAILURES ***" if FAILED else "ALL CHECKS PASSED"))
sys.exit(1 if FAILED else 0)
