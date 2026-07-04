#!/usr/bin/env python3
"""Escalation drill simulator for the SHORT monitor (issue #14).

Runs the REAL short_dashboard.py pipeline with injected metrics and renders
the actual email HTML to reports/simulation_<UTCSTAMP>.html so the
INITIATE SHORT path can be verified end-to-end without touching production.

Hard guarantees:
  * NO email is ever sent: the module is executed with run_name != "__main__"
    (send_email lives under the __main__ guard) AND the GMAIL_* secrets are
    scrubbed from the environment as a second lock.
  * NO writes to the repo state.json: the script runs against a throwaway
    copy in a temp directory (its HERE/state.json is the seeded temp file),
    and CACHE.save() only executes under __main__ anyway. The repo
    state.json is snapshotted before and verified byte-identical after.
  * Every rendered page carries a "SIMULATION DRILL - NOT A SIGNAL" banner
    strip at the very top, in addition to the normal verdict pill, so a
    drill can never be mistaken for a live signal.

Usage:
    python sim_drill.py --list
    python sim_drill.py --scenario full_escalation
    python sim_drill.py --scenario blocked_fomc --out reports
"""
import argparse, datetime, io, json, os, re, runpy, shutil, sys, tempfile
import time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "short_dashboard.py")

# ---------------------------------------------------------------------------
# Fixture builders (kept in sync with test_harness.py)
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

