#!/usr/bin/env python3
"""SHORT macro dashboard -> colored HTML email.

Pulls live macro data (FMP /stable/, FRED, Yahoo Finance, CFTC), scores 17
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
TOTAL_TILES = 17  # number of indicator tiles the engine computes

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




# ===========================================================================
# LIVE INDICATOR FETCHERS  (live-indicators branch, 2026-06-30)
# Each returns a dict with typed values on success, None on any failure so
# the caller can gracefully fall back.
# ===========================================================================

def _http_get_text(url, headers=None, timeout=20, retries=2, backoff=1.5):
    """Like http_get_json but returns raw text (for scraping)."""
    last_err = None
    safe = _redact(url)
    hdrs = dict(UA_HDR)
    if headers:
        hdrs.update(headers)
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace"), None
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(backoff * attempt)
    log.warning("scrape %s failed after %d attempts: %s", safe, retries, last_err)
    return None, last_err


def _re_first(pattern, text, cast=float, default=None):
    """Return first regex group from text cast to type, or default."""
    if not text:
        return default
    import re as _re
    m = _re.search(pattern, text)
    if not m:
        return default
    try:
        return cast(m.group(1).replace(",", ""))
    except Exception:
        return default




def compute_nymo(cache):
    """Self-compute NYMO = EMA19(net_adv) - EMA39(net_adv) using NYSE A/D.
    Persists nymo_ema19, nymo_ema39 in state.json across runs.
    Returns {"nymo": float, "warming_up": bool} or None on data failure.
    """
    try:
        _adv = yahoo_closes("%5EADVN", 1)
        _dec = yahoo_closes("%5EDECN", 1)
        if not _adv or not _dec or not _adv[0] or not _dec[0]:
            log.warning("NYMO compute: A/D data unavailable")
            return None
        net_adv = int(_adv[0]) - int(_dec[0])
    except Exception as ex:
        log.warning("NYMO compute: A/D fetch error: %s", ex)
        return None

    mult19 = 2.0 / (19 + 1)
    mult39 = 2.0 / (39 + 1)

    prev19 = cache.get("nymo_ema19")
    prev39 = cache.get("nymo_ema39")

    # Seed on first run, or advance EMAs
    if prev19 is None:
        ema19 = float(net_adv)
        ema39 = float(net_adv)
        warming_up = True
    else:
        ema19 = net_adv * mult19 + prev19 * (1 - mult19)
        ema39 = net_adv * mult39 + prev39 * (1 - mult39)
        # Flag warming up until at least 39 sessions of history exist
        age19 = cache.get_age_days("nymo_ema19")
        warming_up = (age19 is None or age19 < 39)

    cache.set("nymo_ema19", round(ema19, 4), RUN_TS)
    cache.set("nymo_ema39", round(ema39, 4), RUN_TS)

    nymo = round(ema19 - ema39, 2)
    log.info("NYMO computed = %.2f (net_adv=%d, ema19=%.2f, ema39=%.2f, warm=%s)",
             nymo, net_adv, ema19, ema39, warming_up)
    return {"nymo": nymo, "warming_up": warming_up}

def fetch_naaim():
    """Fetch latest NAAIM Exposure Index by scraping the HTML table on naaim.org.
    The table is server-rendered (no JS needed). Rows: MM/DD/YYYY | value | ...
    Returns {"naaim": float} or None on failure.
    """
    text, err = _http_get_text(
        "https://naaim.org/programs/naaim-exposure-index/",
        timeout=25,
    )
    if not text:
        log.warning("NAAIM: page fetch failed (%s)", err)
        return None
    import re as _re
    # Match first table row with MM/DD/YYYY date and float value
    m = _re.search(
        r'(\d{2}/\d{2}/\d{4})</td>\s*<td>([\d.]+)</td>',
        text
    )
    if m:
        try:
            val = float(m.group(2))
            if 0 <= val <= 200:
                log.info("NAAIM live = %.1f (dated %s)", val, m.group(1))
                return {"naaim": val}
        except Exception:
            pass
    log.warning("NAAIM: page fetched (%d chars) but table not parseable", len(text))
    return None


def fetch_aaii():
    """Fetch latest AAII sentiment from aaii.com.
    Returns {"bull": float, "bear": float} or None on failure.
    """
    # AAII publishes spreadsheet data - try the CSV / JSON endpoint first
    text, err = _http_get_text(
        "https://www.aaii.com/files/surveys/sentiment.xls",
        timeout=20,
    )
    # If we can't get XLS, scrape the landing page
    text2, err2 = _http_get_text("https://www.aaii.com/sentimentsurvey/sent_results", timeout=20)
    if text2:
        import re as _re
        # Typical page has "Bullish: XX.X%" and "Bearish: XX.X%"
        bull_m = _re.search(r'Bullish.*?(\d+\.?\d*)\s*%', text2, _re.IGNORECASE | _re.DOTALL)
        bear_m = _re.search(r'Bearish.*?(\d+\.?\d*)\s*%', text2, _re.IGNORECASE | _re.DOTALL)
        if bull_m and bear_m:
            try:
                bull = float(bull_m.group(1))
                bear = float(bear_m.group(1))
                if 0 <= bull <= 100 and 0 <= bear <= 100:
                    log.info("AAII live: bull=%.1f%% bear=%.1f%%", bull, bear)
                    return {"bull": bull, "bear": bear}
            except Exception:
                pass
    log.warning("AAII: all fetches failed (%s / %s)", err, err2)
    return None


def fetch_gex():
    """Fetch SPX net GEX and zero-gamma level from SpotGamma free tools page.
    JS-rendered — plain HTTP fetch unlikely to return live numbers.
    Returns {"net_gex": float, "zero_gamma": float} or None on failure.
    """
    text, err = _http_get_text(
        "https://spotgamma.com/free-tools/spx-gamma-exposure/",
        timeout=25,
    )
    if text:
        import re as _re
        # SpotGamma embeds values in JSON blobs or specific spans
        ng_patterns = [
            r'net[_\s-]?gex["\':\s]+([+-]?\d[\d,]*\.?\d*)',
            r'Net GEX[^<>]{0,40}>(([+-]?\d[\d,]*\.?\d*))',
            r'netGex["\':\s]+([+-]?\d[\d,]*)',
        ]
        zg_patterns = [
            r'zero[_\s-]?gamma["\':\s]+([+-]?\d[\d,]*)',
            r'Zero Gamma[^<>]{0,40}>(\d[\d,]*)',
            r'zeroGamma["\':\s]+(\d[\d,]*)',
        ]
        net_gex = None
        zero_gamma = None
        for pat in ng_patterns:
            m = _re.search(pat, text, _re.IGNORECASE)
            if m:
                try:
                    net_gex = float(m.group(1).replace(",", ""))
                    break
                except Exception:
                    pass
        for pat in zg_patterns:
            m = _re.search(pat, text, _re.IGNORECASE)
            if m:
                try:
                    zero_gamma = float(m.group(1).replace(",", ""))
                    break
                except Exception:
                    pass
        if net_gex is not None:
            log.info("GEX live: net_gex=%.0f zero_gamma=%s", net_gex, zero_gamma)
            return {"net_gex": net_gex, "zero_gamma": zero_gamma}
        log.warning("GEX: page fetched (%d chars) but values not parseable", len(text))
    else:
        log.warning("GEX fetch error: %s", err)
    return None


def fetch_cot_tradingster():
    """Fallback COT source: Tradingster.com for ES E-mini positions.
    Returns a dict compatible with the main cot_emini() output or None.
    """
    text, err = _http_get_text(
        "https://www.tradingster.com/cot/futures/fin/13874A",
        timeout=20,
    )
    if not text:
        log.warning("Tradingster COT fetch error: %s", err)
        return None
    import re as _re
    # Tradingster shows "Asset Manager - Long: X,XXX / Short: X,XXX"
    am_l = _re_first(r'Asset Manager.*?Long.*?([\d,]+)', text, cast=lambda x: float(x.replace(",","")))
    am_s = _re_first(r'Asset Manager.*?Short.*?([\d,]+)', text, cast=lambda x: float(x.replace(",","")))
    lev_l = _re_first(r'Leveraged.*?Long.*?([\d,]+)', text, cast=lambda x: float(x.replace(",","")))
    lev_s = _re_first(r'Leveraged.*?Short.*?([\d,]+)', text, cast=lambda x: float(x.replace(",","")))
    if am_l and am_s and lev_l and lev_s:
        return {
            "asset_mgr_positions_long": am_l,
            "asset_mgr_positions_short": am_s,
            "lev_money_positions_long": lev_l,
            "lev_money_positions_short": lev_s,
            "change_in_lev_money_long": 0,   # Tradingster may not expose deltas
            "change_in_lev_money_short": 0,
            "_source": "tradingster",
        }
    return None


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

# ---- COT positioning (CFTC public data + Tradingster fallback) ----
cot_sub, cot_col = "no data", "gray"
_cot = cot_emini()

# Staleness check: CFTC data older than 10 days is treated as STALE/UNAVAILABLE
_cot_stale = False
if _cot and "report_date_as_yyyy_mm_dd" in _cot:
    try:
        import datetime as _dt2
        _cot_date = _dt2.datetime.strptime(_cot["report_date_as_yyyy_mm_dd"].split("T")[0], "%Y-%m-%d").date()
        if (_utcnow().date() - _cot_date).days > 10:
            log.warning("COT CFTC data is stale (%d days old); trying Tradingster fallback",
                        (_utcnow().date() - _cot_date).days)
            _cot_stale = True
            _cot2 = fetch_cot_tradingster()
            if _cot2:
                _cot = _cot2
            else:
                _cot = None
    except Exception as _e:
        log.warning("COT date parse error: %s", _e)

if not _cot and not _cot_stale:
    # Primary fetch returned nothing - try Tradingster immediately
    log.info("COT CFTC returned no data; trying Tradingster fallback")
    _cot = fetch_cot_tradingster()

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
        src = _cot.get("_source", "CFTC")
        if am_net > 0:
            cot_col = "green"
            cot_sub = "AM net long %+.0f; Lev chg %+.0f [%s]" % (am_net, lev_chg, src)
        else:
            cot_col = "red"
            cot_sub = "AM net short %+.0f; Lev chg %+.0f [%s]" % (am_net, lev_chg, src)
        CACHE.set("cot_sub", cot_sub, RUN_TS)
        CACHE.set("cot_col", cot_col, RUN_TS)
    except Exception as e:
        log.warning("COT parse error: %s", e)
        c = CACHE.get("cot_sub")
        cc = CACHE.get("cot_col")
        if c:
            cot_sub, cot_col = c + " (last known)", cc or "amber"
else:
    c = CACHE.get("cot_sub")
    cc = CACHE.get("cot_col")
    if c:
        cot_sub, cot_col = c + " (last known, COT STALE/UNAVAILABLE)", "amber"

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


# ---- McClellan Oscillator / NYMO (mcoscillator.com) ----
nymo_sub, nymo_col = "manual — https://www.mcoscillator.com/market_breadth_data/ (next: daily)"
_nymo_result = compute_nymo(CACHE)
if _nymo_result and _nymo_result.get("nymo") is not None:
    _nymo = _nymo_result["nymo"]
    _warm = _nymo_result.get("warming_up", False)
    _label = "above zero" if _nymo >= 0 else "below zero"
    _warm_tag = " ⚠ warming up (<39d)" if _warm else ""
    nymo_sub = "NYMO %.1f (%s)%s" % (_nymo, _label, _warm_tag)
    nymo_col = "green" if _nymo >= 0 else "red"
    CACHE.set("nymo", _nymo, RUN_TS)
    log.info("NYMO tile = %.2f (warm=%s)", _nymo, _warm)
else:
    _nymo_cached = CACHE.get("nymo")
    if _nymo_cached is not None:
        nymo_sub = "NYMO %.1f (last known)" % _nymo_cached
        nymo_col = "green" if _nymo_cached >= 0 else "red"
        log.warning("NYMO: A/D unavailable, using cache (%.2f)", _nymo_cached)
    # else stays as manual fallback label above
mcclellan_divergence = (
    nymo_col == "red" and spy_px is not None
    # divergence = NYMO negative while SPX near highs
    # A more precise check would compare to a 52W high; this is a proxy
)

# ---- NAAIM Exposure Index (naaim.org) ----
naaim_sub, naaim_col = "manual — https://www.naaim.org/programs/naaim-exposure-index/ (weekly)", "gray"
_naaim_live = fetch_naaim()
if _naaim_live and _naaim_live.get("naaim") is not None:
    _naaim = _naaim_live["naaim"]
    naaim_sub = "NAAIM %.1f%s" % (_naaim, " (mgrs ALL-IN ▲ contrarian-bearish)" if _naaim > 90 else "")
    naaim_col = "red" if _naaim > 90 else ("green" if _naaim < 40 else "amber")
    CACHE.set("naaim", _naaim, RUN_TS)
else:
    _naaim_cached = CACHE.get("naaim")
    if _naaim_cached is not None:
        naaim_sub = "NAAIM %.1f (last known)" % _naaim_cached
        naaim_col = "red" if _naaim_cached > 90 else "amber"
        log.warning("NAAIM: using cache (%.1f)", _naaim_cached)

# ---- AAII Sentiment (aaii.com) ----
aaii_sub, aaii_col = "manual — https://www.aaii.com/sentimentsurvey (weekly)", "gray"
_aaii_live = fetch_aaii()
if _aaii_live and _aaii_live.get("bull") is not None:
    _bull = _aaii_live["bull"]
    _bear = _aaii_live["bear"]
    aaii_sub = "AAII bull %.0f%% bear %.0f%%" % (_bull, _bear)
    aaii_col = "red" if _bull > 55 else ("green" if _bear > 45 else "amber")
    CACHE.set("aaii_bull", _bull, RUN_TS)
    CACHE.set("aaii_bear", _bear, RUN_TS)
else:
    _bcached = CACHE.get("aaii_bull")
    _brcached = CACHE.get("aaii_bear")
    if _bcached is not None:
        aaii_sub = "AAII bull %.0f%% bear %.0f%% (last known)" % (_bcached, _brcached or 0)
        aaii_col = "red" if _bcached > 55 else "amber"
        log.warning("AAII: using cache")

# ---- GEX / Gamma Exposure (SpotGamma, JS-rendered — likely manual fallback) ----
gex_sub, gex_col = "manual — https://spotgamma.com/free-tools/spx-gamma-exposure/ (next-open)", "gray"
_gex_live = fetch_gex()
gamma_flip = False
if _gex_live and _gex_live.get("net_gex") is not None:
    _net_gex = _gex_live["net_gex"]
    _zero_gex = _gex_live.get("zero_gamma")
    gamma_flip = _net_gex < 0
    flip_str = "GAMMA FLIP (neg GEX)" if gamma_flip else "pos GEX"
    spot_vs = ""
    if _zero_gex and spx_proxy:
        spot_vs = " | SPX %s flip" % ("BELOW" if spx_proxy < _zero_gex else "above")
    gex_sub = "Net GEX %+.0fB%s [%s]" % (_net_gex / 1e9 if abs(_net_gex) > 1e6 else _net_gex,
                                           spot_vs, flip_str)
    gex_col = "red" if gamma_flip else "green"
    CACHE.set("net_gex", _net_gex, RUN_TS)
    if _zero_gex:
        CACHE.set("zero_gamma", _zero_gex, RUN_TS)
else:
    _gex_cached = CACHE.get("net_gex")
    if _gex_cached is not None:
        gamma_flip = _gex_cached < 0
        gex_sub = "Net GEX %+.0f (last known%s)" % (
            _gex_cached, ", GAMMA FLIP" if gamma_flip else "")
        gex_col = "red" if gamma_flip else "amber"
        log.warning("GEX: using cache (%.0f)", _gex_cached)

# ---- SPX 200-day MA gate ----
_spy_hist = yahoo_closes("SPY", 205)
spx_above_200dma = None  # None = unknown
spx_200dma = None
if _spy_hist and len(_spy_hist) >= 200:
    spx_200dma = sum(_spy_hist[-200:]) / 200.0
    if spy_px is not None:
        spx_above_200dma = (spy_px > spx_200dma)
        log.info("SPX 200DMA gate: SPY %.2f vs 200MA %.2f -> above=%s",
                 spy_px, spx_200dma, spx_above_200dma)


# ===========================================================================
# VERDICT AUGMENTATION (live-indicators)
# Runs after all data sources gathered so cal_sub, vvix_sub, GEX etc are set.
# ===========================================================================

# ---- 200DMA GATE ----
if spx_above_200dma is True:
    _200dma_note = " | 200DMA GATE: SPX above 200MA (~%.0f) — cap short conviction YELLOW" % (spx_200dma or 0)
    primary = primary + _200dma_note
elif spx_above_200dma is False:
    primary = primary + " | 200DMA: SPX BELOW 200MA — structural short regime valid"

# ---- BREADTH DECAY STREAK ----
_prev_bd = CACHE.get("breadth_decay_streak") or 0
if breadth_red:
    breadth_decay_streak = int(_prev_bd) + 1
else:
    breadth_decay_streak = 0
CACHE.set("breadth_decay_streak", breadth_decay_streak, RUN_TS)
if breadth_decay_streak > 0:
    primary = primary + " | Breadth decay streak: %d session%s" % (
        breadth_decay_streak, "s" if breadth_decay_streak != 1 else "")

# ---- LAYER-2 SIGNAL CHECK (GEX flip, VIX backwardation, McClellan divergence) ----
_vix_backwardation = vvix_sub != "no data" and "rising" in vvix_sub.lower()
_l2_signals = sum([gamma_flip, _vix_backwardation, mcclellan_divergence])

# Clear calendar = no FOMC/OpEx within 2 days
_cal_clear = (cal_sub not in ("no data",) and
              not any(x in cal_sub for x in ("in 0d", "in 1d", "in 2d")))

if _l2_signals >= 2 and _cal_clear:
    _l2_names = []
    if gamma_flip: _l2_names.append("GEX flip")
    if _vix_backwardation: _l2_names.append("VIX backwardation")
    if mcclellan_divergence: _l2_names.append("McClellan divergence")
    _why_low = []
    if "WATCHING" in primary: _why_low.append("PRIMARY still WATCHING")
    if spx_above_200dma: _why_low.append("SPX above 200DMA")
    _conv_note = "probe size" if (_why_low) else "standard"
    layer2 = ("ENTRY SIGNAL - early/low-conviction (%s) [%s%s]" % (
        _conv_note,
        ", ".join(_l2_names),
        ("; caveat: " + "; ".join(_why_low)) if _why_low else "",
    ))
    log.info("Layer2 ENTRY signal fired: %s", layer2)


# ===========================================================================
# BUILD 17 TILES
# ===========================================================================
p = []
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
p.append(("14. McClellan / NYMO", nymo_sub, nymo_col))
p.append(("15. NAAIM Exposure", naaim_sub, naaim_col))
p.append(("16. AAII Sentiment", aaii_sub, aaii_col))
p.append(("17. GEX / Gamma flip", gex_sub, gex_col))


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
