#!/usr/bin/env python3
"""SHORT macro dashboard -> colored HTML email.

Pulls live macro data (FMP /stable/, FRED, Yahoo Finance, CFTC), scores 18
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
TOTAL_TILES = 18  # number of indicator tiles the engine computes

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




def fetch_ad_series(n=60):
    """Fetch NYSE advancing/declining issues as a net-advances series.
    Primary : WSJ Markets Diary JSON (wsj.com, free, no auth, 2 sessions/call).
    Secondary: Finviz homepage HTML (today only; PRE-MARKET returns 0/0 - rejected).
               NOTE: FMP v3 historical-price-full ^ADVN/^DECN was removed (dead:
               returned HTTP 403 - legacy /api/v3/ index endpoint no longer entitled).
    # DEAD (removed): FMP v3 ^ADVN/^DECN -> 403; FMP stable ^ADVN -> 404; Yahoo v7/v8 -> 401/404.
    Returns  : list of net_adv (int) oldest-first, >=40 sessions on success, or None.
    Logs WARNING and returns None if all sources fail — caller uses last-known cache.
    """
    import datetime as _dt

    t_end = _dt.date.today()
    t_start = t_end - _dt.timedelta(days=n * 2)  # 2x buffer for weekends/holidays

    # --- Primary: WSJ Markets Diary JSON (free, no auth, NYSE daily A/D, 2 sessions) ---
    # Endpoint: https://www.wsj.com/market-data/stocks/marketsdiary
    #   ?id={"application":"WSJ","marketsDiaryType":"diaries"}&type=mdc_marketsdiary
    # Returns instrumentSets[0]=NYSE with advances/declines for latestClose+previousClose.
    try:
        _wsj_url = (
            "https://www.wsj.com/market-data/stocks/marketsdiary"
            "?id=%7B%22application%22%3A%22WSJ%22"
            "%2C%22marketsDiaryType%22%3A%22diaries%22%7D"
            "&type=mdc_marketsdiary"
        )
        _wsj_d, _wsj_e = http_get_json(_wsj_url, headers=UA_HDR)
        if _wsj_d:
            _sets = _wsj_d.get("data", {}).get("instrumentSets", [])
            _nyse = next((s for s in _sets
                if (s.get("headerFields") or [{}])[0].get("label","").upper() == "NYSE"),
                None)
            if _nyse:
                def _wsj_val(instr, row_id, field):
                    row = next((r for r in instr if r.get("id") == row_id), None)
                    if not row: return None
                    raw = str(row.get(field, "")).replace(",", "").strip()
                    return int(float(raw)) if raw else None
                _instr = _nyse.get("instruments", [])
                _adv_cur = _wsj_val(_instr, "advances", "latestClose")
                _dec_cur = _wsj_val(_instr, "declines", "latestClose")
                _adv_prv = _wsj_val(_instr, "advances", "previousClose")
                _dec_prv = _wsj_val(_instr, "declines", "previousClose")
                _sess = []
                if _adv_prv is not None and _dec_prv is not None:
                    _sess.append(_adv_prv - _dec_prv)  # older session first
                if _adv_cur is not None and _dec_cur is not None:
                    _sess.append(_adv_cur - _dec_cur)
                if _sess:
                    log.info("NYMO A/D: WSJ diaries NYSE, %d sessions (adv=%s dec=%s)",
                            len(_sess), _adv_cur, _dec_cur)
                    return _sess[-n:]
                else:
                    log.warning("NYMO A/D WSJ: advances/declines missing in NYSE set")
            else:
                log.warning("NYMO A/D WSJ: NYSE set not found in response")
        else:
            log.warning("NYMO A/D WSJ: fetch failed (%s)", _wsj_e)
    except Exception as ex:
        log.warning("NYMO A/D WSJ error: %s", ex)

    # --- Secondary: Finviz homepage HTML (TODAY ONLY; PRE-MARKET returns 0/0) ---
    # WARNING: This path returns a single-session series. NYMO will be warm=True.
    # The pre-market 0/0 guard below prevents seeding EMAs with garbage.
    try:
        fv_html, _ = _http_get_text("https://finviz.com/")
        if fv_html:
            import re as _re
            adv_m = _re.search(r'Advancing[^(]*\((\d+)\)', fv_html, _re.DOTALL)
            dec_m = _re.search(r'Declining[^(]*\((\d+)\)', fv_html, _re.DOTALL)
            if adv_m and dec_m:
                adv_n, dec_n = int(adv_m.group(1)), int(dec_m.group(1))
                if adv_n > 0 or dec_n > 0:  # reject pre-market 0/0
                    net = adv_n - dec_n
                    log.info("NYMO A/D: Finviz today-only fallback (all-US proxy), "
                             "net=%d — WSJ+Finviz both failed; NYMO warm=True", net)
                    return [net]
                log.warning("NYMO A/D Finviz: pre-market 0/0 rejected")
            else:
                log.warning("NYMO A/D Finviz: pattern not found in page")
        else:
            log.warning("NYMO A/D Finviz: fetch failed")
    except Exception as ex:
        log.warning("NYMO A/D Finviz error: %s", ex)

    log.warning("NYMO A/D: ALL sources failed (WSJ, Finviz) — NYMO will use cache")
    return None


def compute_nymo(cache):
    """Self-compute NYMO = EMA19(net_adv) - EMA39(net_adv).
    Fetches A/D series via fetch_ad_series().
    With historical data (>=2 sessions) backfills EMAs immediately.
    Persists nymo_ema19, nymo_ema39 in state.json across runs.
    Returns {"nymo": float, "warming_up": bool} or None on data failure.
    """
    series = fetch_ad_series(60)
    if not series:
        log.warning("NYMO compute: A/D data unavailable")
        return None

    mult19 = 2.0 / (19 + 1)
    mult39 = 2.0 / (39 + 1)

    prev19 = cache.get("nymo_ema19")
    prev39 = cache.get("nymo_ema39")

    if prev19 is None and len(series) > 1:
        # Backfill EMAs from historical series (seed from first session)
        ema19 = ema39 = float(series[0])
        for net_adv in series[1:]:
            ema19 = net_adv * mult19 + ema19 * (1 - mult19)
            ema39 = net_adv * mult39 + ema39 * (1 - mult39)
        warming_up = len(series) < 39
        if not warming_up:
            cache.set("nymo_warmed", 1.0, RUN_TS)  # persist: once warm stays warm
    elif prev19 is None:
        # First run, today-only data: seed and flag warming up
        ema19 = ema39 = float(series[-1])
        warming_up = True
    else:
        net_adv = series[-1]
        ema19 = net_adv * mult19 + prev19 * (1 - mult19)
        ema39 = net_adv * mult39 + prev39 * (1 - mult39)
        # Once fully warmed by historical backfill, stay warmed forever.
        # nymo_warmed cached as 1.0 (True) / absent (False) in state.json.
        warming_up = not bool(cache.get("nymo_warmed"))

    cache.set("nymo_ema19", round(ema19, 4), RUN_TS)
    cache.set("nymo_ema39", round(ema39, 4), RUN_TS)

    nymo = round(ema19 - ema39, 2)
    log.info("NYMO computed = %.2f (ema19=%.2f, ema39=%.2f, warm=%s, sessions=%d)",
             nymo, ema19, ema39, warming_up, len(series))
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


def _yahoo_closes_range(symbol, rng="6mo"):
    """Daily closes (oldest-first) over a longer window than yahoo_closes().
    yahoo_closes() hard-codes range=10d (fine for 1-2 day deltas) but the
    breadth proxy needs >=60 sessions for a 50-day SMA, so this uses a wider
    range. Returns list[float] oldest-first or None on failure.
    """
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?range=%s&interval=1d" % (symbol, rng))
    d, _ = http_get_json(url, headers=UA_HDR)
    try:
        q = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        vals = [float(c) for c in q if c is not None]
        return vals or None
    except Exception:
        return None


def fetch_rsp_spy_ratio(n=120):
    """Equal-weight vs cap-weight breadth proxy: daily RSP/SPY close ratio.

    A rising RSP/SPY ratio means the average stock is outperforming the
    mega-cap-dominated index -> breadth BROADENING. A falling ratio means
    gains are concentrating in the largest names -> breadth NARROWING.

    Free / no-paid-tier sources, in order:
      Primary  : Yahoo v8 chart API (keyless, returns ~60-120 sessions on
                 range=6mo; verified to serve both RSP and SPY).
      Secondary: Stooq CSV (keyless; may be rate-limited from some IPs).
                 NOTE: FMP /api/v3/historical-price-full removed (legacy; HTTP 403).

    Returns list[float] of daily ratios oldest-first (>=60 on success) or
    None if no source yields aligned RSP+SPY history (caller uses cache).
    """
    # --- Primary: Yahoo (keyless) ---
    try:
        rsp = _yahoo_closes_range("RSP", "6mo")
        spy = _yahoo_closes_range("SPY", "6mo")
        if rsp and spy:
            m = min(len(rsp), len(spy))
            if m >= 60:
                # align on the most recent m sessions (both feeds same calendar)
                ratio = [rsp[-m + i] / spy[-m + i] for i in range(m)
                         if spy[-m + i]]
                if len(ratio) >= 60:
                    log.info("breadth proxy: Yahoo RSP/SPY, %d sessions", len(ratio))
                    return ratio[-n:]
            log.warning("breadth proxy Yahoo: too few aligned sessions (rsp=%d spy=%d)",
                        len(rsp), len(spy))
        else:
            log.warning("breadth proxy Yahoo: rsp=%s spy=%s",
                        "ok" if rsp else "None", "ok" if spy else "None")
    except Exception as ex:
        log.warning("breadth proxy Yahoo error: %s", ex)

    # --- Secondary: Stooq CSV (keyless) ---
    try:
        def _stooq_closes(symbol):
            url = "https://stooq.com/q/d/l/?s=%s&i=d" % symbol
            text, err = _http_get_text(url, timeout=20)
            if not text or not text.strip().lower().startswith("date"):
                log.warning("breadth proxy Stooq %s: bad/empty response (%s)", symbol, err)
                return None
            lines = text.strip().splitlines()
            closes = []
            for row in lines[1:]:  # Stooq daily CSV is oldest-first
                parts = row.split(",")
                if len(parts) >= 5:
                    try:
                        closes.append(float(parts[4]))
                    except (ValueError, IndexError):
                        pass
            return closes or None

        rsp = _stooq_closes("rsp.us")
        spy = _stooq_closes("spy.us")
        if rsp and spy:
            m = min(len(rsp), len(spy))
            if m >= 60:
                ratio = [rsp[-m + i] / spy[-m + i] for i in range(m)
                         if spy[-m + i]]
                if len(ratio) >= 60:
                    log.info("breadth proxy: Stooq RSP/SPY, %d sessions", len(ratio))
                    return ratio[-n:]
            log.warning("breadth proxy Stooq: too few aligned sessions")
    except Exception as ex:
        log.warning("breadth proxy Stooq error: %s", ex)

    log.warning("breadth proxy: ALL sources failed (Yahoo, FMP v3, Stooq) - using cache")
    return None


def _yahoo_closes_dated(symbol, rng="6mo"):
    """Daily closes as an ordered {date: close} map (oldest-first insertion).

    Same wider Yahoo fetcher used for RSP/SPY and the 200DMA gate
    (range=rng, ~60-126 sessions), but keyed by session date so two
    series can be aligned on a common trading day rather than blindly by
    -1 index (Yahoo occasionally drops a single session for one symbol).
    Returns dict[str,float] or None on failure.
    """
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?range=%s&interval=1d" % (symbol, rng))
    d, _ = http_get_json(url, headers=UA_HDR)
    try:
        res = d["chart"]["result"][0]
        q = res["indicators"]["quote"][0]["close"]
        ts = res["timestamp"]
        out = {}
        for i in range(len(q)):
            if q[i] is not None:
                day = datetime.datetime.fromtimestamp(ts[i], datetime.timezone.utc).strftime("%Y-%m-%d")
                out[day] = float(q[i])
        return out or None
    except Exception:
        return None


def compute_vix_term_structure(cache):
    """VIX vs VIX3M term-structure regime (replaces the paywalled GEX tile).

    ratio = VIX / VIX3M, measured on the most recent COMMON trading day.
      ratio <  1.00 -> CONTANGO      (calm, vol-suppressing)
      ratio >= 1.00 -> BACKWARDATION (stress, vol-amplifying)

    Front-month ^VIX here is the SAME series feeding tile #2 (Volatility);
    on the Starter FMP tier index quotes are gated, so both legs use the
    keyless wider Yahoo fetcher for consistency.

    Persists a backwardation_streak in state.json (integer, same shape as
    breadth_proxy_streak / breadth_decay_streak). Returns dict or None:
      {"vix": float, "vix3m": float, "ratio": float, "regime": str,
       "streak": int, "date": str, "stale": bool}
    """
    vix_map = _yahoo_closes_dated("%5EVIX", "6mo")
    v3_map = _yahoo_closes_dated("%5EVIX3M", "6mo")
    if vix_map and v3_map:
        common = sorted(d for d in vix_map if d in v3_map)
        if common:
            day = common[-1]
            vix = vix_map[day]
            v3 = v3_map[day]
            if v3 and 5 <= vix <= 150 and 5 <= v3 <= 150:
                ratio = vix / v3
                regime = "BACKWARDATION" if ratio >= 1.0 else "CONTANGO"
                prev = int(cache.get("backwardation_streak") or 0)
                streak = prev + 1 if regime == "BACKWARDATION" else 0
                cache.set("vix_ts_vix", round(vix, 2), RUN_TS)
                cache.set("vix_ts_vix3m", round(v3, 2), RUN_TS)
                cache.set("vix_ts_ratio", round(ratio, 4), RUN_TS)
                cache.set("vix_ts_regime", regime, RUN_TS)
                cache.set("backwardation_streak", streak, RUN_TS)
                log.info("VIX term structure: VIX=%.2f VIX3M=%.2f ratio=%.4f -> %s"
                         " (backwardation streak=%d, %s)",
                         vix, v3, ratio, regime, streak, day)
                return {"vix": vix, "vix3m": v3, "ratio": ratio,
                        "regime": regime, "streak": streak, "date": day,
                        "stale": False}
            log.warning("VIX term structure: values out of range VIX=%s VIX3M=%s",
                        vix, v3)
        else:
            log.warning("VIX term structure: no common date between ^VIX and ^VIX3M")
    else:
        log.warning("VIX term structure: Yahoo fetch failed (vix=%s vix3m=%s)",
                    "ok" if vix_map else "None", "ok" if v3_map else "None")
    # Fallback to last-known regime without advancing the streak.
    cached = cache.get("vix_ts_ratio")
    if cached is not None:
        log.warning("VIX term structure: live unavailable; using last-known")
        return {"vix": cache.get("vix_ts_vix"), "vix3m": cache.get("vix_ts_vix3m"),
                "ratio": cached, "regime": cache.get("vix_ts_regime") or "CONTANGO",
                "streak": int(cache.get("backwardation_streak") or 0),
                "date": None, "stale": True}
    log.warning("VIX term structure: no data and no cache")
    return None


def compute_breadth_proxy(cache):
    """Compute RSP/SPY breadth-proxy direction + streak from the ratio series.

    Directional / relative only (NOT a precise %): we report whether the ratio
    is rising vs its 50-day SMA and its short slope. Persists the running
    same-direction streak in state.json alongside breadth_decay_streak.

    Returns dict or None on data failure:
      {"ratio": float, "sma50": float, "slope": float,
       "direction": "BROADENING"|"NARROWING", "streak": int, "stale": bool}
    """
    series = fetch_rsp_spy_ratio(120)
    stale = False
    if not series or len(series) < 50:
        cached = cache.get("breadth_proxy_ratio")
        if cached is None:
            log.warning("breadth proxy: no data and no cache")
            return None
        # Fall back to last-known direction/streak without advancing the streak.
        log.warning("breadth proxy: live unavailable; using last-known")
        return {
            "ratio": cached,
            "sma50": cache.get("breadth_proxy_sma50"),
            "slope": cache.get("breadth_proxy_slope") or 0.0,
            "direction": cache.get("breadth_proxy_dir") or "NARROWING",
            "streak": int(cache.get("breadth_proxy_streak") or 0),
            "stale": True,
        }

    ratio = series[-1]
    sma50 = sum(series[-50:]) / 50.0
    # slope over the last N sessions (default 5): simple end-vs-start delta
    N = 5
    window = series[-(N + 1):] if len(series) > N else series
    slope = (window[-1] - window[0]) / max(len(window) - 1, 1)
    # Direction: rising AND above its 50-day MA -> BROADENING; else NARROWING.
    rising = slope > 0
    above_ma = ratio >= sma50
    direction = "BROADENING" if (rising and above_ma) else "NARROWING"

    prev_dir = cache.get("breadth_proxy_dir")
    prev_streak = int(cache.get("breadth_proxy_streak") or 0)
    if direction == prev_dir:
        streak = prev_streak + 1
    else:
        streak = 1

    cache.set("breadth_proxy_ratio", round(ratio, 6), RUN_TS)
    cache.set("breadth_proxy_sma50", round(sma50, 6), RUN_TS)
    cache.set("breadth_proxy_slope", round(slope, 8), RUN_TS)
    cache.set("breadth_proxy_dir", direction, RUN_TS)
    cache.set("breadth_proxy_streak", streak, RUN_TS)
    log.info("breadth proxy (RSP/SPY): ratio=%.5f sma50=%.5f slope=%+.6f -> %s, streak=%d",
             ratio, sma50, slope, direction, streak)
    return {"ratio": ratio, "sma50": sma50, "slope": slope,
            "direction": direction, "streak": streak, "stale": False}


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
nymo_sub, nymo_col = "manual — https://www.mcoscillator.com/market_breadth_data/ (next: daily)", "gray"
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

# ---- VIX Term Structure (VIX / VIX3M) regime ----
# Replaces tile #17 (was GEX / gamma flip — permanently manual because
# SpotGamma paywalls + JS-renders it). Front-month ^VIX is the same series
# used by tile #2; both legs come from the keyless wider Yahoo fetcher
# (FMP Starter tier gates index quotes). See compute_vix_term_structure().
_vts = compute_vix_term_structure(CACHE)
vix_ts_sub, vix_ts_col = "unavailable", "gray"
# _vix_backwardation feeds the Layer-2 2-of-3 ENTRY SIGNAL check below.
_vix_backwardation = False
if _vts is not None:
    _ratio = _vts["ratio"]
    _regime = _vts["regime"]
    _vts_stale = _vts.get("stale", False)
    _vix_backwardation = (_regime == "BACKWARDATION") and not _vts_stale
    _depth = "backwardation, ratio %.2f" % _ratio if _regime == "BACKWARDATION" \
        else "contango, ratio %.2f" % _ratio
    _streak_txt = ""
    if _vts.get("streak"):
        _streak_txt = " | backwardation streak: %d session%s" % (
            _vts["streak"], "s" if _vts["streak"] != 1 else "")
    vix_ts_sub = "VIX %.2f / VIX3M %.2f = %.3f — %s%s%s" % (
        _vts["vix"], _vts["vix3m"], _ratio, _depth, _streak_txt,
        " (last known)" if _vts_stale else "")
    # BACKWARDATION = stress/vol-amplifying -> bearish/red; CONTANGO = calm -> green.
    if _vts_stale:
        vix_ts_col = "amber"
    else:
        vix_ts_col = "red" if _regime == "BACKWARDATION" else "green"
else:
    _cached_ratio = CACHE.get("vix_ts_ratio")
    if _cached_ratio is not None:
        vix_ts_sub = "VIX/VIX3M ratio %.3f (last known)" % _cached_ratio
        vix_ts_col = "amber"
        log.warning("VIX term structure: using cache (%.3f)", _cached_ratio)

# GEX-flip Layer-2 input: the SpotGamma source stays unavailable (paywalled,
# JS-rendered). We do NOT silently drop it from the 2-of-3 count — it is kept
# as an explicit, currently-unavailable manual input (always False here) so the
# ENTRY-SIGNAL math is intentional and can be re-enabled if a live GEX feed is
# wired back in (an explicit, currently-unavailable manual input).
gamma_flip = False  # GEX flip: manual/unavailable input (SpotGamma paywalled)

# ---- SPX 200-day MA gate ----
# FIXED 2026-06-30 (fix-200dma): previously called yahoo_closes("SPY", 205),
# but yahoo_closes() is hard-capped at range=10d so it never returned 200
# sessions and this gate was permanently dormant. Now uses the wider Yahoo
# fetcher (range=1y ~= 251 sessions) so the 200-day SMA can actually be built.
_spy_hist = _yahoo_closes_range("SPY", "1y")
spx_above_200dma = None  # None = unknown
spx_200dma = None
if _spy_hist and len(_spy_hist) >= 200:
    spx_200dma = sum(_spy_hist[-200:]) / 200.0
    if spy_px is not None:
        spx_above_200dma = (spy_px > spx_200dma)
        log.info("SPX 200DMA gate: SPY %.2f vs 200MA %.2f -> above=%s (%d sessions)",
                 spy_px, spx_200dma, spx_above_200dma, len(_spy_hist))
    else:
        log.warning("SPX 200DMA gate: 200MA=%.2f computed but SPY price unavailable",
                    spx_200dma)
else:
    log.warning("SPX 200DMA gate: insufficient SPY history (%s sessions); gate inactive",
                len(_spy_hist) if _spy_hist else 0)

# ---- 50DMA (companion to the 200DMA gate; reuses the same SPY history) ----
spx_above_50dma = None  # None = unknown
spx_50dma = None
if _spy_hist and len(_spy_hist) >= 50:
    spx_50dma = sum(_spy_hist[-50:]) / 50.0
    if spy_px is not None:
        spx_above_50dma = (spy_px > spx_50dma)
        log.info("SPX 50DMA: SPY %.2f vs 50MA %.2f -> above=%s",
                 spy_px, spx_50dma, spx_above_50dma)
else:
    log.warning("SPX 50DMA: insufficient SPY history (%d sessions)",
                len(_spy_hist) if _spy_hist else 0)


# ---- BREADTH PROXY (RSP/SPY equal-weight vs cap-weight) ----
# Free, no-paid-tier breadth-direction proxy. Directional/relative only.
_bp = compute_breadth_proxy(CACHE)
# Recent-high context for the SPX-vs-breadth divergence test. Reuses the wide
# SPY history (range=1y) now fetched by the repaired 200DMA gate above; the
# 52-week (~252-session) high-water window is computed below.
_spx_near_high = None
try:
    # Reuse the wide SPY history fetched for the 200DMA gate (range=1y ~= 251
    # sessions); fall back to a fresh 1y pull if the gate fetch returned nothing.
    _spy_wide = _spy_hist if _spy_hist else _yahoo_closes_range("SPY", "1y")
    if _spy_wide and len(_spy_wide) >= 20 and spy_px is not None:
        # 52-week high-water window (~252 sessions); widened from 60 on fix-200dma.
        _recent_high = max(_spy_wide[-252:]) if len(_spy_wide) >= 252 else max(_spy_wide)
        # "at/near recent highs" = within 2% of the 52-week high
        _spx_near_high = spy_px >= _recent_high * 0.98
except Exception as _ex:
    log.warning("breadth proxy: SPX recent-high context failed: %s", _ex)


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


# ---- BREADTH PROXY DIRECTION (RSP/SPY) + DIVERGENCE ----
# Additive to the existing NYMO / sector-breadth tiles (not a replacement).
breadth_proxy_divergence = False
if _bp is not None:
    _bp_dir = _bp["direction"]
    _bp_streak = _bp["streak"]
    _bp_stale = _bp.get("stale", False)
    _bp_tag = " (last known)" if _bp_stale else ""
    primary = primary + " | breadth proxy (RSP/SPY) %s: %d session%s%s" % (
        _bp_dir, _bp_streak, "s" if _bp_streak != 1 else "", _bp_tag)
    # Divergence: SPX at/near recent highs while RSP/SPY ratio is falling.
    if _spx_near_high and _bp_dir == "NARROWING" and not _bp_stale:
        breadth_proxy_divergence = True
        primary = primary + " | BREADTH DIVERGENCE (RSP/SPY) CONFIRMED"
        log.info("BREADTH DIVERGENCE (RSP/SPY) CONFIRMED: SPX near highs, ratio narrowing")
# ---- LAYER-2 SIGNAL CHECK (GEX flip, VIX backwardation, McClellan divergence) ----
# _vix_backwardation is now set from the real VIX/VIX3M term-structure tile
# (compute_vix_term_structure) above — no longer inferred from VVIX text.
# The three ENTRY-SIGNAL inputs remain a 2-of-3 set:
#   1) gamma_flip           — GEX flip (manual/unavailable: SpotGamma paywalled)
#   2) _vix_backwardation   — VIX term structure in backwardation (LIVE via Yahoo)
#   3) mcclellan_divergence — NYMO negative while SPX near highs
# GEX-flip is retained (not dropped) as an explicit unavailable input so the
# 2-of-3 threshold is unchanged and the math stays intentional.
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
# BUILD 18 TILES
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
p.append(("17. VIX Term Structure (VIX/VIX3M)", vix_ts_sub, vix_ts_col))

# ---- Tile 18: breadth proxy (RSP/SPY) — directional/relative, NOT a precise % ----
if _bp is not None:
    _bp_ma = "above" if (_bp.get("sma50") is not None and _bp["ratio"] >= _bp["sma50"]) else "below"
    _bp_stale2 = _bp.get("stale", False)
    bp_sub = "RSP/SPY %s, ratio %s 50d MA — %d session%s%s%s" % (
        _bp["direction"],
        _bp_ma,
        _bp["streak"], "s" if _bp["streak"] != 1 else "",
        " | DIVERGENCE vs SPX highs" if breadth_proxy_divergence else "",
        " (last known)" if _bp_stale2 else "")
    if breadth_proxy_divergence:
        bp_col = "red"
    elif _bp_stale2:
        bp_col = "amber"
    else:
        bp_col = "green" if _bp["direction"] == "BROADENING" else "red"
else:
    bp_sub, bp_col = "unavailable", "gray"
p.append(("18. Breadth proxy (RSP/SPY)", bp_sub, bp_col))


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

# ===========================================================================
# HTML EMAIL (compact dark card; fully inline CSS; table-based for Gmail/Outlook/iPhone)
# Renders identically on a wide desktop (fixed 440px centered card) and iPhone Mail.
# Status-dot colours are derived from each tile's live PAL colour key
# (green/amber/red/gray) - nothing about the numbers is hardcoded.
# ===========================================================================

# ---- theme tokens ----
_C_PAGE   = "#0f1115"
_C_PANEL  = "#12161c"
_C_PANEL2 = "#161a20"
_C_BORDER = "#262c36"
_C_TEXT   = "#f4f6fa"
_C_MUTED  = "#8b95a5"
_DOT = {"green": "#4caf7d", "amber": "#e0a72d", "red": "#e0533d", "gray": "#8b95a5"}

def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

# verdict pill: STAND DOWN / WATCH / INITIATE, derived from the live verdict text.
_pv = (primary or "").upper()
if "INITIATE" in _pv or "SHORT NOW" in _pv or "GO" in _pv.split():
    _banner_txt, _banner_bg, _banner_fg = "INITIATE", "#e0533d", "#0f1115"
elif "WATCH" in _pv:
    _banner_txt, _banner_bg, _banner_fg = "WATCH", "#e0a72d", "#0f1115"
else:
    _banner_txt, _banner_bg, _banner_fg = "STAND DOWN", "#4caf7d", "#0f1115"

# 200DMA gate note (live)
if spx_above_200dma is True:
    _gate_note = "200DMA gate: SPX above 200MA (~%.0f) - short conviction capped." % (spx_200dma or 0)
elif spx_above_200dma is False:
    _gate_note = "200DMA gate: SPX below 200MA - gate open."
else:
    _gate_note = "200DMA gate: status unavailable."

# hero SPX metrics (all live)
_h_level = fmt_money(spx_proxy) if spx_proxy else "n/a"
_h_day = ("%+.2f%%" % spy_chg) if spy_chg is not None else "n/a"
_h_day_col = _DOT["green"] if (spy_chg or 0) >= 0 else _DOT["red"]
if spx_proxy and spx_200dma:
    _v200 = (spx_proxy / (spx_200dma * 10.0) - 1.0) * 100.0
    _h_200 = "%+.1f%%" % _v200
    _h_200_col = _DOT["green"] if _v200 >= 0 else _DOT["red"]
else:
    _h_200, _h_200_col = "n/a", _C_MUTED
if spx_proxy and spx_50dma:
    _v50 = (spx_proxy / (spx_50dma * 10.0) - 1.0) * 100.0
    _h_50 = "%+.1f%%" % _v50
    _h_50_col = _DOT["green"] if _v50 >= 0 else _DOT["red"]
else:
    _h_50, _h_50_col = "n/a", _C_MUTED

# tally from live tile colours
_n_red = sum(1 for _t, _s, _c in p if _c == "red")
_n_amber = sum(1 for _t, _s, _c in p if _c == "amber")
_n_green = sum(1 for _t, _s, _c in p if _c == "green")
_n_gray = sum(1 for _t, _s, _c in p if _c == "gray")

# indicator grid rows: coloured dot + label + right-aligned value (all live)
_grid_rows = ""
for _title, _sub, _ckey in p:
    _dotc = _DOT.get(_ckey, _C_MUTED)
    _grid_rows += (
        '<tr>'
        '<td style="padding:7px 0;border-bottom:1px solid %s;width:16px;vertical-align:top;">' % _C_BORDER +
        '<span style="display:inline-block;width:9px;height:9px;border-radius:50%%;background:%s;"></span></td>' % _dotc +
        '<td style="padding:7px 8px;border-bottom:1px solid %s;font-size:12px;color:%s;line-height:1.35;">%s</td>' % (_C_BORDER, _C_TEXT, _esc(_title)) +
        '<td align="right" style="padding:7px 0;border-bottom:1px solid %s;font-size:11px;color:%s;line-height:1.35;white-space:nowrap;vertical-align:top;">%s</td>' % (_C_BORDER, _C_MUTED, _esc(_sub)) +
        '</tr>')

# data flags strip
_flags = []
if IS_HOLIDAY:
    _flags.append("US market holiday - figures may reflect the prior session")
if IS_WEEKEND:
    _flags.append("US market closed (weekend) - figures reflect the prior session")
if _n_gray:
    _flags.append("%d/%d indicators unavailable this run" % (_n_gray, TOTAL_TILES))
if not _flags:
    _flags.append("All %d indicators returned live data" % TOTAL_TILES)
_flags_html = " &nbsp;|&nbsp; ".join(_esc(f) for f in _flags)

# next session label (informational)
_next_session = "Next update after the next US close (~22:00 UTC, Mon-Fri)"

html = (
    '<div style="margin:0;padding:0;background:' + _C_PAGE + ';">'
    '<div style="width:100%;background:' + _C_PAGE + ';padding:18px 0;">'
    # fixed-width centered card
    '<table role="presentation" align="center" width="440" cellpadding="0" cellspacing="0" border="0" '
    'style="width:440px;max-width:440px;margin:0 auto;background:' + _C_PANEL + ';border:1px solid ' + _C_BORDER + ';'
    'border-radius:14px;font-family:-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;">'
    # (1) HEADER
    '<tr><td style="padding:16px 18px 10px 18px;">'
    '<div style="font-size:15px;font-weight:700;color:' + _C_TEXT + ';letter-spacing:0.3px;">MacroSage &middot; SHORT</div>'
    '<div style="font-size:11px;color:' + _C_MUTED + ';padding-top:3px;">' + _esc(today) + '</div>'
    '<div style="font-size:11px;color:' + _C_MUTED + ';padding-top:1px;">' + _esc(_next_session) + '</div>'
    '</td></tr>'
    # (2) HERO SPX BLOCK
    '<tr><td style="padding:0 18px;">'
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
    'style="background:' + _C_PANEL2 + ';border:1px solid ' + _C_BORDER + ';border-radius:10px;">'
    '<tr><td style="padding:12px 14px;">'
    '<div style="font-size:11px;color:' + _C_MUTED + ';text-transform:uppercase;letter-spacing:0.6px;">S&amp;P 500 (SPY x10)</div>'
    '<div style="font-size:26px;font-weight:700;color:' + _C_TEXT + ';padding-top:2px;">' + _esc(_h_level) + '</div>'
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="padding-top:8px;"><tr>'
    '<td width="33%" style="font-size:10px;color:' + _C_MUTED + ';">DAY<br><span style="font-size:14px;font-weight:600;color:' + _h_day_col + ';">' + _esc(_h_day) + '</span></td>'
    '<td width="33%" style="font-size:10px;color:' + _C_MUTED + ';">vs 200DMA<br><span style="font-size:14px;font-weight:600;color:' + _h_200_col + ';">' + _esc(_h_200) + '</span></td>'
    '<td width="33%" style="font-size:10px;color:' + _C_MUTED + ';">vs 50DMA<br><span style="font-size:14px;font-weight:600;color:' + _h_50_col + ';">' + _esc(_h_50) + '</span></td>'
    '</tr></table>'
    '</td></tr></table></td></tr>'
    # (3) VERDICT BANNER + gate note + summary
    '<tr><td style="padding:14px 18px 0 18px;">'
    '<div style="text-align:center;"><span style="display:inline-block;background:' + _banner_bg + ';color:' + _banner_fg + ';'
    'font-size:14px;font-weight:700;letter-spacing:1px;padding:7px 20px;border-radius:20px;">' + _esc(_banner_txt) + '</span></div>'
    '<div style="font-size:11px;color:' + _C_MUTED + ';text-align:center;padding-top:8px;">' + _esc(_gate_note) + '</div>'
    '<div style="font-size:12px;color:' + _C_TEXT + ';line-height:1.5;padding-top:8px;">' + _esc(final_signal) + '</div>'
    '</td></tr>'
    # (4) PRIMARY + LAYER 2 verdict rows
    '<tr><td style="padding:14px 18px 0 18px;">'
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
    '<tr><td style="padding:9px 12px;background:' + _C_PANEL2 + ';border:1px solid ' + _C_BORDER + ';border-radius:8px;">'
    '<span style="font-size:10px;color:' + _C_MUTED + ';text-transform:uppercase;letter-spacing:0.6px;">Primary</span><br>'
    '<span style="font-size:12px;color:' + _C_TEXT + ';line-height:1.45;">' + _esc(primary) + '</span></td></tr>'
    '<tr><td style="height:8px;line-height:8px;font-size:8px;">&nbsp;</td></tr>'
    '<tr><td style="padding:9px 12px;background:' + _C_PANEL2 + ';border:1px solid ' + _C_BORDER + ';border-radius:8px;">'
    '<span style="font-size:10px;color:' + _C_MUTED + ';text-transform:uppercase;letter-spacing:0.6px;">Layer 2</span><br>'
    '<span style="font-size:12px;color:' + _C_TEXT + ';line-height:1.45;">' + _esc(layer2) + '</span></td></tr>'
    '</table></td></tr>'
    # (5) INDICATOR GRID
    '<tr><td style="padding:14px 18px 0 18px;">'
    '<div style="font-size:10px;color:' + _C_MUTED + ';text-transform:uppercase;letter-spacing:0.6px;padding-bottom:2px;">Indicators</div>'
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
    + _grid_rows +
    '</table></td></tr>'
    # (6) TALLY
    '<tr><td style="padding:12px 18px 0 18px;">'
    '<div style="font-size:11px;color:' + _C_MUTED + ';text-align:center;">'
    '<span style="color:' + _DOT["red"] + ';font-weight:700;">' + str(_n_red) + ' red</span> &nbsp;&middot;&nbsp; '
    '<span style="color:' + _DOT["amber"] + ';font-weight:700;">' + str(_n_amber) + ' yellow</span> &nbsp;&middot;&nbsp; '
    '<span style="color:' + _DOT["green"] + ';font-weight:700;">' + str(_n_green) + ' green</span></div>'
    '</td></tr>'
    # (7) LAYER 2 TACTICAL BLOCK
    '<tr><td style="padding:14px 18px 0 18px;">'
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
    'style="background:' + _C_PANEL2 + ';border:1px solid ' + _C_BORDER + ';border-radius:8px;"><tr><td style="padding:10px 12px;">'
    '<div style="font-size:10px;color:' + _C_MUTED + ';text-transform:uppercase;letter-spacing:0.6px;">Layer 2 &middot; Tactical</div>'
    '<div style="font-size:12px;color:' + _C_TEXT + ';line-height:1.5;padding-top:4px;">' + _esc(layer2) + '</div>'
    '</td></tr></table></td></tr>'
    # (8) DATA FLAGS STRIP
    '<tr><td style="padding:14px 18px 0 18px;">'
    '<div style="background:#231a10;border:1px solid #4a3a1a;border-radius:8px;padding:9px 12px;'
    'font-size:11px;color:#e0a72d;line-height:1.5;">&#9888; DATA FLAGS: ' + _flags_html + '</div>'
    '</td></tr>'
    # (9) FOOTER
    '<tr><td style="padding:14px 18px 18px 18px;">'
    '<div style="border-top:1px solid ' + _C_BORDER + ';padding-top:10px;font-size:10px;color:' + _C_MUTED + ';line-height:1.5;">'
    'Generated ' + _esc(now) + '. Sources: FMP /stable/, FRED, Yahoo Finance, CFTC/Tradingster, Stooq, WSJ. '
    + str(TILES_WITH_DATA) + ' of ' + str(TOTAL_TILES) + ' indicators retrieved this run. '
    'Research/educational only - not investment advice.</div>'
    '</td></tr>'
    '</table></div></div>'
)

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
