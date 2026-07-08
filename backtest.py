#!/usr/bin/env python3
"""MacroSage backtest - trend-regime core of the crash signal.

Replays the RECONSTRUCTABLE core of the INITIATE gate over ~10 years of price
history and measures whether it actually precedes forward drawdowns.

SCOPE / HONESTY: this backtests the price-derived core that gates every INITIATE
SHORT - SPX (via SPY) below its 200-day MA AND below its 10-month EMA. It does
NOT replay the full 18-tile sentiment stack (breadth %, net liquidity, COT,
NAAIM, AAII, NYMO, VIX term structure), which lack free multi-year history.
Read the numbers as the edge of the trend-regime CORE - the hard precondition
the live engine layers sentiment gates on top of. Free data only (Yahoo
primary, Stooq fallback); no third-party packages.
"""
import urllib.request
import json
import datetime
import sys

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _get(url, headers=None, timeout=30):
    try:
        req = urllib.request.Request(url, headers=headers or UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        print("fetch failed: %s (%s)" % (url, e), file=sys.stderr)
        return None


def yahoo_daily(symbol, rng="10y"):
    """[(YYYY-MM-DD, close), ...] oldest-first, or None."""
    raw = _get("https://query1.finance.yahoo.com/v8/finance/chart/%s"
               "?range=%s&interval=1d" % (symbol, rng))
    if not raw:
        return None
    try:
        res = json.loads(raw)["chart"]["result"][0]
        ts = res["timestamp"]
        cl = res["indicators"]["quote"][0]["close"]
        out = []
        for i in range(len(ts)):
            if cl[i] is not None:
                d = datetime.datetime.fromtimestamp(
                    ts[i], datetime.timezone.utc).strftime("%Y-%m-%d")
                out.append((d, float(cl[i])))
        return out or None
    except Exception as e:
        print("yahoo parse failed %s (%s)" % (symbol, e), file=sys.stderr)
        return None


def stooq_daily(symbol):
    """Fallback CSV, e.g. 'spy.us' -> [(date, close), ...] or None."""
    raw = _get("https://stooq.com/q/d/l/?s=%s&i=d" % symbol)
    if not raw or not raw.strip().lower().startswith("date"):
        return None
    out = []
    for line in raw.strip().splitlines()[1:]:
        p = line.split(",")
        if len(p) >= 5:
            try:
                out.append((p[0], float(p[4])))
            except ValueError:
                pass
    return out or None


def load_spy():
    d = yahoo_daily("SPY", "10y")
    if d and len(d) > 500:
        print("data: Yahoo SPY, %d sessions %s..%s" % (len(d), d[0][0], d[-1][0]))
        return d
    d = stooq_daily("spy.us")
    if d and len(d) > 500:
        print("data: Stooq SPY, %d sessions %s..%s" % (len(d), d[0][0], d[-1][0]))
        return d
    print("FATAL: could not load SPY history", file=sys.stderr)
    sys.exit(1)


def month_end_ema(dates, closes, period=10):
    """10-month EMA of month-end closes, mapped to the EMA effective for each
    calendar month (uses the last COMPLETED month, matching the live engine).
    Returns dict 'YYYY-MM' -> ema-effective-that-month."""
    m_last = {}
    for d, c in zip(dates, closes):
        m_last[d[:7]] = c  # dates ascending -> keeps last close of each month
    months = sorted(m_last)
    mvals = [m_last[m] for m in months]
    if len(mvals) < period:
        return {}
    k = 2.0 / (period + 1)
    ema = sum(mvals[:period]) / float(period)  # SMA seed through month[period-1]
    ema_by_month = {}
    for j in range(period - 1, len(mvals)):
        if j >= period:
            ema = mvals[j] * k + ema * (1 - k)
        if j + 1 < len(months):          # effective for the FOLLOWING month
            ema_by_month[months[j + 1]] = ema
    return ema_by_month


def pct(xs, cond):
    return (100.0 * sum(1 for x in xs if cond(x)) / len(xs)) if xs else float("nan")


def mean(xs):
    return (sum(xs) / len(xs)) if xs else float("nan")


HZ = (5, 10, 20)


def build_rows(data):
    dates = [d for d, _ in data]
    closes = [c for _, c in data]
    n = len(closes)
    ema_m = month_end_ema(dates, closes, 10)
    rows = []  # (date, below200, below10m, {h: fwd_ret%})
    for i in range(n):
        if i < 200:
            continue
        sma200 = sum(closes[i - 199:i + 1]) / 200.0
        below200 = closes[i] < sma200
        e = ema_m.get(dates[i][:7])
        below10m = (e is not None and closes[i] < e)
        fwd = {}
        for h in HZ:
            if i + h < n:
                fwd[h] = (closes[i + h] / closes[i] - 1) * 100
        if fwd:
            rows.append((dates[i], below200, below10m, fwd))
    return rows


def summarize(rows, name, sel):
    print("\n=== %s ===" % name)
    for h in HZ:
        base = [r[3][h] for r in rows if h in r[3]]
        on = [r[3][h] for r in rows if sel(r) and h in r[3]]
        if not on:
            print("  %2dd: (no qualifying days)" % h)
            continue
        print("  %2dd fwd | ON: n=%4d mean=%+.2f%% neg=%4.1f%%  |  base: "
              "n=%5d mean=%+.2f%% neg=%4.1f%%  |  edge=%+.2f%%"
              % (h, len(on), mean(on), pct(on, lambda x: x < 0),
                 len(base), mean(base), pct(base, lambda x: x < 0),
                 mean(on) - mean(base)))


def episodes(rows, sel):
    eps, prev, starts = 0, False, []
    for r in rows:
        cur = sel(r)
        if cur and not prev:
            eps += 1
            starts.append(r)
        prev = cur
    return eps, starts


def run():
    rows = build_rows(load_spy())
    neg20 = pct([r[3][20] for r in rows if 20 in r[3]], lambda x: x < 0)
    print("\nWindow: %s .. %s (%d eligible days)" % (rows[0][0], rows[-1][0], len(rows)))
    print("Base rate: SPY 20d-forward negative %.1f%% of all days" % neg20)
    summarize(rows, "Variant A - SPX below 200DMA", lambda r: r[1])
    summarize(rows, "Variant B - below 200DMA AND below 10M-EMA (live INITIATE regime)",
              lambda r: r[1] and r[2])
    epsB, starts = episodes(rows, lambda r: r[1] and r[2])
    e20 = [s[3][20] for s in starts if 20 in s[3]]
    hit = [x for x in e20 if x < 0]
    print("\nEpisodes (Variant B): %d distinct regime entries." % epsB)
    if e20:
        print("  On regime ENTRY day, forward 20d SPY: mean=%+.2f%%, fell %d/%d (%.0f%%)"
              % (mean(e20), len(hit), len(e20), 100.0 * len(hit) / len(e20)))
    print("\nNOTE: trend-regime CORE only (200DMA + 10M-EMA). The live engine adds "
          "sentiment gates (dual-red breadth+liquidity, volume, R:R, VIX term\n"
          "structure) that further restrict firing; this is the reconstructable "
          "backbone, not the full 18-tile signal.")


if __name__ == "__main__":
    run()
