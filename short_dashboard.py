#!/usr/bin/env python3
"""SHORT macro dashboard -> colored HTML email.

Pulls live macro data (FMP /stable/, FRED, Yahoo Finance, CFTC), scores 13
indicator tiles, and emails a colour-coded dashboard to the recipients.

Hardened 2026-06-29:
  * every external call has a timeout + 3 retries with exponential backoff
  * every numeric metric is range-validated; anomalies are logged and dropped
  * on any API failure the last-known-good value (state.json) is used instead
    of crashing, so a single dead API never breaks the run
  * structured logging to stdout (visible in the GitHub Actions console)
  * US market-holiday detection (flags stale data, still sends)
  * a final summary line: "Run complete - X/13 signals retrieved, email sent: Y"

Keys/secrets are read from environment variables (GitHub Actions Secrets) and
fall back to a local .env for development. No third-party packages are required.
"""
import os
import sys
import ssl
import json
import time
import logging
import smtplib
import datetime
import calendar as _cal
import urllib.request
import urllib.error
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# Encoding: force UTF-8 so non-ASCII API responses never crash the run.
# ---------------------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("short")


def _utcnow():
    """Naive UTC timestamp (timezone-aware internally; avoids utcnow() deprecation)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
DEFAULT_RECIPIENTS = ["wolfgangduke@gmail.com", "richard.macrae.gordon@gmail.com"]
TOTAL_TILES = 13  # number of indicator tiles the engine computes

# ---------------------------------------------------------------------------
# Config / secrets
# ---------------------------------------------------------------------------
def load_env():
    env = {}
    try:
        with open(os.path.join(HERE, ".env"), encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass  # in GitHub Actions there is no .env; secrets come from os.environ
    return env


ENV = load_env()


def cfg(k):
    # env vars (Actions secrets) take precedence over .env
    return (os.environ.get(k) or ENV.get(k, "")).strip()


FMP = cfg("FMP_API_KEY")
FRED = cfg("FRED_API_KEY")


def recipients():
    """Always include the two named recipients; merge in anything from MAIL_TO."""
    out = list(DEFAULT_RECIPIENTS)
    extra = cfg("MAIL_TO")
    if extra:
        for part in extra.replace(";", ",").replace(" ", ",").split(","):
            part = part.strip()
            if "@" in part and part not in out:
                out.append(part)
    return out


RECIPIENTS = recipients()

# ---------------------------------------------------------------------------
# Last-known-value cache (persisted across runs via state.json, committed back
# by the workflow). This is what lets a dead API fall back instead of crash.
# ---------------------------------------------------------------------------
class Cache:
    def __init__(self, path):
        self.path = path
        self.data = {}
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
            log.info("loaded %d cached values from state.json", len(self.data))
        except FileNotFoundError:
            log.info("no state.json yet (first run); starting empty cache")
        except Exception as e:
            log.warning("could not read state.json (%s); starting empty", e)

    def get(self, key):
        rec = self.data.get(key)
        return rec.get("value") if isinstance(rec, dict) else None

    def get_age_days(self, key):
        rec = self.data.get(key)
        if not isinstance(rec, dict) or "ts" not in rec:
            return None
        try:
            then = datetime.datetime.fromisoformat(rec["ts"])
            return (_utcnow() - then).days
        except Exception:
            return None

    def set(self, key, value, ts):
        self.data[key] = {"value": value, "ts": ts}

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
            log.info("saved %d values to state.json", len(self.data))
        except Exception as e:
            log.warning("could not write state.json: %s", e)


CACHE = Cache(STATE_PATH)
RUN_TS = _utcnow().isoformat(timespec="seconds")

# Track how many tiles ended up with real (live or cached) data.
TILES_WITH_DATA = 0


def _redact(url):
    """Strip api keys out of a URL before logging it."""
    out = url
    for token in (FMP, FRED):
        if token:
            out = out.replace(token, "***")
    return out


# ---------------------------------------------------------------------------
# HTTP layer: timeout + retries + exponential backoff
# ---------------------------------------------------------------------------
UA_HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
# Client errors that will never succeed on retry (bad key, bad request, missing).
NO_RETRY_CODES = {400, 401, 403, 404}


def http_get_json(url, headers=None, timeout=15, retries=3, backoff=1.0):
    """GET a URL and parse JSON. Returns (data, error_string)."""
    last_err = None
    safe = _redact(url)
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            if not raw.strip():
                last_err = "empty response"
            else:
                return json.loads(raw), None
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            if e.code in NO_RETRY_CODES:
                log.warning("GET %s -> %s (will not retry)", safe, last_err)
                return None, last_err
        except json.JSONDecodeError as e:
            last_err = "malformed JSON: %s" % e
        except Exception as e:  # timeout, DNS, connection reset, etc.
            last_err = str(e)
        if attempt < retries:
            wait = backoff * (2 ** (attempt - 1))
            log.info("GET %s failed (%s); retry %d/%d in %.0fs",
                     safe, last_err, attempt, retries, wait)
            time.sleep(wait)
    log.warning("GET %s failed after %d attempts: %s", safe, retries, last_err)
    return None, last_err


# ---------------------------------------------------------------------------
# Validation + cache fallback for scalar metrics
# ---------------------------------------------------------------------------
def keep(name, value, lo=None, hi=None):
    """Validate a freshly-fetched scalar, cache it, or fall back to last-known.

    Returns (value, stale_flag). stale_flag is True when a cached value is used.
    """
    if value is not None:
        try:
            value = float(value)
        except (TypeError, ValueError):
            log.warning("metric %s: non-numeric value %r; discarding", name, value)
            value = None
    if value is not None and lo is not None and not (lo <= value <= hi):
        log.warning("metric %s: %.4g outside sane range [%s, %s]; discarding",
                    name, value, lo, hi)
        value = None
    if value is not None:
        CACHE.set(name, value, RUN_TS)
        log.info("metric %s = %.4g (live)", name, value)
        return value, False
    cached = CACHE.get(name)
    if cached is not None:
        age = CACHE.get_age_days(name)
        log.warning("metric %s: live unavailable; using last-known %.4g (%s days old)",
                    name, cached, "?" if age is None else age)
        return cached, True
    log.warning("metric %s: unavailable and no cached value", name)
    return None, False


# ---------------------------------------------------------------------------
# Data-source helpers
# ---------------------------------------------------------------------------
def fmp(path):
    if not FMP:
        return None, "no FMP key"
    sep = "&" if "?" in path else "?"
    url = "https://financialmodelingprep.com/stable/%s%sapikey=%s" % (path, sep, FMP)
    return http_get_json(url)


def fred_series(series, n=2):
    if not FRED:
        return None
    url = ("https://api.stlouisfed.org/fred/series/observations?series_id=%s"
           "&api_key=%s&file_type=json&sort_order=desc&limit=%d" % (series, FRED, n))
    d, _ = http_get_json(url)
    if d and isinstance(d, dict) and d.get("observations"):
        out = []
        for o in d["observations"]:
            try:
                out.append(float(o["value"]))
            except (ValueError, KeyError, TypeError):
                pass
        return out or None
    return None


def yahoo_closes(symbol, n=6):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?range=10d&interval=1d" % symbol)
    d, _ = http_get_json(url, headers=UA_HDR)
    try:
        q = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        vals = [c for c in q if c is not None][-n:]
        return vals or None
    except Exception:
        return None


def cot_emini():
    url = ("https://publicreporting.cftc.gov/resource/gpe5-46if.json"
           "?$where=upper(contract_market_name)%20like%20'%25E-MINI%20S%26P%20500%25'"
           "&$order=report_date_as_yyyy_mm_dd%20DESC&$limit=1")
    d, _ = http_get_json(url, headers=UA_HDR)
    return d[0] if isinstance(d, list) and d else None


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# US market-holiday calendar (NYSE) - so a holiday run is flagged, not silent
# ---------------------------------------------------------------------------
def _easter(year):
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return datetime.date(year, month, day + 1)


def _nth_weekday(year, month, weekday, n):
    days = [d for d in _cal.Calendar().itermonthdates(year, month)
            if d.month == month and d.weekday() == weekday]
    return days[n - 1]


def _last_weekday(year, month, weekday):
    days = [d for d in _cal.Calendar().itermonthdates(year, month)
            if d.month == month and d.weekday() == weekday]
    return days[-1]


def _observed(d):
    if d.weekday() == 5:   # Saturday -> Friday
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:   # Sunday -> Monday
        return d + datetime.timedelta(days=1)
    return d


def market_holidays(year):
    h = set()
    h.add(_observed(datetime.date(year, 1, 1)))            # New Year's Day
    h.add(_nth_weekday(year, 1, 0, 3))                     # MLK Day (3rd Mon Jan)
    h.add(_nth_weekday(year, 2, 0, 3))                     # Presidents' Day
    h.add(_easter(year) - datetime.timedelta(days=2))     # Good Friday
    h.add(_last_weekday(year, 5, 0))                       # Memorial Day
    h.add(_observed(datetime.date(year, 6, 19)))          # Juneteenth
    h.add(_observed(datetime.date(year, 7, 4)))           # Independence Day
    h.add(_nth_weekday(year, 9, 0, 1))                    # Labor Day
    h.add(_nth_weekday(year, 11, 3, 4))                  # Thanksgiving (4th Thu)
    h.add(_observed(datetime.date(year, 12, 25)))         # Christmas
    return h


def eastern_today():
    """Current US/Eastern calendar date, with a dependency-free DST estimate."""
    utc = _utcnow()
    y = utc.year
    dst_start = _nth_weekday(y, 3, 6, 2)   # 2nd Sunday in March
    dst_end = _nth_weekday(y, 11, 6, 1)    # 1st Sunday in November
    is_dst = dst_start <= utc.date() < dst_end
    offset = 4 if is_dst else 5            # EDT = UTC-4, EST = UTC-5
    return (utc - datetime.timedelta(hours=offset)).date()


ET_TODAY = eastern_today()
IS_HOLIDAY = ET_TODAY in market_holidays(ET_TODAY.year)
IS_WEEKEND = ET_TODAY.weekday() >= 5
if IS_HOLIDAY:
    log.warning("US market HOLIDAY today (%s ET) - data may be stale; sending with a flag", ET_TODAY)
elif IS_WEEKEND:
    log.warning("US market closed (weekend, %s ET) - data may be stale", ET_TODAY)
else:
    log.info("US trading day: %s ET", ET_TODAY)


# ===========================================================================
# GATHER LIVE DATA
# ===========================================================================
log.info("=== gathering live data (FMP key: %s, FRED key: %s) ===",
         "set" if FMP else "MISSING", "set" if FRED else "MISSING")

D = {}
for key, path in [
    ("spy", "quote?symbol=SPY"),
    ("vix", "quote?symbol=%5EVIX"),
    ("gold", "quote?symbol=GCUSD"),
    ("treasury", "treasury-rates"),
    ("rsi", "technical-indicators/rsi?symbol=SPY&periodLength=14&timeframe=1day"),
]:
    d, e = fmp(path)
    if d:
        D[key] = d[0] if isinstance(d, list) and d else d
    else:
        log.warning("FMP %s unavailable (%s)", key, e)

# sector breadth (may be gated on some plans)
sectors, e = fmp("sector-performance-snapshot?date=%s" % datetime.date.today().isoformat())
if not sectors:
    sectors, e = fmp("sector-performance-snapshot")

# ---- raw scalars with validation + cache fallback ----
spy = D.get("spy", {}) if isinstance(D.get("spy"), dict) else {}
spy_px, _ = keep("spy_px", num(spy.get("price")), 50, 2000)
spy_chg, _ = keep("spy_chg", num(spy.get("changePercentage")), -25, 25)
spx_proxy = spy_px * 10 if spy_px is not None else None

vix_px, _ = keep("vix_px", num(D.get("vix", {}).get("price")
                               if isinstance(D.get("vix"), dict) else None), 5, 150)
gold_px, _ = keep("gold_px", num(D.get("gold", {}).get("price")
                                 if isinstance(D.get("gold"), dict) else None), 200, 10000)

t = D.get("treasury", {}) if isinstance(D.get("treasury"), dict) else {}
y2, _ = keep("y2", num(t.get("year2")), -2, 25)
y10, _ = keep("y10", num(t.get("year10")), -2, 25)
spread_bps = round((y10 - y2) * 100) if (y2 is not None and y10 is not None) else None

rsi_val = None
r = D.get("rsi", {})
if isinstance(r, dict):
    rsi_val, _ = keep("rsi", num(r.get("rsi")), 0, 100)


def sector_chg(s):
    for f in ("changesPercentage", "averageChange", "changePercentage", "changePct"):
        if f in s:
            return num(s[f])
    return None


breadth = None
up = down = 0
if sectors and isinstance(sectors, list):
    for s in sectors:
        c = sector_chg(s)
        if c is None:
            continue
        if c >= 0:
            up += 1
        else:
            down += 1
    if up + down:
        breadth = round(up / (up + down) * 100)
# fallback: Yahoo Finance advance/decline if FMP sectors unavailable
if breadth is None:
    try:
        _adv = yahoo_closes("%5EADVN", 1)
        _dec = yahoo_closes("%5EDECN", 1)
        if _adv and _dec and _adv[0] and _dec[0]:
            up = int(_adv[0])
            down = int(_dec[0])
            if up + down:
                breadth = round(up / (up + down) * 100)
    except Exception as ex:
        log.warning("breadth Yahoo fallback failed: %s", ex)
breadth, _ = keep("breadth", breadth, 0, 100)
if breadth is not None:
    breadth = int(round(breadth))

# ---- net liquidity, all converted to $bn ----
# FRED units (verified): WALCL = millions, WTREGEN (TGA) = millions,
# RRPONTSYD (RRP) = billions. So WALCL and TGA are /1000; RRP is used as-is.
netliq = None
walcl = fred_series("WALCL")
tga = fred_series("WTREGEN")
rrp = fred_series("RRPONTSYD")
if walcl and tga and rrp and len(walcl) >= 2:
    try:
        cur = walcl[0] / 1000.0 - tga[0] / 1000.0 - rrp[0]
        prv = (walcl[1] / 1000.0 - tga[min(1, len(tga) - 1)] / 1000.0
               - rrp[min(1, len(rrp) - 1)])
        # sanity: US net liquidity is on the order of a few $trillion (in $bn here)
        if -2000 < cur < 12000 and -2000 < prv < 12000:
            netliq = (cur, prv)
            CACHE.set("netliq_cur", cur, RUN_TS)
            CACHE.set("netliq_prv", prv, RUN_TS)
            log.info("net liquidity cur=%.0f prv=%.0f ($bn)", cur, prv)
        else:
            log.warning("net liquidity out of range (cur=%.0f prv=%.0f); discarding", cur, prv)
    except Exception as ex:
        log.warning("net liquidity calc failed: %s", ex)
if netliq is None:
    cc = CACHE.get("netliq_cur")
    cp = CACHE.get("netliq_prv")
    if cc is not None and cp is not None:
        netliq = (cc, cp)
        log.warning("net liquidity: using last-known values")

netliq_dir = None
if netliq:
    netliq_dir = "declining" if netliq[0] < netliq[1] else "rising"

# ===========================================================================
# VERDICTS
# ===========================================================================
breadth_red = breadth is not None and breadth < 50
netliq_decl = netliq_dir == "declining"
if breadth_red and netliq_decl:
    primary = "WATCHING - both triggers RED (confirm 3-day streak)"
else:
    primary = "WATCHING - Day 1 of 3"
layer2 = "WAIT"

# ===========================================================================
# EXTRA FEEDS: credit, dollar, fiscal, calendar
# ===========================================================================
PAL = {"red": ("#fcebeb", "#e24b4a", "#791f1f"),
       "amber": ("#faeeda", "#ef9f27", "#633806"),
       "green": ("#eaf3de", "#639922", "#27500a"),
       "gray": ("#f1efe8", "#888780", "#444441")}


def fmt_money(v):
    return "n/a" if v is None else ("{:,.0f}".format(v))


hy = fred_series("BAMLH0A0HYM2", 2)
credit_sub, credit_col = "no data", "gray"
if hy and len(hy) >= 2:
    hy0, _ = keep("hy_oas", hy[0], 0, 30)
    if hy0 is not None:
        credit_sub = "HY OAS %.2f%% (%s)" % (hy0, "widening" if hy[0] > hy[1] else "tightening")
        credit_col = "red" if hy[0] > hy[1] else "green"
else:
    c = CACHE.get("hy_oas")
    if c is not None:
        credit_sub, credit_col = "HY OAS %.2f%% (last known)" % c, "amber"

dxy = fred_series("DTWEXBGS", 2)
usd_sub, usd_col = "no data", "gray"
if dxy and len(dxy) >= 2:
    dxy0, _ = keep("dxy", dxy[0], 80, 180)
    if dxy0 is not None:
        usd_sub = "Broad $ %.1f (%s)" % (dxy0, "rising" if dxy[0] > dxy[1] else "falling")
        usd_col = "red" if dxy[0] > dxy[1] else "green"
else:
    c = CACHE.get("dxy")
    if c is not None:
        usd_sub, usd_col = "Broad $ %.1f (last known)" % c, "amber"

mts = fred_series("MTSDS133FMS", 12)
fisc_sub, fisc_col = "no data", "gray"
if mts and len(mts) >= 12:
    deficit = -sum(mts[:12]) / 1e6  # millions -> $T, deficit positive
    dval, _ = keep("deficit_T", deficit, -1, 10)
    if dval is not None:
        fisc_col = "red" if dval > 2.0 else ("amber" if dval > 1.5 else "green")
        fisc_sub = "12M deficit $%.2fT" % dval
else:
    c = CACHE.get("deficit_T")
    if c is not None:
        fisc_sub, fisc_col = "12M deficit $%.2fT (last known)" % c, "amber"


def third_friday(y, m):
    fr = [d for d in _cal.Calendar().itermonthdates(y, m) if d.month == m and d.weekday() == 4]
    return fr[2]


_t = datetime.date.today()
opex_days = abs((third_friday(_t.year, _t.month) - _t).days)
fomc_days = None
ec, _ = fmp("economic-calendar?from=%s&to=%s" % (_t, _t + datetime.timedelta(days=10)))
if ec and isinstance(ec, list):
    for ev in ec:
        nm = (ev.get("event") or "").lower()
        ct = (ev.get("country") or "")
        if ct in ("US", "USA") and ("fed interest rate" in nm or "fomc" in nm or "federal funds" in nm):
            try:
                dd = (datetime.date.fromisoformat((ev.get("date") or "")[:10]) - _t).days
                if dd >= 0 and (fomc_days is None or dd < fomc_days):
                    fomc_days = dd
            except Exception:
                pass
cal_flags = []
if fomc_days is not None and fomc_days <= 5:
    cal_flags.append("FOMC in %dd" % fomc_days)
if opex_days <= 3:
    cal_flags.append("OpEx in %dd" % opex_days)
cal_sub = "; ".join(cal_flags) if cal_flags else "clear"
cal_col = "red" if cal_flags else "green"

# ---- COT positioning (CFTC public data) ----
cot_sub, cot_col = "no data", "gray"
_cot = cot_emini()
if _cot:
    try:
        amL = float(_cot["asset_mgr_positions_long"])
        amS = float(_cot["asset_mgr_positions_short"])
        levL = float(_cot["lev_money_positions_long"])
        levS = float(_cot["lev_money_positions_short"])
        dL = float(_cot["change_in_lev_money_long"])
        dS = float(_cot["change_in_lev_money_short"])
        am_net = amL - amS
        lev_net = levL - levS
        lev_chg = dL - dS
        cot_col = "red" if lev_chg <= -20000 else ("green" if lev_chg >= 20000 else "amber")
        cot_sub = "Lev net %+.0fk (%+.0fk WoW); AM net %+.0fk" % (lev_net / 1000, lev_chg / 1000, am_net / 1000)
        CACHE.set("cot_sub", cot_sub, RUN_TS)
    except Exception as ex:
        log.warning("COT parse error: %s", ex)
        cot_sub, cot_col = "parse error", "gray"
if cot_sub in ("no data", "parse error"):
    c = CACHE.get("cot_sub")
    if c:
        cot_sub, cot_col = c + " (last known)", "amber"

# ---- VVIX divergence (Yahoo Finance) ----
vvix_sub, vvix_col = "no data", "gray"
_vv = yahoo_closes("%5EVVIX")
_vx = yahoo_closes("%5EVIX")
if _vv and _vx and len(_vv) >= 2 and len(_vx) >= 2:
    vvix = _vv[-1]
    vvc = (vvix / _vv[-2] - 1) * 100
    vxc = (_vx[-1] / _vx[-2] - 1) * 100
    if 50 <= vvix <= 250:
        vvix_col = "red" if (vvc >= 3 and vxc <= 1) else ("green" if vvc <= -2 else "amber")
        vvix_sub = "VVIX %.0f (%+.1f%%) vs VIX %+.1f%%" % (vvix, vvc, vxc)
        CACHE.set("vvix_sub", vvix_sub, RUN_TS)
    else:
        log.warning("VVIX %.0f out of range; discarding", vvix)
if vvix_sub == "no data":
    c = CACHE.get("vvix_sub")
    if c:
        vvix_sub, vvix_col = c + " (last known)", "amber"

# ===========================================================================
# BUILD 13 TILES
# ===========================================================================
p = []
p.append(("1. Equities (S&P via SPY)",
          ("SPY %.2f (%+.2f%%)" % (spy_px, spy_chg))
          if (spy_px is not None and spy_chg is not None)
          else (("SPY %.2f" % spy_px) if spy_px is not None else "unavailable"),
          "amber" if spy_px is not None else "gray"))
p.append(("2. Volatility (VIX)",
          ("%.1f" % vix_px) if vix_px is not None else "unavailable",
          "amber" if vix_px is not None else "gray"))
p.append(("3. Rates / yield curve",
          ("2s10s %+d bps" % spread_bps) if spread_bps is not None else "unavailable",
          ("green" if (spread_bps is not None and spread_bps >= 0)
           else ("red" if spread_bps is not None else "gray"))))
p.append(("4. Credit spreads", credit_sub, credit_col))
p.append(("5. Commodities (Cu/Au)",
          ("Gold $%s; copper=Premium" % fmt_money(gold_px)) if gold_px is not None else "unavailable",
          "gray"))
p.append(("6. Dollar / FX", usd_sub, usd_col))
p.append(("7. Market breadth",
          ("%d%% advancing" % breadth) if breadth is not None else "unavailable",
          ("red" if breadth_red else ("green" if breadth is not None else "gray"))))
p.append(("8. Net liquidity",
          netliq_dir if netliq_dir else "unavailable",
          ("red" if netliq_dir == "declining" else ("green" if netliq_dir == "rising" else "gray"))))
p.append(("9. Positioning (COT)", cot_sub, cot_col))
p.append(("10. VVIX divergence", vvix_sub, vvix_col))
p.append(("11. Sector rotation",
          ("defensive tilt" if breadth_red else "broad") if breadth is not None else "unavailable",
          ("red" if breadth_red else ("green" if breadth is not None else "gray"))))
p.append(("12. Calendar gate", cal_sub, cal_col))
p.append(("13. Fiscal impulse", fisc_sub, fisc_col))

# count tiles that actually have data (not "unavailable"/"no data")
DEAD = {"unavailable", "no data", "parse error"}
TILES_WITH_DATA = sum(1 for _, sub, _c in p if str(sub).strip().lower() not in DEAD)
log.info("tiles populated: %d/%d", TILES_WITH_DATA, TOTAL_TILES)

# ===========================================================================
# BUILD HTML
# ===========================================================================
now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
today = datetime.date.today().strftime("%B %d, %Y")

stale_banner = ""
if IS_HOLIDAY:
    stale_banner = ("<div style=\"background:#faeeda;border-left:4px solid #ef9f27;"
                    "padding:8px 12px;margin-bottom:12px;font-size:12px;color:#633806;\">"
                    "US market holiday today - figures may reflect the prior session.</div>")
elif IS_WEEKEND:
    stale_banner = ("<div style=\"background:#f1efe8;border-left:4px solid #888780;"
                    "padding:8px 12px;margin-bottom:12px;font-size:12px;color:#444441;\">"
                    "US market closed (weekend) - figures reflect the prior session.</div>")


def card(label, value, sub, subcolor):
    return ('<td width="25%" style="background:#f1efe8;border-radius:8px;padding:12px;">'
            '<div style="font-size:12px;color:#5f5e5a;">' + label + '</div>'
            '<div style="font-size:22px;font-weight:bold;">' + value + '</div>'
            '<div style="font-size:11px;color:' + subcolor + ';">' + sub + '</div></td>')


def tile(title, sub, color):
    bg, bd, tc = PAL[color]
    return ('<td width="50%" valign="top" style="background:' + bg + ';border-left:4px solid ' + bd +
            ';padding:8px 10px;"><span style="font-size:13px;font-weight:bold;color:' + tc + ';">' +
            title + '</span><br><span style="font-size:12px;color:#5f5e5a;">' + sub + '</span></td>')


spx_card = fmt_money(spx_proxy) if spx_proxy else "n/a"
spx_sub = ("%+.2f%%" % spy_chg) if spy_chg is not None else "via SPY x10"
spx_subcol = "#3b6d11" if (spy_chg or 0) >= 0 else "#a32d2d"
vix_card = ("%.1f" % vix_px) if vix_px is not None else "n/a"
sp_card = ("%+d bps" % spread_bps) if spread_bps is not None else "n/a"
sp_sub = "steepening" if (spread_bps is not None and spread_bps >= 0) else ("inverted" if spread_bps is not None else "n/a")
br_card = ("%d%%" % breadth) if breadth is not None else "n/a"
br_sub = ("%d of %d green" % (up, up + down)) if breadth is not None else "no data"

rows = ""
for i in range(0, len(p), 2):
    left = tile(*p[i])
    right = tile(*p[i + 1]) if i + 1 < len(p) else '<td width="50%"></td>'
    rows += "<tr>" + left + right + "</tr>"

final_signal = ("No short tonight. " +
    ("Breadth is sub-50% with a defensive tilt - the one bearish tell - but it is a single session and the liquidity/positioning confirms are not aligned. "
     if breadth_red else
     "Breadth is holding above 50% and the curve is not inverted, so there is no edge to press here. ") +
    "Stay flat; watch breadth and net liquidity.")

html = (
'<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;color:#2c2c2a;">'
+ stale_banner +
'<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:14px;"><tr>'
'<td style="font-size:18px;font-weight:bold;">MacroSage - SHORT signal</td>'
'<td align="right" style="font-size:12px;color:#5f5e5a;">' + now + ' (live FMP/stable)</td></tr></table>'
'<table width="100%" cellpadding="0" cellspacing="0" style="border-spacing:6px 0;margin-bottom:14px;"><tr>'
+ card("S&P 500 (SPYx10)", spx_card, spx_sub, spx_subcol)
+ card("VIX", vix_card, "live", "#5f5e5a")
+ card("2s10s", sp_card, sp_sub, "#5f5e5a")
+ card("Breadth", br_card, br_sub, ("#a32d2d" if breadth_red else "#5f5e5a"))
+ '</tr></table>'
'<table width="100%" cellpadding="0" cellspacing="0" style="border-spacing:6px;margin-bottom:14px;">' + rows + '</table>'
'<table width="100%" cellpadding="0" cellspacing="0" style="border-spacing:6px;margin-bottom:14px;"><tr>'
'<td width="50%" valign="top" style="background:#faeeda;border-radius:8px;padding:12px 14px;">'
'<div style="font-size:11px;color:#854f0b;text-transform:uppercase;">Primary verdict</div>'
'<div style="font-size:16px;font-weight:bold;color:#633806;">' + primary + '</div></td>'
'<td width="50%" valign="top" style="background:#f1efe8;border-radius:8px;padding:12px 14px;">'
'<div style="font-size:11px;color:#5f5e5a;text-transform:uppercase;">Layer 2 verdict</div>'
'<div style="font-size:16px;font-weight:bold;color:#2c2c2a;">' + layer2 + '</div></td></tr></table>'
'<div style="border:1px solid #d3d1c7;border-radius:8px;padding:11px 14px;font-size:13px;line-height:1.6;">'
'<b>Final signal:</b> ' + final_signal + '</div>'
'<div style="font-size:11px;color:#5f5e5a;margin-top:10px;">Legend: '
'<span style="color:#e24b4a;">&#9632;</span> bearish &nbsp; '
'<span style="color:#ef9f27;">&#9632;</span> neutral/capped &nbsp; '
'<span style="color:#639922;">&#9632;</span> not bearish &nbsp; '
'<span style="color:#888780;">&#9632;</span> unavailable</div>'
'<div style="font-size:10px;color:#9a9890;margin-top:8px;">'
+ ("%d of %d indicators retrieved this run. " % (TILES_WITH_DATA, TOTAL_TILES)) +
'Research/educational only - not investment advice.</div></div>')

plain = ("MacroSage SHORT signal - %s\nPRIMARY VERDICT: %s\nLAYER 2 VERDICT: %s\n\n%s\n\n"
         "%d of %d indicators retrieved. Research/educational only - not investment advice.\n"
         % (now, primary, layer2, final_signal, TILES_WITH_DATA, TOTAL_TILES))

# ---- save a timestamped report (best-effort) ----
try:
    rdir = os.path.join(HERE, "reports")
    os.makedirs(rdir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    with open(os.path.join(rdir, "short_%s.html" % stamp), "w", encoding="utf-8") as fh:
        fh.write(html)
except Exception as ex:
    log.warning("report save warning: %s", ex)


# ===========================================================================
# EMAIL
# ===========================================================================
def send_email():
    user = cfg("GMAIL_USER")
    pw = cfg("GMAIL_APP_PASSWORD").replace(" ", "")
    if not user or not pw:
        log.error("EMAIL SKIPPED: missing GMAIL_USER / GMAIL_APP_PASSWORD secret")
        return False
    msg = MIMEMultipart("alternative")
    subject = "SHORT Signal - %s Post-Market" % today
    if IS_HOLIDAY:
        subject += " [US holiday]"
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as s:
            s.login(user, pw)
            refused = s.sendmail(user, RECIPIENTS, msg.as_string())
        if refused:
            log.error("EMAIL PARTIAL: some recipients refused: %s", refused)
            return False
        log.info("EMAIL SENT to %s", ", ".join(RECIPIENTS))
        return True
    except smtplib.SMTPAuthenticationError as e:
        log.error("EMAIL AUTH FAILED - check GMAIL_APP_PASSWORD is a Gmail *App Password* "
                  "(not the account password) and 2FA is on. Detail: %s", e)
        return False
    except Exception as e:
        log.error("EMAIL ERROR: %s", e)
        return False


if __name__ == "__main__":
    log.info("=== SHORT dashboard %s ===", now)
    log.info("SPX(SPYx10): %s | VIX: %s | 2s10s: %s | Breadth: %s",
             spx_card, vix_card, sp_card, br_card)
    log.info("PRIMARY: %s | LAYER2: %s", primary, layer2)
    log.info("recipients: %s", ", ".join(RECIPIENTS))

    email_ok = False
    try:
        email_ok = send_email()
    except Exception as ex:
        log.error("EMAIL ERROR (unhandled): %s", ex)

    # persist last-known values for the next run's fallback
    CACHE.save()

    # final one-line summary
    log.info("Run complete - %d/%d signals retrieved, email sent: %s",
             TILES_WITH_DATA, TOTAL_TILES, "YES" if email_ok else "NO")

    # Exit 0 even on email failure: a missed email should not mark the whole
    # scheduled job red (the log already states what failed). Only a hard crash
    # above this point (which we guard against) produces a non-zero exit.
    sys.exit(0)