def build_fixtures(spy_daily_closes, spy_px, vols=None, extra=None):
    """Full fixture set. `vols` overrides SPY daily volumes; `extra` is a
    list of (host, path, payload) PREPENDED so they shadow the defaults."""
    if vols is None:
        vols = [80_000_000.0] * len(spy_daily_closes)
        if len(vols) >= 2:
            vols[-1] = 120_000_000.0  # breakdown volume 1.5x
            vols[-2] = 120_000_000.0  # in case the partial-bar guard drops the last
    base = [
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
    return list(extra or []) + base

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

# ---------------------------------------------------------------------------
# Scenario library. Every scenario returns kwargs for run_drill plus a one-
# line expectation used in the console summary.
# ---------------------------------------------------------------------------
STREAK3 = {"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}}

def _fomc_tomorrow():
    d = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    return [("financialmodelingprep", "economic-calendar",
             [{"event": "FOMC - Fed Interest Rate Decision", "country": "US",
               "date": d}])]

def get_scenario(name):
    S = {}
    S["full_escalation"] = dict(
        closes=descending(520, 400.5, 251), spy_px=400.0, seed=STREAK3,
        expect="INITIATE SHORT fires: red pill + full escalation head")
    S["blocked_200dma"] = dict(
        closes=ascending(350, 500.5, 251), spy_px=500.0, seed=STREAK3,
        expect="WATCHING: blocked by SPX above 200DMA")
    S["blocked_streak"] = dict(
        closes=descending(520, 400.5, 251), spy_px=400.0,
        seed={"dual_red_streak": {"value": 1, "ts": "2026-07-01T00:00:00"}},
        expect="WATCHING: blocked by dual-red streak 1/3")
    S["blocked_10m_ema"] = dict(
        closes=descending(520, 400.5, 251), spy_px=400.0, seed=STREAK3,
        extra=[("yahoo", "chart/SPY?range=3y&interval=1mo",
                yahoo_daily(descending(520, 300, 36)))],
        expect="WATCHING: blocked by SPX above 10M EMA (monthly gate)")
    S["blocked_volume"] = dict(
        closes=descending(520, 400.5, 251), spy_px=400.0, seed=STREAK3,
        vols=[80_000_000.0] * 251,
        expect="WATCHING: blocked by breakdown volume < 1.2x")
    S["blocked_rr"] = dict(
        closes=descending(520, 402, 246) + [447.0, 445.0, 430.0, 415.0, 400.5],
        spy_px=400.0, seed=STREAK3,
        expect="WATCHING: blocked by R:R < 5.0 (stop at 5-day swing high)")
    S["blocked_fomc"] = dict(
        closes=descending(520, 400.5, 251), spy_px=400.0, seed=STREAK3,
        extra=_fomc_tomorrow(),
        expect="WATCHING: blocked by FOMC within 2 days")
    S["missing_monthly"] = dict(
        closes=descending(520, 400.5, 251), spy_px=400.0, seed=STREAK3,
        extra=[("yahoo", "chart/SPY?range=3y&interval=1mo", yahoo_daily([]))],
        expect="WATCHING: 10M-EMA gate UNKNOWN fails CLOSED")
    S["missing_volume"] = dict(
        closes=descending(520, 400.5, 251), spy_px=400.0, seed=STREAK3,
        vols=[None] * 251,
        expect="WATCHING: volume gate UNKNOWN fails CLOSED")
    S["missing_history"] = dict(
        closes=descending(470, 455, 25), spy_px=400.0, seed=STREAK3,
        expect="WATCHING: no daily history -> 200DMA/volume/R:R all UNKNOWN, fail CLOSED")
    S["pre_alert"] = dict(
        closes=ascending(350, 500.5, 251), spy_px=500.0,
        seed={"dual_red_streak": {"value": 3, "ts": "2026-07-01T00:00:00"},
              "breadth_proxy_dir": {"value": "NARROWING", "ts": "2026-07-01T00:00:00"},
              "breadth_proxy_streak": {"value": 3, "ts": "2026-07-01T00:00:00"}},
        expect="WATCHING + amber PRE-ALERT strip (narrowing near highs + vol input)")
    if name not in S:
        raise SystemExit("unknown scenario %r; use --list" % name)
    return S[name]

SCENARIO_NAMES = ["full_escalation", "blocked_200dma", "blocked_streak",
                  "blocked_10m_ema", "blocked_volume", "blocked_rr",
                  "blocked_fomc", "missing_monthly", "missing_volume",
                  "missing_history", "pre_alert"]

BANNER = ('<div style="background:#b91c1c;color:#ffffff;border:4px dashed #ffffff;'
          'outline:4px solid #b91c1c;text-align:center;padding:16px 10px;'
          'font:700 18px/1.4 -apple-system,BlinkMacSystemFont,Arial,sans-serif;'
          'letter-spacing:2px;margin:0 0 14px 0;">'
          'SIMULATION DRILL - NOT A SIGNAL'
          '<div style="font:400 12px/1.5 Arial,sans-serif;letter-spacing:0.5px;'
          'margin-top:6px;">scenario: %s &middot; injected metrics &middot; '
          'no email sent &middot; no state written</div></div>')

def run_drill(name, out_dir=None):
    sc = get_scenario(name)
    repo_state = os.path.join(HERE, "state.json")
    state_before = None
    if os.path.exists(repo_state):
        with open(repo_state, "rb") as f:
            state_before = f.read()
    d = tempfile.mkdtemp(prefix="sim_drill_%s_" % name)
    script = os.path.join(d, "short.py")
    shutil.copy(SRC, script)
    with open(os.path.join(d, "state.json"), "w") as f:
        json.dump(sc["seed"], f)
    # secrets scrub: even though send_email() only runs under __main__,
    # remove every credential the mailer could possibly pick up.
    for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "MAIL_TO"):
        os.environ.pop(k, None)
    os.environ["FMP_API_KEY"] = "test"
    os.environ["FRED_API_KEY"] = "test"
    install_stub(build_fixtures(sc["closes"], sc["spy_px"],
                                vols=sc.get("vols"), extra=sc.get("extra")))
    g = runpy.run_path(script)  # run_name != "__main__": no email, no CACHE.save()
    html = g["build_html"]()
    banner = BANNER % name
    m = re.search(r"<body[^>]*>", html)
    if m:
        html = html[:m.end()] + banner + html[m.end():]
    else:
        html = banner + html
    out_dir = out_dir or os.path.join(HERE, "reports")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(out_dir, "simulation_%s.html" % stamp)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    state_after = None
    if os.path.exists(repo_state):
        with open(repo_state, "rb") as f:
            state_after = f.read()
    if state_before != state_after:
        raise SystemExit("FATAL: repo state.json changed during a drill run")
    return g, out_path

def main():
    ap = argparse.ArgumentParser(description="INITIATE SHORT escalation drill "
                                 "(simulation only - never emails, never "
                                 "writes state.json)")
    ap.add_argument("--scenario", default="full_escalation",
                    choices=SCENARIO_NAMES, help="drill scenario to run")
    ap.add_argument("--out", default=None, help="output directory "
                    "(default: <repo>/reports)")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = ap.parse_args()
    if args.list:
        for n in SCENARIO_NAMES:
            print("%-18s %s" % (n, get_scenario(n)["expect"]))
        return 0
    print("=" * 70)
    print("SIMULATION DRILL: %s" % args.scenario)
    print("expect: %s" % get_scenario(args.scenario)["expect"])
    print("=" * 70)
    g, out_path = run_drill(args.scenario, args.out)
    print("-" * 70)
    print("initiate_short : %s" % g["initiate_short"])
    print("primary        : %s" % g["primary"])
    print("layer2         : %s" % g.get("layer2"))
    print("vol_ratio=%s  rr_value=%s  200dma_above=%s  10mema_above=%s" % (
        g.get("vol_ratio"), g.get("rr_value"),
        g.get("spx_above_200dma"), g.get("spx_above_10mema")))
    print("catalyst_auto=%s  vol_expansion=%s" % (
        g.get("catalyst_auto"), g.get("vol_expansion")))
    print("vix9d_ratio=%s  ts_velocity=%s  pre_alert=%s" % (
        g.get("vix9d_ratio"), g.get("ts_velocity"), g.get("pre_alert")))
    print("-" * 70)
    print("rendered: %s" % out_path)
    print("SIMULATION DRILL - NOT A SIGNAL (no email sent, no state written)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
