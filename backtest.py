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


def realized_vol_flags(closes):
    """vol_exp[i] = True when 5d realized vol >= 1.30x the 20d realized vol
    (RMS of daily returns), matching the live engine's vol-expansion input."""
    rets = [None] * len(closes)
    for i in range(1, len(closes)):
        if closes[i - 1]:
            rets[i] = closes[i] / closes[i - 1] - 1.0

    def rms(xs):
        xs = [x for x in xs if x is not None]
        return (sum(x * x for x in xs) / len(xs)) ** 0.5 if xs else None

    out = [False] * len(closes)
    for i in range(len(closes)):
        if i >= 20:
            r5, r20 = rms(rets[i - 4:i + 1]), rms(rets[i - 19:i + 1])
            out[i] = bool(r5 is not None and r20 and r5 / r20 >= 1.30)
    return out


def build_rows(data, vix_map=None, v3_map=None):
    dates = [d for d, _ in data]
    closes = [c for _, c in data]
    n = len(closes)
    ema_m = month_end_ema(dates, closes, 10)
    volx = realized_vol_flags(closes)
    rows = []
    for i in range(n):
        if i < 200:
            continue
        sma200 = sum(closes[i - 199:i + 1]) / 200.0
        below200 = closes[i] < sma200
        e = ema_m.get(dates[i][:7])
        below10m = (e is not None and closes[i] < e)
        vx = (vix_map or {}).get(dates[i])
        v3 = (v3_map or {}).get(dates[i])
        backw = bool(vx and v3 and vx / v3 >= 1.0)
        fwd = {}
        for h in HZ:
            if i + h < n:
                fwd[h] = (closes[i + h] / closes[i] - 1) * 100
        if fwd:
            rows.append({"date": dates[i], "below200": below200,
                         "below10m": below10m, "backw": backw,
                         "volx": volx[i], "have_vix": bool(vx and v3), "fwd": fwd})
    return rows


def summarize(rows, name, sel):
    for h in HZ:
        base = [r["fwd"][h] for r in rows if h in r["fwd"]]
        on = [r["fwd"][h] for r in rows if sel(r) and h in r["fwd"]]
        if not on:
            print("  %-46s %2dd: (no qualifying days)" % (name if h == HZ[0] else "", h))
            continue
        print("  %-46s %2dd | ON n=%4d mean=%+.2f%% neg=%4.1f%% | base mean=%+.2f%% neg=%4.1f%% | edge=%+.2f%%"
              % (name if h == HZ[0] else "", h, len(on), mean(on), pct(on, lambda x: x < 0),
                 mean(base), pct(base, lambda x: x < 0), mean(on) - mean(base)))


def episodes(rows, sel):
    eps, prev, starts = 0, False, []
    for r in rows:
        cur = sel(r)
        if cur and not prev:
            eps += 1
            starts.append(r)
        prev = cur
    return eps, starts


def entry_line(rows, name, sel):
    eps, starts = episodes(rows, sel)
    e20 = [s["fwd"][20] for s in starts if 20 in s["fwd"]]
    hit = [x for x in e20 if x < 0]
    if e20:
        print("  %-34s %2d entries | entry+20d mean=%+.2f%% | fell %d/%d (%.0f%%)"
              % (name, eps, mean(e20), len(hit), len(e20), 100.0 * len(hit) / len(e20)))
    else:
        print("  %-34s %2d entries | (no closed 20d outcomes)" % (name, eps))


def run():
    data = load_spy()
    vix = yahoo_daily("%5EVIX", "10y") or []
    v3 = yahoo_daily("%5EVIX3M", "10y") or []
    vix_map = {d: c for d, c in vix}
    v3_map = {d: c for d, c in v3}
    rows = build_rows(data, vix_map, v3_map)
    neg20 = pct([r["fwd"][20] for r in rows if 20 in r["fwd"]], lambda x: x < 0)
    print("\nWindow: %s .. %s (%d eligible days)" % (rows[0]["date"], rows[-1]["date"], len(rows)))
    print("Base rate: SPY 20d-forward negative %.1f%% of all days" % neg20)
    _vr = [r for r in rows if r["have_vix"]]
    if _vr:
        print("VIX/VIX3M coverage: %s .. %s (%d days)" % (_vr[0]["date"], _vr[-1]["date"], len(_vr)))
    reg = lambda r: r["below200"] and r["below10m"]
    print("\nForward-return edge vs base (positive edge = ABOVE base = the OPPOSITE of a crash signal):")
    summarize(rows, "A - below 200DMA", lambda r: r["below200"])
    summarize(rows, "B - regime (200DMA AND 10M-EMA)", reg)
    summarize(rows, "C - regime AND VIX backwardation", lambda r: reg(r) and r["backw"])
    summarize(rows, "D - regime AND vol-expansion", lambda r: reg(r) and r["volx"])
    summarize(rows, "E - regime AND (backw OR vol-exp)", lambda r: reg(r) and (r["backw"] or r["volx"]))
    summarize(rows, "F - VIX backwardation alone", lambda r: r["backw"])
    summarize(rows, "G - vol-expansion alone", lambda r: r["volx"])
    print("\nEpisode entry outcomes ('hit' = SPY fell over the next 20d; 50%% = coin flip):")
    entry_line(rows, "B regime", reg)
    entry_line(rows, "C regime + backwardation", lambda r: reg(r) and r["backw"])
    entry_line(rows, "D regime + vol-expansion", lambda r: reg(r) and r["volx"])
    entry_line(rows, "E regime + (backw OR volx)", lambda r: reg(r) and (r["backw"] or r["volx"]))
    print("\nNOTE: still a SUBSET of the live signal - adds the two reconstructable "
          "confirmations (VIX/VIX3M backwardation, realized-vol expansion) on top of\n"
          "the trend regime, but omits breadth %, net liquidity, COT, NAAIM, AAII "
          "(no free multi-year history). VIX3M limits the confirmation window; the\n"
          "stricter combos have small samples - read them as directional, not precise.")


if __name__ == "__main__":
    run()
