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
  * a final summary line: "Run complete - X/18 signals retrieved, email sent: Y"
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
DEFAULT_RECIPIENTS = ["wolfgangduke@gmail.com"]
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
    """Include the named recipient; merge in anything from MAIL_TO."""
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
def keep(name, value, lo=None, hi=None, source="live"):
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
        log.info("metric %s = %.4g (%s)", name, value, source)
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
    # TEST SEAM: force the FMP path to look dead so a workflow_dispatch run can
    # exercise the free fallback tier on purpose. Off unless FMP_FORCE_FAIL is set.
    if cfg("FMP_FORCE_FAIL") in ("1", "true", "True"):
        return None, "forced-fail (test seam)"
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
def _stooq_last_closes(symbol, n=2):
    # Free Stooq daily CSV -> last n closes (oldest->newest). Fallback fetcher for
    # the spot-quote tiles. Intentionally duplicates the nested _stooq_closes inside
    # compute_breadth_proxy (kept separate on purpose; do not remove the nested one).
    url = "https://stooq.com/q/d/l/?s=%s&i=d" % symbol
    text, err = _http_get_text(url, timeout=20)
    if not text or not text.strip().lower().startswith("date"):
        log.warning("stooq %s: bad/empty response (%s)", symbol, err)
        return None
    closes = []
    for row in text.strip().splitlines()[1:]:
        parts = row.split(",")
        if len(parts) >= 5:
            try:
                closes.append(float(parts[4]))
            except (ValueError, IndexError):
                pass
    return closes[-n:] or None
def cot_emini():
    url = ("https://publicreporting.cftc.gov/resource/gpe5-46if.json"
           "?$where=upper(contract_market_name)%20like%20'%25E-MINI%20S%26P%20500%25'"
           "&$order=report_date_as_yyyy_mm_dd%20DESC&$limit=1")
    d, _ = http_get_json(url, headers=UA_HDR)
    return d[0] if isinstance(d, list) and d else None
# ---------------------------------------------------------------------------
# WSJ Markets Diary — fetched ONCE per run, shared by NYMO and the breadth
# fallback (the old Yahoo ^ADVN/^DECN fallback was dead: 401/404).
# ---------------------------------------------------------------------------
_WSJ_AD = {"fetched": False, "data": None}
def fetch_wsj_ad():
    """NYSE advances/declines from the WSJ Markets Diary JSON (free, no auth).
    Returns {"adv":int,"dec":int,"adv_prv":int,"dec_prv":int} (any may be None)
    or None on failure. Result is memoized for the run."""
    if _WSJ_AD["fetched"]:
        return _WSJ_AD["data"]
    _WSJ_AD["fetched"] = True
    try:
        _wsj_url = (
            "https://www.wsj.com/market-data/stocks/marketsdiary"
            "?id=%7B%22application%22%3A%22WSJ%22"
            "%2C%22marketsDiaryType%22%3A%22diaries%22%7D"
            "&type=mdc_marketsdiary"
        )
        d, e = http_get_json(_wsj_url, headers=UA_HDR)
        if not d:
            log.warning("WSJ diaries: fetch failed (%s)", e)
            return None
        _sets = d.get("data", {}).get("instrumentSets", [])
        _nyse = next((s for s in _sets
            if (s.get("headerFields") or [{}])[0].get("label", "").upper() == "NYSE"),
            None)
        if not _nyse:
            log.warning("WSJ diaries: NYSE set not found")
            return None
        def _val(instr, row_id, field):
            row = next((r for r in instr if r.get("id") == row_id), None)
            if not row:
                return None
            raw = str(row.get(field, "")).replace(",", "").strip()
            return int(float(raw)) if raw else None
        _instr = _nyse.get("instruments", [])
        out = {
            "adv": _val(_instr, "advances", "latestClose"),
            "dec": _val(_instr, "declines", "latestClose"),
            "adv_prv": _val(_instr, "advances", "previousClose"),
            "dec_prv": _val(_instr, "declines", "previousClose"),
        }
        _WSJ_AD["data"] = out
        return out
    except Exception as ex:
        log.warning("WSJ diaries error: %s", ex)
        return None
# ===========================================================================
# LIVE INDICATOR FETCHERS  (live-indicators branch, 2026-06-30)
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
    try:
        _w = fetch_wsj_ad()
        if _w:
            _sess = []
            if _w["adv_prv"] is not None and _w["dec_prv"] is not None:
                _sess.append(_w["adv_prv"] - _w["dec_prv"])  # older session first
            if _w["adv"] is not None and _w["dec"] is not None:
                _sess.append(_w["adv"] - _w["dec"])
            if _sess:
                log.info("NYMO A/D: WSJ diaries NYSE, %d sessions (adv=%s dec=%s)",
                         len(_sess), _w["adv"], _w["dec"])
                return _sess[-n:]
            log.warning("NYMO A/D WSJ: advances/declines missing in NYSE set")
    except Exception as ex:
        log.warning("NYMO A/D WSJ error: %s", ex)
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
    series = fetch_ad_series(60)
    if not series:
        log.warning("NYMO compute: A/D data unavailable")
        return None
    mult19 = 2.0 / (19 + 1)
    mult39 = 2.0 / (39 + 1)
    prev19 = cache.get("nymo_ema19")
    prev39 = cache.get("nymo_ema39")
    if prev19 is None and len(series) > 1:
        ema19 = ema39 = float(series[0])
        for net_adv in series[1:]:
            ema19 = net_adv * mult19 + ema19 * (1 - mult19)
            ema39 = net_adv * mult39 + ema39 * (1 - mult39)
        warming_up = len(series) < 39
        if not warming_up:
            cache.set("nymo_warmed", 1.0, RUN_TS)
        mark_session(cache, "nymo_session")
    elif prev19 is None:
        ema19 = ema39 = float(series[-1])
        warming_up = True
        mark_session(cache, "nymo_session")
    else:
        # SESSION GUARD (added 2026-07-03): only fold today's net-advance into
        # the EMAs ONCE per US trading day. Weekend/holiday runs and repeated
        # same-day runs previously re-applied the same session repeatedly,
        # silently distorting the oscillator.
        if not is_new_session(cache, "nymo_session"):
            cached_nymo = cache.get("nymo")
            warming_up = not bool(cache.get("nymo_warmed"))
            if cached_nymo is not None:
                log.info("NYMO: same/non-trading session — reusing %.2f, EMAs not advanced",
                         cached_nymo)
                return {"nymo": cached_nymo, "warming_up": warming_up}
        net_adv = series[-1]
        ema19 = net_adv * mult19 + prev19 * (1 - mult19)
        ema39 = net_adv * mult39 + prev39 * (1 - mult39)
        warming_up = not bool(cache.get("nymo_warmed"))
        mark_session(cache, "nymo_session")
    cache.set("nymo_ema19", round(ema19, 4), RUN_TS)
    cache.set("nymo_ema39", round(ema39, 4), RUN_TS)
    nymo = round(ema19 - ema39, 2)
    log.info("NYMO computed = %.2f (ema19=%.2f, ema39=%.2f, warm=%s, sessions=%d)",
             nymo, ema19, ema39, warming_up, len(series))
    return {"nymo": nymo, "warming_up": warming_up}
def fetch_naaim():
    text, err = _http_get_text(
        "https://naaim.org/programs/naaim-exposure-index/",
        timeout=25,
    )
    if not text:
        log.warning("NAAIM: page fetch failed (%s)", err)
        return None
    import re as _re
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
    text2, err2 = _http_get_text("https://www.aaii.com/sentimentsurvey/sent_results", timeout=20)
    if text2:
        import re as _re
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
    log.warning("AAII: all fetches failed (%s)", err2)
    return None
def fetch_cot_tradingster():
    text, err = _http_get_text(
        "https://www.tradingster.com/cot/futures/fin/13874A",
        timeout=20,
    )
    if not text:
        log.warning("Tradingster COT fetch error: %s", err)
        return None
    import re as _re
    # FIXED 2026-07-03: patterns previously lacked DOTALL so '.' never crossed
    # the newlines inside the HTML table and the fallback could never match.
    # Also switched the truthiness test to `is not None` so a legitimate zero
    # position no longer discards the whole result.
    def _tg(pat):
        m = _re.search(pat, text, _re.DOTALL | _re.IGNORECASE)
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", ""))
        except Exception:
            return None
    am_l = _tg(r'Asset Manager[^<]*(?:<[^>]*>\s*)*?([\d,]{3,})')
    am_l = _tg(r'Asset Manager.*?Long.*?([\d,]+)') if am_l is None else am_l
    am_s = _tg(r'Asset Manager.*?Short.*?([\d,]+)')
    lev_l = _tg(r'Leveraged.*?Long.*?([\d,]+)')
    lev_s = _tg(r'Leveraged.*?Short.*?([\d,]+)')
    if all(v is not None for v in (am_l, am_s, lev_l, lev_s)):
        return {
            "asset_mgr_positions_long": am_l,
            "asset_mgr_positions_short": am_s,
            "lev_money_positions_long": lev_l,
            "lev_money_positions_short": lev_s,
            "change_in_lev_money_long": 0,
            "change_in_lev_money_short": 0,
            "_source": "tradingster",
        }
    return None
def _yahoo_closes_range(symbol, rng="6mo"):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?range=%s&interval=1d" % (symbol, rng))
    d, _ = http_get_json(url, headers=UA_HDR)
    try:
        q = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        vals = [float(c) for c in q if c is not None]
        return vals or None
    except Exception:
        return None
def _yahoo_monthly_closes(symbol, rng="3y"):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?range=%s&interval=1mo" % (symbol, rng))
    d, _ = http_get_json(url, headers=UA_HDR)
    try:
        q = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        vals = [float(c) for c in q if c is not None]
        return vals or None
    except Exception:
        return None
def _ema(values, period):
    if not values or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / float(period)  # SMA seed
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema
def fetch_rsp_spy_ratio(n=120):
    try:
        rsp = _yahoo_closes_range("RSP", "6mo")
        spy = _yahoo_closes_range("SPY", "6mo")
        if rsp and spy:
            m = min(len(rsp), len(spy))
            if m >= 60:
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
    try:
        def _stooq_closes(symbol):
            url = "https://stooq.com/q/d/l/?s=%s&i=d" % symbol
            text, err = _http_get_text(url, timeout=20)
            if not text or not text.strip().lower().startswith("date"):
                log.warning("breadth proxy Stooq %s: bad/empty response (%s)", symbol, err)
                return None
            lines = text.strip().splitlines()
            closes = []
            for row in lines[1:]:
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
                # DATE GUARD (2026-07-03): only advance the streak when the
                # underlying data session actually changed (weekend/holiday and
                # repeated same-day runs no longer inflate it).
                _last_day = cache.get("vix_ts_last_day")
                if regime == "BACKWARDATION":
                    streak = prev + 1 if day != _last_day else max(prev, 1)
                else:
                    streak = 0
                cache.set("vix_ts_last_day", day, RUN_TS)
                cache.set("vix_ts_vix", round(vix, 2), RUN_TS)
                cache.set("vix_ts_vix3m", round(v3, 2), RUN_TS)
                cache.set("vix_ts_ratio", round(ratio, 4), RUN_TS)
                cache.set("vix_ts_regime", regime, RUN_TS)
                cache.set("backwardation_streak", streak, RUN_TS)
                log.info("VIX term structure: VIX=%.2f VIX3M=%.2f ratio=%.4f -> %s"
                         " (backwardation streak=%d, %s)",
                         vix, v3, ratio, regime, streak, day)
                # Issue #17: full common-date ratio series for the
                # term-structure VELOCITY measure (rate of flattening).
                _series = [vix_map[d] / v3_map[d] for d in common
                           if v3_map[d] and 5 <= vix_map[d] <= 150 and 5 <= v3_map[d] <= 150]
                return {"vix": vix, "vix3m": v3, "ratio": ratio,
                        "regime": regime, "streak": streak, "date": day,
                        "series": _series, "stale": False}
            log.warning("VIX term structure: values out of range VIX=%s VIX3M=%s",
                        vix, v3)
        else:
            log.warning("VIX term structure: no common date between ^VIX and ^VIX3M")
    else:
        log.warning("VIX term structure: Yahoo fetch failed (vix=%s vix3m=%s)",
                    "ok" if vix_map else "None", "ok" if v3_map else "None")
    cached = cache.get("vix_ts_ratio")
    if cached is not None:
        log.warning("VIX term structure: live unavailable; using last-known")
        return {"vix": cache.get("vix_ts_vix"), "vix3m": cache.get("vix_ts_vix3m"),
                "ratio": cached, "regime": cache.get("vix_ts_regime") or "CONTANGO",
                "streak": int(cache.get("backwardation_streak") or 0),
                "date": None, "stale": True}
    log.warning("VIX term structure: no data and no cache")
    return None
def compute_vix9d_ratio():
    """VIX9D/VIX front-of-curve ratio (issue #17, keyless via Yahoo).
    Returns (ratio, vix9d, vix, date) or None on any failure (fail-safe)."""
    m9 = _yahoo_closes_dated("%5EVIX9D", "6mo")
    mv = _yahoo_closes_dated("%5EVIX", "6mo")
    if m9 and mv:
        common = sorted(d for d in m9 if d in mv)
        if common:
            day = common[-1]
            v9, vx = m9[day], mv[day]
            if vx and 5 <= v9 <= 200 and 5 <= vx <= 150:
                return v9 / vx, v9, vx, day
            log.warning("VIX9D/VIX: values out of range v9=%s vix=%s", v9, vx)
        else:
            log.warning("VIX9D/VIX: no common date between ^VIX9D and ^VIX")
    return None
def compute_breadth_proxy(cache):
    series = fetch_rsp_spy_ratio(120)
    stale = False
    if not series or len(series) < 50:
        cached = cache.get("breadth_proxy_ratio")
        if cached is None:
            log.warning("breadth proxy: no data and no cache")
            return None
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
    N = 5
    window = series[-(N + 1):] if len(series) > N else series
    slope = (window[-1] - window[0]) / max(len(window) - 1, 1)
    rising = slope > 0
    above_ma = ratio >= sma50
    direction = "BROADENING" if (rising and above_ma) else "NARROWING"
    prev_dir = cache.get("breadth_proxy_dir")
    prev_streak = int(cache.get("breadth_proxy_streak") or 0)
    # SESSION GUARD (2026-07-03): streak counts trading sessions, not runs.
    if direction == prev_dir:
        if is_new_session(cache, "bp_session"):
            streak = prev_streak + 1
            mark_session(cache, "bp_session")
        else:
            streak = max(prev_streak, 1)
    else:
        streak = 1
        mark_session(cache, "bp_session")
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
    if d.weekday() == 5:
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:
        return d + datetime.timedelta(days=1)
    return d
def market_holidays(year):
    h = set()
    h.add(_observed(datetime.date(year, 1, 1)))
    h.add(_nth_weekday(year, 1, 0, 3))
    h.add(_nth_weekday(year, 2, 0, 3))
    h.add(_easter(year) - datetime.timedelta(days=2))
    h.add(_last_weekday(year, 5, 0))
    h.add(_observed(datetime.date(year, 6, 19)))
    h.add(_observed(datetime.date(year, 7, 4)))
    h.add(_nth_weekday(year, 9, 0, 1))
    h.add(_nth_weekday(year, 11, 3, 4))
    h.add(_observed(datetime.date(year, 12, 25)))
    return h
def eastern_now():
    """Current US/Eastern datetime, dependency-free DST estimate."""
    utc = _utcnow()
    y = utc.year
    dst_start = _nth_weekday(y, 3, 6, 2)
    dst_end = _nth_weekday(y, 11, 6, 1)
    is_dst = dst_start <= utc.date() < dst_end
    return utc - datetime.timedelta(hours=4 if is_dst else 5)
def eastern_today():
    return eastern_now().date()
ET_TODAY = eastern_today()
IS_HOLIDAY = ET_TODAY in market_holidays(ET_TODAY.year)
IS_WEEKEND = ET_TODAY.weekday() >= 5
if IS_HOLIDAY:
    log.warning("US market HOLIDAY today (%s ET) - data may be stale; sending with a flag", ET_TODAY)
elif IS_WEEKEND:
    log.warning("US market closed (weekend, %s ET) - data may be stale", ET_TODAY)
else:
    log.info("US trading day: %s ET", ET_TODAY)
# ---------------------------------------------------------------------------
# SESSION GUARDS (added 2026-07-03). All persisted streaks and incremental
# EMAs must advance at most ONCE per US trading day. Weekend/holiday runs and
# multiple same-day runs previously re-counted the same session, inflating
# streaks and corrupting the NYMO EMAs in state.json.
# ---------------------------------------------------------------------------
def is_new_session(cache, key):
    """True when today is a US trading day AND this guard-key hasn't been
    advanced yet today. Weekends/holidays never count as a new session."""
    if IS_WEEKEND or IS_HOLIDAY:
        return False
    return cache.get(key) != ET_TODAY.isoformat()
def mark_session(cache, key):
    if not (IS_WEEKEND or IS_HOLIDAY):
        cache.set(key, ET_TODAY.isoformat(), RUN_TS)
log.info("=== gathering live data (FMP key: %s, FRED key: %s) ===",
         "set" if FMP else "MISSING", "set" if FRED else "MISSING")
D = {}
for key, path in [
    ("spy", "quote?symbol=SPY"),
    ("vix", "quote?symbol=%5EVIX"),
    ("gold", "quote?symbol=GCUSD"),
    ("treasury", "treasury-rates"),
]:
    d, e = fmp(path)
    if d:
        D[key] = d[0] if isinstance(d, list) and d else d
    else:
        log.warning("FMP %s unavailable (%s)", key, e)
# FIXED 2026-07-06: use ET_TODAY (US market day) not the runner's UTC date,
# which could request the wrong calendar day near midnight UTC.
sectors, e = fmp("sector-performance-snapshot?date=%s" % ET_TODAY.isoformat())
if not sectors:
    sectors, e = fmp("sector-performance-snapshot")
spy = D.get("spy", {}) if isinstance(D.get("spy"), dict) else {}
# --- spy_px / spy_chg: FMP primary -> Yahoo -> Stooq (free fallbacks) ---
spy_px_v = num(spy.get("price"))
spy_chg_v = num(spy.get("changePercentage"))
spy_src = "live"
if spy_px_v is None:
    yc = yahoo_closes("SPY", 2)
    if yc and len(yc) >= 2 and yc[-2]:
        spy_px_v, spy_chg_v = yc[-1], (yc[-1] / yc[-2] - 1) * 100
        spy_src = "fallback:yahoo"
    else:
        sc = _stooq_last_closes("spy.us", 2)
        if sc and len(sc) >= 2 and sc[-2]:
            spy_px_v, spy_chg_v = sc[-1], (sc[-1] / sc[-2] - 1) * 100
            spy_src = "fallback:stooq"
spy_px, _ = keep("spy_px", spy_px_v, 50, 2000, source=spy_src)
spy_chg, _ = keep("spy_chg", spy_chg_v, -25, 25, source=spy_src)
spx_proxy = spy_px * 10 if spy_px is not None else None
# --- vix_px: FMP primary -> Yahoo -> Stooq (price only) ---
vix_px_v = num(D.get("vix", {}).get("price") if isinstance(D.get("vix"), dict) else None)
vix_src = "live"
if vix_px_v is None:
    yv = yahoo_closes("^VIX", 1)
    if yv:
        vix_px_v, vix_src = yv[-1], "fallback:yahoo"
    else:
        sv = _stooq_last_closes("^vix", 1)
        if sv:
            vix_px_v, vix_src = sv[-1], "fallback:stooq"
vix_px, _ = keep("vix_px", vix_px_v, 5, 150, source=vix_src)
# --- gold_px: FMP primary -> Yahoo -> Stooq ---
gold_px_v = num(D.get("gold", {}).get("price") if isinstance(D.get("gold"), dict) else None)
gold_src = "live"
if gold_px_v is None:
    yg = yahoo_closes("GC=F", 1)
    if yg:
        gold_px_v, gold_src = yg[-1], "fallback:yahoo"
    else:
        sg = _stooq_last_closes("xauusd", 1)
        if sg:
            gold_px_v, gold_src = sg[-1], "fallback:stooq"
gold_px, _ = keep("gold_px", gold_px_v, 200, 10000, source=gold_src)
# --- y2 / y10: FMP primary -> FRED (DGS2 / DGS10) ---
t = D.get("treasury", {}) if isinstance(D.get("treasury"), dict) else {}
y2_v, y2_src = num(t.get("year2")), "live"
if y2_v is None:
    f2 = fred_series("DGS2", 1)
    if f2:
        y2_v, y2_src = f2[0], "fallback:FRED"
y2, _ = keep("y2", y2_v, -2, 25, source=y2_src)
y10_v, y10_src = num(t.get("year10")), "live"
if y10_v is None:
    f10 = fred_series("DGS10", 1)
    if f10:
        y10_v, y10_src = f10[0], "fallback:FRED"
y10, _ = keep("y10", y10_v, -2, 25, source=y10_src)
spread_bps = round((y10 - y2) * 100) if (y2 is not None and y10 is not None) else None
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
# fallback: WSJ NYSE advances/declines (the old Yahoo ^ADVN/^DECN endpoints
# are dead — 401/404 — see fetch_ad_series comments). Shares the memoized
# fetch with the NYMO tile, so this costs no extra HTTP call.
if breadth is None:
    try:
        _w = fetch_wsj_ad()
        if _w and _w["adv"] is not None and _w["dec"] is not None:
            up, down = _w["adv"], _w["dec"]
            if up + down:
                breadth = round(up / (up + down) * 100)
                log.info("breadth: WSJ NYSE A/D fallback -> %d%% advancing", breadth)
    except Exception as ex:
        log.warning("breadth WSJ fallback failed: %s", ex)
breadth, _ = keep("breadth", breadth, 0, 100)
if breadth is not None:
    breadth = int(round(breadth))
netliq = None
walcl = fred_series("WALCL")
tga = fred_series("WTREGEN")
rrp = fred_series("RRPONTSYD")
if walcl and tga and rrp and len(walcl) >= 2:
    try:
        cur = walcl[0] / 1000.0 - tga[0] / 1000.0 - rrp[0]
        prv = (walcl[1] / 1000.0 - tga[min(1, len(tga) - 1)] / 1000.0
               - rrp[min(1, len(rrp) - 1)])
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
breadth_red = breadth is not None and breadth < 50
netliq_decl = netliq_dir == "declining"
if breadth_red and netliq_decl:
    primary = "WATCHING - both triggers RED (confirm 3-day streak)"
else:
    primary = "WATCHING - Day 1 of 3"
layer2 = "WAIT"
initiate_short = False
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
# ---- Fiscal impulse (full Point-19 MTS spec, 2026-07-03) ----
#   RED   : rolling-12M deficit > $2.0T AND YoY outlays > +8%
#   AMBER : deficit $1.5-2.0T OR outlays +5-8%
#   GREEN : below both
#   Sub-note: gross interest / receipts ⚠ if > 13%
#   Appends: [Deficit $X.XT | Outlays YoY +X% | Interest/Receipts X%]
mts = fred_series("MTSDS133FMS", 12)     # monthly surplus/deficit ($M)
outl = fred_series("MTSO133FMS", 24)     # monthly total outlays ($M)
rcpt = fred_series("MTSR133FMS", 12)     # monthly total receipts ($M)
intr = fred_series("A091RC1Q027SBEA", 1) # fed interest payments ($bn, SAAR)
fisc_sub, fisc_col = "no data", "gray"
deficit_T = outlays_yoy = int_ratio = None
if mts and len(mts) >= 12:
    deficit_T, _ = keep("deficit_T", -sum(mts[:12]) / 1e6, -1, 10)
else:
    deficit_T = CACHE.get("deficit_T")
if outl and len(outl) >= 24:
    _cur12, _prv12 = sum(outl[:12]), sum(outl[12:24])
    if _prv12:
        outlays_yoy, _ = keep("outlays_yoy", (_cur12 / _prv12 - 1) * 100, -50, 50)
else:
    outlays_yoy = CACHE.get("outlays_yoy")
if rcpt and len(rcpt) >= 12 and intr:
    _rc_T = sum(rcpt[:12]) / 1e6           # $T
    _int_T = intr[0] / 1000.0              # $bn SAAR -> $T
    if _rc_T > 0:
        int_ratio, _ = keep("int_ratio", _int_T / _rc_T * 100, 0, 60)
else:
    int_ratio = CACHE.get("int_ratio")
if deficit_T is not None:
    _red = deficit_T > 2.0 and (outlays_yoy is not None and outlays_yoy > 8)
    _amber = ((1.5 <= deficit_T <= 2.0)
              or (outlays_yoy is not None and 5 <= outlays_yoy <= 8)
              # deficit >2T but outlays unknown/below 8% cannot be full red
              or (deficit_T > 2.0 and not _red))
    fisc_col = "red" if _red else ("amber" if _amber else "green")
    _warn = " ⚠ interest/receipts >13%" if (int_ratio is not None and int_ratio > 13) else ""
    fisc_sub = "[Deficit $%.2fT | Outlays YoY %s | Interest/Receipts %s]%s" % (
        deficit_T,
        ("%+.1f%%" % outlays_yoy) if outlays_yoy is not None else "n/a",
        ("%.1f%%" % int_ratio) if int_ratio is not None else "n/a",
        _warn)
def third_friday(y, m):
    fr = [d for d in _cal.Calendar().itermonthdates(y, m) if d.month == m and d.weekday() == 4]
    return fr[2]
def next_third_friday(d):
    """Next monthly OpEx ON OR AFTER d (rolls to next month once passed).
    FIXED 2026-07-03: the old abs() version measured distance to THIS month's
    3rd Friday in either direction, so post-OpEx it reported days SINCE OpEx
    and never saw the next one."""
    tf = third_friday(d.year, d.month)
    if tf < d:
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        tf = third_friday(y, m)
    return tf
_t = ET_TODAY
_next_opex = next_third_friday(_t)
opex_days = (_next_opex - _t).days
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
# OpEx Structural Transition Window: 10 days before monthly OpEx per the
# MacroSage calendar rule (short-gamma dominant window).
if opex_days <= 10:
    cal_flags.append("CALENDAR GATE — TRANSITION WINDOW (OpEx in %dd)" % opex_days)
cal_sub = "; ".join(cal_flags) if cal_flags else "clear"
cal_col = "red" if cal_flags else "green"
cot_sub, cot_col = "no data", "gray"
_cot = cot_emini()
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
        # FIXED 2026-07-06: the Tradingster fallback has no week-over-week change
        # field (hardcoded 0), so show "n/a" rather than a misleading "+0".
        _lev_txt = "n/a" if src == "tradingster" else "%+.0f" % lev_chg
        if am_net > 0:
            cot_col = "green"
            cot_sub = "AM net long %+.0f; Lev chg %s [%s]" % (am_net, _lev_txt, src)
        else:
            cot_col = "red"
            cot_sub = "AM net short %+.0f; Lev chg %s [%s]" % (am_net, _lev_txt, src)
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
_vts = compute_vix_term_structure(CACHE)
vix_ts_sub, vix_ts_col = "unavailable", "gray"
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
# ---- VIX9D/VIX front-of-curve inversion (issue #17, keyless) --------------
# 9-day implied vol above 30-day = stress showing at the FRONT of the curve,
# the earliest term-structure tell. Fail-safe: off when ^VIX9D unavailable.
vix9d_ratio = None
vix9d_inversion = False
_v9 = compute_vix9d_ratio()
if _v9:
    vix9d_ratio, _v9_px, _v9_vix, _v9_day = _v9
    vix9d_inversion = vix9d_ratio >= 1.0
    vix_ts_sub += " | VIX9D/VIX %.3f%s" % (
        vix9d_ratio, " — FRONT-OF-CURVE INVERTED" if vix9d_inversion else "")
    log.info("VIX9D/VIX: %.3f (9d %.2f / 30d %.2f, %s) -> inversion=%s",
             vix9d_ratio, _v9_px, _v9_vix, _v9_day, vix9d_inversion)
else:
    log.warning("VIX9D/VIX: unavailable — signal off (fail-safe)")
# ---- Term-structure VELOCITY: rate of flattening of VIX/VIX3M (issue #17) --
# Not just the >1.0 cross: how FAST the curve is racing toward inversion.
# PRE-ALERT input only (does not join the Layer-2 2-of-3 set). Fail-safe: off.
TS_VELOCITY_THRESH = 0.08   # ratio points per 5 sessions
TS_VELOCITY_FLOOR = 0.95    # only meaningful when already near inversion
ts_velocity = None
ts_accelerating = False
_ts_series = (_vts or {}).get("series") or []
if len(_ts_series) >= 6:
    ts_velocity = _ts_series[-1] - _ts_series[-6]
    ts_accelerating = (ts_velocity >= TS_VELOCITY_THRESH) and (
        _ts_series[-1] >= TS_VELOCITY_FLOOR)
    vix_ts_sub += " | flattening %+.3f/5d%s" % (
        ts_velocity, " — ACCELERATING" if ts_accelerating else "")
    log.info("TS velocity: %+.3f per 5 sessions (ratio %.3f) -> accelerating=%s",
             ts_velocity, _ts_series[-1], ts_accelerating)
else:
    log.warning("TS velocity: insufficient ratio history — off (fail-safe)")
# Layer-2 vol-regime signal: the paywalled GEX/dealer-gamma flip is replaced by
# a keyless REALIZED-VOLATILITY EXPANSION measure (computed below, once SPY
# history is available). Dealers short gamma amplify moves -> realized vol
# expands; that acceleration is the tradable tell. Manual GEX_FLIP=1 / state
# "gex_flip_manual" still forces it on. Fail-safe: off.
_gex_manual = (cfg("GEX_FLIP") == "1") or bool(CACHE.get("gex_flip_manual"))
# ---- SPY daily history WITH VOLUME (Yahoo primary, Stooq fallback) ----
# Volume feeds the new breakdown-volume gate; Stooq removes the single point
# of failure where one Yahoo block froze every trend gate at 'last known'.
def _stooq_daily(symbol):
    """Stooq daily CSV -> (closes, volumes) oldest-first, or None."""
    url = "https://stooq.com/q/d/l/?s=%s&i=d" % symbol
    text, err = _http_get_text(url, timeout=20)
    if not text or not text.strip().lower().startswith("date"):
        log.warning("Stooq %s: bad/empty response (%s)", symbol, err)
        return None
    closes, vols = [], []
    for row in text.strip().splitlines()[1:]:
        parts = row.split(",")
        if len(parts) >= 6:
            try:
                closes.append(float(parts[4]))
                vols.append(float(parts[5]) if parts[5] else 0.0)
            except (ValueError, IndexError):
                pass
    return (closes, vols) if closes else None
def fetch_spy_history():
    """(closes, volumes, source) for ~1y of SPY daily data, oldest-first."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/SPY"
           "?range=1y&interval=1d")
    d, _ = http_get_json(url, headers=UA_HDR)
    try:
        q = d["chart"]["result"][0]["indicators"]["quote"][0]
        closes = [float(c) for c in q["close"] if c is not None]
        vols = [float(v) for v in (q.get("volume") or []) if v is not None]
        if len(closes) >= 60:
            log.info("SPY history: Yahoo, %d sessions", len(closes))
            return closes, vols, "yahoo"
    except Exception:
        pass
    log.warning("SPY history: Yahoo failed; trying Stooq")
    s = _stooq_daily("spy.us")
    if s and len(s[0]) >= 60:
        log.info("SPY history: Stooq, %d sessions", len(s[0]))
        return s[0][-260:], s[1][-260:], "stooq"
    log.warning("SPY history: ALL sources failed")
    return None, None, None
_spy_hist, _spy_vols, _spy_src = fetch_spy_history()
spx_above_200dma = None
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
spx_50dma = None
if _spy_hist and len(_spy_hist) >= 50:
    spx_50dma = sum(_spy_hist[-50:]) / 50.0
else:
    log.warning("SPX 50DMA: insufficient SPY history (%d sessions)",
                len(_spy_hist) if _spy_hist else 0)
# ---- VOLUME GATE (2026-07-03): breakdown volume >= 1.2x the 20-day average.
# Sub-average volume = WAIT (blocks INITIATE). Intraday guard: before the
# 16:00 ET close the last bar is partial, so it is dropped and the prior
# completed session is used instead.
vol_ratio = None
if _spy_vols and len(_spy_vols) >= 25:
    _vols = [v for v in _spy_vols if v]
    _et_now = eastern_now()
    if (_et_now.hour < 16) and not (IS_WEEKEND or IS_HOLIDAY) and len(_vols) > 21:
        _vols = _vols[:-1]
        log.info("volume gate: pre-close run — dropped partial intraday bar")
    if len(_vols) >= 21:
        _v_last = _vols[-1]
        _v_avg20 = sum(_vols[-21:-1]) / 20.0
        if _v_avg20 > 0:
            vol_ratio = _v_last / _v_avg20
            log.info("volume gate: last %.0f vs 20d avg %.0f -> %.2fx (need >=1.20)",
                     _v_last, _v_avg20, vol_ratio)
else:
    log.warning("volume gate: SPY volume unavailable — gate FAIL-CLOSED (blocks INITIATE)")
# ---- VOL-EXPANSION SIGNAL (Layer-2, keyless): 5-day realized vol accelerating
# vs its 20-day baseline (annualized, from SPY daily closes). Replaces the
# retired GEX/dealer-gamma flip. Fail-safe: insufficient history -> off.
vol_expansion = False
rvol5 = rvol20 = None
if _spy_hist and len(_spy_hist) >= 26:
    _rets = [(_spy_hist[i] / _spy_hist[i - 1] - 1.0)
             for i in range(1, len(_spy_hist)) if _spy_hist[i - 1]]
    def _rms(xs):
        if not xs:
            return None
        return (sum(x * x for x in xs) / len(xs)) ** 0.5
    _s5, _s20 = _rms(_rets[-5:]), _rms(_rets[-20:])
    if _s5 is not None and _s20 is not None and _s20 > 0:
        rvol5, rvol20 = _s5 * (252 ** 0.5) * 100, _s20 * (252 ** 0.5) * 100
        vol_expansion = (_s5 / _s20) >= 1.30
        log.info("vol-expansion (Layer-2): rvol5=%.1f%% vs rvol20=%.1f%% -> "
                 "ratio %.2f (fire >=1.30) = %s", rvol5, rvol20, _s5 / _s20, vol_expansion)
else:
    log.warning("vol-expansion: insufficient SPY history — signal off (fail-safe)")
gamma_flip = bool(_gex_manual or vol_expansion or vix9d_inversion)
# ---- R:R GATE (2026-07-03): reward:risk must be >= 5.0, else WATCHING.
#   entry  = current SPY price
#   stop   = highest close of the last 5 sessions (floor: entry +0.5%)
#            -> override with env/state RR_STOP (SPY points)
#   target = measured-move projection: entry - (52wk high - entry), i.e. the
#            existing drawdown doubled. Deliberately mechanical; override with
#            env/state RR_TARGET when you have a level-based target.
# Fail-closed: missing inputs -> rr_value None -> INITIATE blocked.
rr_value = rr_stop = rr_target = None
try:
    _stop_ovr = num(cfg("RR_STOP")) or num(CACHE.get("rr_stop_manual"))
    _tgt_ovr = num(cfg("RR_TARGET")) or num(CACHE.get("rr_target_manual"))
    if spy_px is not None and _spy_hist and len(_spy_hist) >= 30:
        _hi52 = max(_spy_hist[-252:]) if len(_spy_hist) >= 252 else max(_spy_hist)
        _swing_hi = max(_spy_hist[-5:])
        rr_stop = _stop_ovr if _stop_ovr else max(_swing_hi, spy_px * 1.005)
        rr_target = _tgt_ovr if _tgt_ovr else (spy_px - (_hi52 - spy_px))
        _risk = rr_stop - spy_px
        _reward = spy_px - rr_target
        if _risk > 0 and _reward > 0:
            rr_value = _reward / _risk
            log.info("R:R gate: entry %.2f stop %.2f target %.2f -> %.1f (need >=5.0)%s%s",
                     spy_px, rr_stop, rr_target, rr_value,
                     " [stop override]" if _stop_ovr else "",
                     " [target override]" if _tgt_ovr else "")
        else:
            log.warning("R:R gate: non-positive risk/reward (stop %.2f target %.2f entry %.2f)",
                        rr_stop, rr_target, spy_px)
    else:
        log.warning("R:R gate: inputs unavailable — gate FAIL-CLOSED (blocks INITIATE)")
except Exception as _ex:
    log.warning("R:R gate error: %s — gate FAIL-CLOSED", _ex)
_bp = compute_breadth_proxy(CACHE)
_spx_near_high = None
try:
    _spy_wide = _spy_hist if _spy_hist else _yahoo_closes_range("SPY", "1y")
    if _spy_wide and len(_spy_wide) >= 20 and spy_px is not None:
        _recent_high = max(_spy_wide[-252:]) if len(_spy_wide) >= 252 else max(_spy_wide)
        _spx_near_high = spy_px >= _recent_high * 0.98
except Exception as _ex:
    log.warning("breadth proxy: SPX recent-high context failed: %s", _ex)
mcclellan_divergence = (nymo_col == "red") and bool(_spx_near_high)
if spx_above_200dma is True:
    _200dma_note = " | 200DMA GATE: SPX above 200MA (~%.0f) — cap short conviction YELLOW" % (spx_200dma or 0)
    primary = primary + _200dma_note
    initiate_short = False
elif spx_above_200dma is False:
    primary = primary + " | 200DMA: SPX BELOW 200MA — structural short regime valid"
spx_above_10mema = None
spx_10mema = None
_spy_monthly = _yahoo_monthly_closes("SPY", "3y")
if _spy_monthly and len(_spy_monthly) >= 10:
    spx_10mema = _ema(_spy_monthly, 10)
    if spx_10mema is not None and spy_px is not None:
        spx_above_10mema = (spy_px >= spx_10mema)
        log.info("MONTHLY TREND GATE: SPY %.2f vs 10M EMA %.2f -> above=%s (%d monthly closes)",
                 spy_px, spx_10mema, spx_above_10mema, len(_spy_monthly))
    else:
        log.warning("MONTHLY TREND GATE: 10M EMA=%s but SPY price unavailable; gate UNKNOWN",
                    ("%.2f" % spx_10mema) if spx_10mema is not None else "None")
else:
    log.warning("MONTHLY TREND GATE: insufficient monthly SPY closes (%s); gate UNKNOWN",
                len(_spy_monthly) if _spy_monthly else 0)
if spx_above_10mema is not False:
    if initiate_short:
        log.info("MONTHLY TREND GATE: blocking INITIATE SHORT (gate not open)")
    initiate_short = False
    _pv_up = primary.upper()
    if "INITIATE" in _pv_up or "SHORT NOW" in _pv_up:
        primary = primary.replace("INITIATE SHORT", "WATCHING").replace("INITIATE", "WATCHING").replace("SHORT NOW", "WATCHING")
if spx_above_10mema is True:
    primary = primary + (" | MONTHLY TREND GATE: SPX above 10M EMA (~%.0f) — INITIATE SHORT blocked" % spx_10mema)
elif spx_above_10mema is False:
    primary = primary + " | MONTHLY TREND GATE: SPX below 10M EMA — gate open for INITIATE SHORT"
else:
    primary = primary + " | MONTHLY TREND GATE: 10M EMA unavailable — gate UNKNOWN, INITIATE SHORT blocked"
_prev_bd = int(CACHE.get("breadth_decay_streak") or 0)
# SESSION GUARD: decay streak counts trading sessions, not runs.
if breadth_red:
    if is_new_session(CACHE, "bd_session"):
        breadth_decay_streak = _prev_bd + 1
        mark_session(CACHE, "bd_session")
    else:
        breadth_decay_streak = max(_prev_bd, 1)
else:
    breadth_decay_streak = 0
CACHE.set("breadth_decay_streak", breadth_decay_streak, RUN_TS)
if breadth_decay_streak > 0:
    primary = primary + " | Breadth decay streak: %d session%s" % (
        breadth_decay_streak, "s" if breadth_decay_streak != 1 else "")
breadth_proxy_divergence = False
if _bp is not None:
    _bp_dir = _bp["direction"]
    _bp_streak = _bp["streak"]
    _bp_stale = _bp.get("stale", False)
    _bp_tag = " (last known)" if _bp_stale else ""
    primary = primary + " | breadth proxy (RSP/SPY) %s: %d session%s%s" % (
        _bp_dir, _bp_streak, "s" if _bp_streak != 1 else "", _bp_tag)
    if _spx_near_high and _bp_dir == "NARROWING" and not _bp_stale:
        breadth_proxy_divergence = True
        primary = primary + " | BREADTH DIVERGENCE (RSP/SPY) CONFIRMED"
        log.info("BREADTH DIVERGENCE (RSP/SPY) CONFIRMED: SPX near highs, ratio narrowing")
# ---- PRE-ALERT early-warning composite (issue #17) -------------------------
# Named state BELOW Layer 2: top-proximity divergence. Fires when breadth is
# narrowing persistently near the highs AND at least one vol input is on.
# Informational only — never gates INITIATE, never touches sizing.
PRE_ALERT_MIN_STREAK = 3
pre_alert = False
pre_alert_inputs = []
_pa_narrowing = bool(_bp is not None and not _bp.get("stale", False)
                     and _bp["direction"] == "NARROWING"
                     and _bp["streak"] >= PRE_ALERT_MIN_STREAK)
if vix9d_inversion: pre_alert_inputs.append("VIX9D inversion")
if ts_accelerating: pre_alert_inputs.append("TS flattening accel")
if _vix_backwardation: pre_alert_inputs.append("VIX backwardation")
if vol_expansion: pre_alert_inputs.append("vol expansion")
if _pa_narrowing and pre_alert_inputs and bool(_spx_near_high):
    pre_alert = True
    log.info("PRE-ALERT fired: narrowing %d sessions + [%s] + SPX near high",
             _bp["streak"], ", ".join(pre_alert_inputs))
pre_alert_txt = ""
if pre_alert:
    pre_alert_txt = ("PRE-ALERT — EARLY WARNING (top-proximity divergence): "
                     "RSP/SPY narrowing %d sessions + %s + SPX within 2%% of "
                     "52wk high" % (_bp["streak"], ", ".join(pre_alert_inputs)))
# NOTE 2026-07-03: the Layer-2 2-of-3 ENTRY-SIGNAL check moved into the
# VERDICT ENGINE below (after the tiles are built) so its caveats reference
# the FINAL primary verdict, not the pre-escalation placeholder.
p = []
_vol_txt = (" | vol %.2fx 20d" % vol_ratio) if vol_ratio is not None else ""
p.append(("1. Equities (S&P via SPY)",
          (("SPY %.2f (%+.2f%%)%s" % (spy_px, spy_chg, _vol_txt))
           if (spy_px is not None and spy_chg is not None)
           else (("SPY %.2f%s" % (spy_px, _vol_txt)) if spy_px is not None else "unavailable")),
          "amber" if spy_px is not None else "gray"))
p.append(("2. Volatility (VIX)",
          ("%.1f" % vix_px) if vix_px is not None else "unavailable",
          "amber" if vix_px is not None else "gray"))
p.append(("3. Rates / yield curve",
          ("2s10s %+d bps" % spread_bps) if spread_bps is not None else "unavailable",
          ("green" if (spread_bps is not None and spread_bps >= 0)
           else ("red" if spread_bps is not None else "gray"))))
p.append(("4. Credit spreads", credit_sub, credit_col))
p.append(("5. Commodities (Gold)",
          ("Gold $%s" % fmt_money(gold_px)) if gold_px is not None else "unavailable",
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
DEAD = {"unavailable", "no data", "parse error"}
TILES_WITH_DATA = sum(1 for _, sub, _c in p if str(sub).strip().lower() not in DEAD)
log.info("tiles populated: %d/%d", TILES_WITH_DATA, TOTAL_TILES)
# ===========================================================================
# VERDICT ENGINE (added 2026-07-03)
# The old build could NEVER escalate: initiate_short started False and no
# code path set it True; "Day 1 of 3" was hardcoded. This engine:
#   1. persists the dual-red (breadth + net-liquidity) streak per SESSION
#   2. fires INITIATE SHORT only when EVERY rule is satisfied (fail-closed:
#      unknown data blocks; it can never fire on missing inputs)
#   3. lists every blocker explicitly so the email shows exactly what's missing
#   4. applies the MacroSage sizing ladder + manual catalyst/post-loss flags
# ===========================================================================
n_red = sum(1 for _, _, c in p if c == "red")
n_amber = sum(1 for _, _, c in p if c == "amber")  # DEAD (flagged 2026-07-06): unused here; build_html recomputes its own. Kept per no-delete rule.
tile11_red = p[10][2] == "red"   # Sector rotation (Pt11)
tile16_red = p[15][2] == "red"   # AAII Sentiment  (Pt16)
# ---- 1. dual-red streak (session-guarded, persisted) ----
dual_red = bool(breadth_red and netliq_decl)
_prev_streak = int(CACHE.get("dual_red_streak") or 0)
if dual_red:
    if is_new_session(CACHE, "verdict_session"):
        dual_red_streak = _prev_streak + 1
        mark_session(CACHE, "verdict_session")
    else:
        dual_red_streak = max(_prev_streak, 1)
else:
    dual_red_streak = 0
CACHE.set("dual_red_streak", dual_red_streak, RUN_TS)
log.info("dual-red streak: %d session(s) (breadth_red=%s, netliq_decl=%s)",
         dual_red_streak, breadth_red, netliq_decl)
# ---- 2. gate evaluation (every gate must be POSITIVELY confirmed) ----
_g_streak = dual_red_streak >= 3
_g_200 = spx_above_200dma is False          # SPX confirmed BELOW 200DMA
_g_10m = spx_above_10mema is False          # SPX confirmed BELOW 10M EMA
_g_vol = vol_ratio is not None and vol_ratio >= 1.2
_g_rr = rr_value is not None and rr_value >= 5.0
# NOTE 2026-07-06: FOMC gate is deliberately fail-OPEN. Unlike the data gates,
# an empty calendar is the NORMAL result (no FOMC most weeks) = safe to trade.
# Fail-closing here would let a flaky calendar API permanently suppress INITIATE.
_g_fomc = not (fomc_days is not None and fomc_days <= 2)  # event-risk block
blockers = []
if not _g_streak:
    blockers.append("dual-red streak %d/3" % dual_red_streak)
if not _g_200:
    blockers.append("SPX above 200DMA" if spx_above_200dma else "200DMA gate unknown")
if not _g_10m:
    blockers.append("SPX above 10M EMA" if spx_above_10mema else "10M-EMA gate unknown")
if not _g_vol:
    blockers.append(("volume %.2fx < 1.2x" % vol_ratio) if vol_ratio is not None
                    else "volume unknown")
if not _g_rr:
    blockers.append(("R:R %.1f < 5.0" % rr_value) if rr_value is not None
                    else "R:R unknown")
if not _g_fomc:
    blockers.append("FOMC within 2 days")
initiate_short = not blockers
# ---- 3. sizing ladder ----
if n_red >= 16:
    size_mult, size_txt = 2.0, "2.0x (16-19 red)"
elif n_red >= 12:
    size_mult, size_txt = 1.5, "1.5x (12-15 red)"
elif n_red >= 8:
    size_mult, size_txt = 1.0, "standard (8-11 red)"
else:
    size_mult, size_txt = 0.5, "probe only (<8 red)"
# Catalyst auto-confirm (keyless): the down-move is objectively underway when
# SPY prints a fresh 20-day low AND breakdown volume >=1.2x the 20d average
# (the volume gate). Replaces the manual-only flag; env/state still force it on.
_break_low = bool(spy_px is not None and _spy_hist and len(_spy_hist) >= 20
                  and spy_px <= min(_spy_hist[-20:]))
_break_vol = bool(vol_ratio is not None and vol_ratio >= 1.2)
catalyst_auto = bool(_break_low and _break_vol)
catalyst_on = ((cfg("CATALYST_ON") == "1") or bool(CACHE.get("catalyst_on"))
               or catalyst_auto)
if catalyst_auto:
    log.info("catalyst auto-confirmed: SPY fresh 20d low + volume %.2fx breakdown", vol_ratio)
post_loss = (cfg("POST_LOSS_DESIZE") == "1") or bool(CACHE.get("post_loss_desize"))
size_notes = []
if not catalyst_on:
    size_mult *= 0.5
    size_notes.append("halved: no active catalyst — need SPY fresh 20d low + >=1.2x volume, or CATALYST_ON=1")
if post_loss:
    size_mult *= 0.5
    size_notes.append("halved: post-loss de-sizing active (clears after a profitable exit)")
max_conviction = initiate_short and tile11_red and tile16_red
# ---- 4. rebuild the PRIMARY verdict head, keeping the appended gate notes ----
_split = primary.split(" | ", 1)
_notes_tail = (" | " + _split[1]) if len(_split) > 1 else ""
if initiate_short:
    head = ("INITIATE SHORT — dual-red streak %d/3 confirmed; SPX below 200DMA & 10M EMA; "
            "volume %.2fx (>=1.2x); R:R %.1f (stop %.2f / target %.2f); FOMC clear. "
            "Size: %.2fx [%s]%s%s. Exit rule: 2%% adverse within 3 sessions = full exit, "
            "no averaging down." % (
                dual_red_streak, vol_ratio, rr_value, rr_stop, rr_target,
                size_mult, size_txt,
                ("; " + "; ".join(size_notes)) if size_notes else "",
                " | MAX CONVICTION (Pt11+Pt16 dual red)" if max_conviction else ""))
    log.info("*** INITIATE SHORT FIRED *** size %.2fx", size_mult)
else:
    if dual_red:
        head = "WATCHING - Day %d of 3 (dual-red active)" % min(dual_red_streak, 3)
    else:
        head = "WATCHING - dual-red not active (breadth %s, net liquidity %s)" % (
            "RED" if breadth_red else ("UNKNOWN" if breadth is None else "ok"),
            "declining" if netliq_decl else ("UNKNOWN" if netliq_dir is None else "ok"))
    head += " | INITIATE blocked by: " + "; ".join(blockers)
primary = head + _notes_tail
# ---- 5. Layer-2 2-of-3 ENTRY SIGNAL (moved here so caveats see the final verdict) ----
_l2_signals = sum([gamma_flip, _vix_backwardation, mcclellan_divergence])
_cal_clear = not any(x in cal_sub for x in ("in 0d", "in 1d", "in 2d"))
if _l2_signals >= 2 and _cal_clear:
    _l2_names = []
    if gamma_flip: _l2_names.append(
        "vol expansion" if vol_expansion else
        ("VIX9D inversion" if vix9d_inversion else "GEX flip (manual)"))
    if _vix_backwardation: _l2_names.append("VIX backwardation")
    if mcclellan_divergence: _l2_names.append("McClellan divergence")
    _why_low = []
    if not initiate_short: _why_low.append("PRIMARY still WATCHING")
    if spx_above_200dma: _why_low.append("SPX above 200DMA")
    _conv_note = "probe size" if _why_low else "standard"
    layer2 = ("ENTRY SIGNAL - early/low-conviction (%s) [%s%s]" % (
        _conv_note,
        ", ".join(_l2_names),
        ("; caveat: " + "; ".join(_why_low)) if _why_low else ""))
    log.info("Layer2 ENTRY signal fired: %s", layer2)
# ---- Plain-English "what to do" summary (deterministic; reads existing verdict vars only) ----
_ll = (layer2 or "")
if initiate_short:
    layman = ("Green light for a short (a bet the market falls). The trend has rolled over and the warning gauges agree, so the math says this is the setup to act on. Follow the size and exit rules in the verdict above.")
elif _ll.startswith("ENTRY SIGNAL"):
    layman = ("Small starter position. A couple of early-warning gauges have flipped while the trend is still up, so the math supports a tiny starter short (a small bet the market falls) only. Keep it small; this is not the full signal yet.")
elif primary and "WATCHING" in primary.upper():
    _t = (" The market is still in an uptrend, trading above its 200-day average (the long-term trend line), so the math says stay out for now." if spx_above_200dma is True else " The trend has not clearly rolled over yet, so the math says wait.")
    _d = ((" Breadth has been weakening for %d day(s), so keep half an eye on it." % dual_red_streak) if dual_red_streak > 0 else "")
    layman = ("Sit tight - no short (a bet the market falls) here." + _t + _d + " There is nothing to do today.")
else:
    layman = ("No clear read today. The dashboard did not produce a confident stance, so the safe move is to do nothing and wait for the next update.")
log.info("Plain-English summary: %s", layman)
now = eastern_now().strftime("%Y-%m-%d %H:%M ET")
today = ET_TODAY.strftime("%B %d, %Y")
stale_banner = ""
if IS_HOLIDAY:
    stale_banner = ("<div style=\"background:#faeeda;border-left:4px solid #ef9f27;"
                    "padding:8px 12px;margin-bottom:12px;font-size:12px;color:#633806;\">"
                    "US market holiday today - figures may reflect the prior session.</div>")
elif IS_WEEKEND:
    stale_banner = ("<div style=\"background:#f1efe8;border-left:4px solid #888780;"
                    "padding:8px 12px;margin-bottom:12px;font-size:12px;color:#444441;\">"
                    "US market closed (weekend) - figures reflect the prior session.</div>")
def build_html():
    PAGE    = "#f0f2f5"
    CARD    = "#ffffff"
    CARD2   = "#f8f9fa"
    BORDER  = "#e1e4e8"
    TEXT    = "#0d1117"
    SUB     = "#57606a"
    MUTED   = "#8b949e"
    GREEN   = "#1a7f37"
    AMBER   = "#9a6700"
    RED     = "#cf222e"
    GRAY    = "#6e7781"
    FONT    = "-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif"
    STEEL   = "#4a7fb5"  # header wordmark - mid-tone steel-blue, legible in light mode and under Gmail's dark-mode inversion
    sig_color = {"green": GREEN, "amber": AMBER, "red": RED, "gray": GRAY}
    # DEAD (flagged 2026-07-06, kept per no-delete rule): sig_label is never
    # read — the tile cards use colour only, not these text labels.
    sig_label = {"green": "Bullish", "amber": "Watch", "red": "Bearish", "gray": "Neutral"}
    def esc(s):
        return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    pv = (primary or "").upper()
    if initiate_short:
        b_txt, b_bg, b_fg, b_bdr = "CRASH ALERT", "#ffebe9", RED, RED
    elif "WATCH" in pv:
        b_txt, b_bg, b_fg, b_bdr = "WATCHING", "#fff8c5", AMBER, AMBER
    else:
        b_txt, b_bg, b_fg, b_bdr = "STAND DOWN", "#dafbe1", GREEN, GREEN
    spx_str = fmt_money(spx_proxy) if spx_proxy else "n/a"
    day_chg = ("%+.2f%%" % spy_chg) if spy_chg is not None else ""
    day_col = GREEN if (spy_chg or 0) >= 0 else RED
    vs200   = (("%+.1f%% vs 200d" % ((spx_proxy/(spx_200dma*10)-1)*100)) if spx_proxy and spx_200dma else "")
    vs50    = (("%+.1f%% vs 50d"  % ((spx_proxy/(spx_50dma*10)-1)*100))  if spx_proxy and spx_50dma  else "")
    if spx_above_200dma is True:
        gate_note = ("SPX above 200MA (~%.0f) - short conviction caution." % (spx_200dma*10)) if spx_200dma else "SPX above 200MA - short conviction caution."
    elif spx_above_200dma is False:
        gate_note = "SPX below 200MA - gate open."
    else:
        gate_note = "200DMA gate status unavailable."
    n_red   = sum(1 for _,_,c in p if c == "red")
    n_amber = sum(1 for _,_,c in p if c == "amber")
    n_green = sum(1 for _,_,c in p if c == "green")
    def metric_card(title, sub, ckey):
        import re as _re
        col     = sig_color.get(ckey, GRAY)
        title_c = _re.sub(r'^\d+\.\s*', '', esc(title))
        sub_s   = esc(sub) if sub else "&mdash;"
        return (
            '<td width="50%%" style="padding:3px;vertical-align:top;">'
            '<table width="100%%" cellpadding="0" cellspacing="0" border="0"'
            ' bgcolor="%s" style="background:%s;border:1px solid %s;'
            'border-left:3px solid %s;border-radius:5px;">'
            '<tr><td style="padding:7px 9px 6px 9px;">'
            '<div style="font-family:%s;font-size:10px;font-weight:700;color:%s;'
            'text-transform:uppercase;letter-spacing:0.2px;line-height:1.2;">%s</div>'
            '<div style="font-family:%s;font-size:11px;color:%s;'
            'margin-top:3px;line-height:1.3;">%s</div>'
            '</td></tr></table></td>'
        ) % (CARD, CARD, BORDER, col, FONT, col, title_c, FONT, SUB, sub_s)
    grid_rows = ""
    for i in range(0, len(p), 2):
        lt, ls, lc = p[i]
        if i+1 < len(p):
            rt, rs, rc = p[i+1]
            rc_cell = metric_card(rt, rs, rc)
        else:
            rc_cell = '<td width="50%%" style="padding:3px;"></td>'
        grid_rows += '<tr>%s%s</tr>' % (metric_card(lt, ls, lc), rc_cell)
    out  = '<!DOCTYPE html>'
    out += ('<html><head><meta charset="UTF-8">'
            '<meta name="color-scheme" content="light">'
            '<meta name="supported-color-schemes" content="light">'
            '</head><body style="margin:0;padding:0;background:%s;">'
            '<table width="100%%" cellpadding="0" cellspacing="0" border="0"'
            ' bgcolor="%s" style="background:%s;">'
            '<tr><td align="center" style="padding:20px 8px;">') % (PAGE, PAGE, PAGE)
    out += ('<table width="480" cellpadding="0" cellspacing="0" border="0"'
            ' bgcolor="%s" style="max-width:480px;width:100%%;background:%s;'
            'border:1px solid %s;border-radius:10px;">') % (CARD2, CARD2, BORDER)
    out += ('<tr><td bgcolor="%s" style="background:%s;padding:13px 16px;'
            'border-radius:10px 10px 0 0;border-bottom:1px solid %s;">'
            '<table width="100%%" cellpadding="0" cellspacing="0" border="0"><tr>'
            '<td style="font-family:%s;">'
            '<div style="font-size:15px;font-weight:700;letter-spacing:0.5px;color:%s;">MACROSAGE</div>'
            '<div style="font-size:9px;font-weight:600;letter-spacing:0.5px;color:%s;margin-top:2px;">MARKET CRASH MONITOR</div>'
            '</td>'
            '<td align="right" style="font-family:%s;font-size:10px;color:%s;">%s</td>'
            '</tr></table></td></tr>') % (CARD, CARD, BORDER, FONT, STEEL, MUTED, FONT, MUTED, esc(today))
    if stale_banner:
        out += f'<tr><td style="padding:0 16px 12px 16px;">{stale_banner}</td></tr>'
    out += ('<tr><td bgcolor="%s" style="background:%s;padding:11px 16px;'
            'border-bottom:1px solid %s;">'
            '<table width="100%%" cellpadding="0" cellspacing="0" border="0"><tr>'
            '<td style="vertical-align:middle;">'
            '<span style="font-family:%s;font-size:20px;font-weight:700;color:%s;">%s</span>'
            '&nbsp;&nbsp;'
            '<span style="font-family:%s;font-size:12px;font-weight:600;color:%s;">%s</span>'
            '<div style="font-family:%s;font-size:9px;color:%s;margin-top:2px;">%s%s%s</div>'
            '</td>'
            '<td align="right" style="vertical-align:middle;">'
            '<span style="display:inline-block;background:%s;color:%s;border:1px solid %s;'
            'font-family:%s;font-size:10px;font-weight:700;letter-spacing:0.5px;'
            'text-transform:uppercase;padding:4px 10px;border-radius:4px;">%s</span>'
            '</td></tr></table></td></tr>') % (
        CARD, CARD, BORDER,
        FONT, TEXT, esc(spx_str),
        FONT, day_col, esc(day_chg),
        FONT, MUTED, esc(vs200), ("  " if vs200 and vs50 else ""), esc(vs50),
        b_bg, b_fg, b_bdr, FONT, b_txt)
    out += ('<tr><td bgcolor="%s" style="background:%s;padding:10px 16px;'
            'border-bottom:1px solid %s;">'
            '<table width="100%%" cellpadding="0" cellspacing="0" border="0"><tr>'
            '<td width="50%%" style="vertical-align:top;padding-right:12px;">'
            '<div style="font-family:%s;font-size:9px;font-weight:700;color:%s;'
            'text-transform:uppercase;letter-spacing:0.7px;margin-bottom:2px;">Primary verdict</div>'
            '<div style="font-family:%s;font-size:11px;color:%s;line-height:1.35;">%s</div>'
            '</td>'
            '<td width="50%%" style="vertical-align:top;padding-left:12px;'
            'border-left:1px solid %s;">'
            '<div style="font-family:%s;font-size:9px;font-weight:700;color:%s;'
            'text-transform:uppercase;letter-spacing:0.7px;margin-bottom:2px;">Layer 2 verdict</div>'
            '<div style="font-family:%s;font-size:11px;color:%s;line-height:1.35;">%s</div>'
            '</td></tr>'
            '<tr><td colspan="2" style="padding-top:5px;">'
            '<div style="font-family:%s;font-size:12px;color:%s;line-height:1.45;font-weight:600;margin-bottom:4px;">%s</div>'
            '<div style="font-family:%s;font-size:9px;color:%s;font-style:italic;">%s</div>'
            '</td></tr></table></td></tr>') % (
        CARD, CARD, BORDER,
        FONT, MUTED, FONT, TEXT, esc(primary or "n/a"),
        BORDER, FONT, MUTED, FONT, TEXT, esc(layer2 or "n/a"),
        FONT, TEXT, esc(layman),
            FONT, MUTED, esc(gate_note))
    if pre_alert:
        out += ('<tr><td bgcolor="%s" style="background:%s;padding:0 16px 10px;'
                'border-bottom:1px solid %s;">'
                '<div style="background:#fff8c5;border:1px solid %s;'
                'border-left:4px solid %s;padding:7px 10px;border-radius:4px;'
                'font-family:%s;font-size:11px;font-weight:700;color:%s;">%s'
                '</div></td></tr>') % (
            CARD, CARD, BORDER, AMBER, AMBER, FONT, AMBER, esc(pre_alert_txt))
    out += ('<tr><td bgcolor="%s" style="background:%s;padding:7px 16px 3px;">'
            '<span style="font-family:%s;font-size:9px;font-weight:700;color:%s;'
            'text-transform:uppercase;letter-spacing:0.8px;">Indicators</span>'
            '</td></tr>') % (CARD2, CARD2, FONT, MUTED)
    out += ('<tr><td bgcolor="%s" style="background:%s;padding:4px 13px 10px;">'
            '<table width="100%%" cellpadding="0" cellspacing="0" border="0">%s</table>'
            '</td></tr>') % (CARD2, CARD2, grid_rows)
    out += ('<tr><td bgcolor="%s" style="background:%s;padding:10px 16px;'
            'border-top:1px solid %s;text-align:center;">'
            '<table cellpadding="0" cellspacing="0" border="0" align="center"><tr>'
            '<td style="padding:0 16px;font-family:%s;text-align:center;">'
            '<div style="font-size:22px;font-weight:700;color:%s;line-height:1;">%d</div>'
            '<div style="font-size:9px;color:%s;text-transform:uppercase;letter-spacing:0.5px;">Bearish</div>'
            '</td>'
            '<td style="padding:0 16px;font-family:%s;text-align:center;'
            'border-left:1px solid %s;">'
            '<div style="font-size:22px;font-weight:700;color:%s;line-height:1;">%d</div>'
            '<div style="font-size:9px;color:%s;text-transform:uppercase;letter-spacing:0.5px;">Watch</div>'
            '</td>'
            '<td style="padding:0 16px;font-family:%s;text-align:center;'
            'border-left:1px solid %s;">'
            '<div style="font-size:22px;font-weight:700;color:%s;line-height:1;">%d</div>'
            '<div style="font-size:9px;color:%s;text-transform:uppercase;letter-spacing:0.5px;">Neutral</div>'
            '</td>'
            '</tr></table></td></tr>') % (
        CARD, CARD, BORDER,
        FONT, RED, n_red, RED,
        FONT, BORDER, AMBER, n_amber, AMBER,
        FONT, BORDER, GREEN, n_green, GREEN)
    out += ('<tr><td bgcolor="%s" style="background:%s;padding:9px 16px;'
            'border-top:1px solid %s;border-radius:0 0 10px 10px;text-align:center;">'
            '<div style="font-family:%s;font-size:9px;color:%s;line-height:1.6;">'
            '%d / %d indicators retrieved &bull; %s<br>'
            'Research &amp; educational only &#8212; not investment advice'
            '</div></td></tr>') % (
        CARD2, CARD2, BORDER, FONT, MUTED,
        TILES_WITH_DATA, TOTAL_TILES, esc(now))
    _lg_hdr = "font-family:%s;font-size:11px;font-weight:700;color:%s;text-transform:uppercase;letter-spacing:0.4px;padding:2px 0 5px 0;" % (FONT, SUB)
    _lg_row = "font-family:%s;font-size:10px;color:%s;line-height:1.45;padding:1px 0;" % (FONT, MUTED)
    _lg_tag = "font-weight:700;color:%s;"
    def _lgm(tag_col, name, body):
        return ('<div style="%s"><span style="%s">%s</span> &mdash; %s</div>'
                % (_lg_row, (_lg_tag % tag_col), esc(name), esc(body)))
    _legend = (
        '<tr><td bgcolor="%s" style="background:%s;padding:12px 16px 14px 16px;'
        'border-top:1px solid %s;">' % (CARD2, CARD2, BORDER)
        + '<div style="font-family:%s;font-size:11px;font-weight:700;color:%s;'
          'letter-spacing:0.3px;padding-bottom:2px;">'
          '&#9432; Why these metrics'
          '<span style="font-weight:400;color:%s;"> &mdash; see foot</span></div>'
          % (FONT, TEXT, MUTED)
        + '<div style="font-family:%s;font-size:9px;color:%s;padding-bottom:9px;">'
          'Rules-based signal (not a weighted average). Each line: what it signals '
          '&amp; how it is used.</div>' % (FONT, MUTED)
        + ('<div style="%s">Gates &mdash; ALL must be open before INITIATE SHORT can fire</div>' % _lg_hdr)
        + _lgm(RED, "Dual-red streak",
               "Breadth <50% AND net liquidity declining for 3 consecutive trading sessions (session-guarded; weekend runs cannot inflate it). The core PRIMARY trigger.")
        + _lgm(RED, "200DMA gate",
               "SPX vs its 200-day MA. Above = long-term uptrend intact, INITIATE blocked; below = structural short regime valid.")
        + _lgm(RED, "Monthly-trend gate",
               "SPX vs its 10-month EMA (monthly SPY closes). INITIATE only fires when SPX is BELOW; above or unknown blocks it.")
        + _lgm(RED, "Volume gate",
               "Breakdown-session SPY volume must be >= 1.2x the 20-day average; sub-average or unknown volume = WAIT.")
        + _lgm(RED, "R:R gate",
               "Reward:risk >= 5.0 required. Stop = 5-day high (override RR_STOP); target = measured-move projection (override RR_TARGET). Unknown = blocked.")
        + _lgm(RED, "Calendar gate (tile 12)",
               "FOMC within 2 days blocks INITIATE. The 10-day OpEx Structural Transition Window is flagged as CALENDAR GATE — TRANSITION WINDOW.")
        + ('<div style="%s">Layer-2 entry inputs &mdash; the 2-of-3 ENTRY SIGNAL set</div>' % _lg_hdr)
        + _lgm(AMBER, "VIX term structure (tile 17)",
               "VIX / VIX3M. Backwardation = near-term fear > forward fear, a stress tell. 1 of 3 entry inputs.")
        + _lgm(AMBER, "McClellan / NYMO divergence (tile 14)",
               "NYMO negative WHILE SPX sits near its 52-week high = price/breadth divergence. 1 of 3 entry inputs.")
        + _lgm(AMBER, "Realized-vol expansion (Layer-2)",
               "5-day realized vol accelerating >=1.30x its 20-day baseline (from SPY closes, keyless) = vol-regime expansion — the tell the old GEX flip proxied. Manual GEX_FLIP=1 or a VIX9D/VIX front-of-curve inversion also lights this input. 1 of 3 entry inputs.")
        + _lgm(AMBER, "VIX9D/VIX inversion (Layer-2)",
               "9-day implied vol above 30-day (VIX9D/VIX >= 1.0) = stress at the FRONT of the curve, the earliest term-structure tell (Yahoo ^VIX9D, keyless). Feeds the same Layer-2 vol-regime input as vol expansion.")
        + _lgm(AMBER, "Term-structure velocity (early warning)",
               "5-session rate of change of VIX/VIX3M. Flattening >= +0.08/5d with the ratio already >= 0.95 = curve racing toward inversion. PRE-ALERT input only - not in the Layer-2 2-of-3 set.")
        + _lgm(AMBER, "PRE-ALERT composite (early warning)",
               "RSP/SPY narrowing >= 3 sessions + any one vol input (VIX9D inversion, TS velocity, backwardation, vol expansion) + SPX within 2% of its 52wk high. Named early-warning state below Layer 2 - informational only, never gates or sizes.")
        + ('<div style="%s">Breadth / divergence</div>' % _lg_hdr)
        + _lgm(GREEN, "Market breadth (tile 7)",
               "% of names advancing (FMP sectors, WSJ NYSE A/D fallback). Below 50% = one of the two dual-red triggers.")
        + _lgm(GREEN, "Net liquidity (tile 8)",
               "Fed balance sheet minus TGA/RRP direction. Declining = the co-equal dual-red trigger paired with breadth.")
        + _lgm(GREEN, "Breadth proxy RSP/SPY (tile 18)",
               "Equal-weight vs cap-weight direction. Narrowing while SPX near highs confirms a breadth divergence.")
        + _lgm(GREEN, "Breadth-decay streak",
               "Consecutive TRADING SESSIONS of red breadth (session-guarded). Persistence context for the PRIMARY verdict.")
        + ('<div style="%s">Sizing &amp; risk</div>' % _lg_hdr)
        + _lgm(AMBER, "Signal-strength sizing",
               "8-11 red tiles = standard, 12-15 = 1.5x, 16-19 = 2x (cap). MAX CONVICTION requires Pt11 (sector rotation) AND Pt16 (AAII) both red.")
        + _lgm(AMBER, "Catalyst / post-loss flags",
               "Catalyst auto-confirms when SPY prints a fresh 20-day low AND breakdown volume >=1.2x (else size halves; CATALYST_ON=1 still forces it). Size halves again while POST_LOSS_DESIZE=1 after a 2% exit trigger.")
        + _lgm(AMBER, "Exit rule",
               "2% adverse within 3 sessions = full exit, no averaging down.")
        + ('<div style="%s">Informational / tally &mdash; context + colour count, not gating</div>' % _lg_hdr)
        + _lgm(MUTED, "Volatility / VIX (tile 2)", "Level of implied vol - overall risk temperature.")
        + _lgm(MUTED, "Rates / 2s10s (tile 3)", "Yield-curve slope; inversion is a recession/risk tell.")
        + _lgm(MUTED, "Credit spreads (tile 4)", "Stress in corporate credit - widening = risk-off.")
        + _lgm(MUTED, "Commodities / Gold (tile 5)", "Safe-haven / real-asset context.")
        + _lgm(MUTED, "Dollar / FX (tile 6)", "USD strength; a rising dollar tightens global conditions.")
        + _lgm(MUTED, "Positioning / COT (tile 9)", "Futures positioning of large traders - crowding context.")
        + _lgm(MUTED, "VVIX divergence (tile 10)", "Vol-of-vol vs VIX - hedging-demand context.")
        + _lgm(MUTED, "Sector rotation (tile 11)", "Defensive vs broad leadership (derived from breadth).")
        + _lgm(MUTED, "Fiscal impulse (tile 13)", "Point-19 MTS spec: deficit + outlays YoY + interest/receipts.")
        + _lgm(MUTED, "NAAIM exposure (tile 15)", "Active-manager equity exposure - sentiment/positioning.")
        + _lgm(MUTED, "AAII sentiment (tile 16)", "Retail bull/bear survey - contrarian sentiment context.")
        + '</td></tr>'
    )
    out += _legend
    out += '</table>'
    out += '</td></tr></table>'
    out += '</body></html>'
    return out
final_signal = primary or "No verdict"
spx_card = fmt_money(spx_proxy) if spx_proxy else "n/a"
vix_card = ("%.1f" % vix_px) if vix_px is not None else "n/a"
sp_card  = ("%+d bps" % spread_bps) if spread_bps is not None else "n/a"
br_card  = ("%d%%" % breadth) if breadth is not None else "n/a"
html = build_html()
plain = ("MacroSage SHORT signal - %s\nPRIMARY VERDICT: %s\nLAYER 2 VERDICT: %s\nWHAT TO DO: %s\n%s\n%s\n\n"
         "%d of %d indicators retrieved. Research/educational only - not investment advice.\n"
         % (now, primary, layer2, layman,
            (pre_alert_txt + "\n") if pre_alert else "",
            final_signal, TILES_WITH_DATA, TOTAL_TILES))
try:
    rdir = os.path.join(HERE, "reports")
    os.makedirs(rdir, exist_ok=True)
    stamp = eastern_now().strftime("%Y-%m-%d_%H%M")
    with open(os.path.join(rdir, "short_%s.html" % stamp), "w", encoding="utf-8") as fh:
        fh.write(html)
except Exception as ex:
    log.warning("report save warning: %s", ex)
def send_email():
    user = cfg("GMAIL_USER")
    pw = cfg("GMAIL_APP_PASSWORD").replace(" ", "")
    if not user or not pw:
        log.error("EMAIL SKIPPED: missing GMAIL_USER / GMAIL_APP_PASSWORD secret")
        return False
    import glob as _glob
    from email.mime.base import MIMEBase
    from email import encoders as _encoders
    # Subject is neutral day-to-day; only an active CRASH ALERT escalates it,
    # so a calm/WATCHING day never looks alarming from the inbox.
    subject = "MacroSage — Daily Risk Report — %s" % today
    if initiate_short:
        subject = "⚠ CRASH ALERT — " + subject
    if IS_HOLIDAY:
        subject += " [US holiday]"
    _alt = MIMEMultipart("alternative")
    _alt.attach(MIMEText(plain, "plain", "utf-8"))
    _alt.attach(MIMEText(html, "html", "utf-8"))
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(DEFAULT_RECIPIENTS)
    _cc = [a for a in RECIPIENTS if a not in DEFAULT_RECIPIENTS]
    if _cc:
        msg["Cc"] = ", ".join(_cc)
     
    msg.attach(_alt)
    try:
        _rdir = os.path.join(HERE, "reports")
        _reports = sorted(_glob.glob(os.path.join(_rdir, "short_*.html")))
        if _reports:
            _rpath = _reports[-1]
            with open(_rpath, "rb") as _rf:
                _part = MIMEBase("text", "html")
                _part.set_payload(_rf.read())
            _encoders.encode_base64(_part)
            _part.add_header("Content-Disposition", "attachment",
                             filename=os.path.basename(_rpath))
            msg.attach(_part)
            log.info("attached interactive report: %s", os.path.basename(_rpath))
        else:
            log.warning("no interactive report found in %s; sending email without attachment", _rdir)
    except Exception as _aex:
        log.warning("report attach failed (%s); sending email without attachment", _aex)
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
    CACHE.save()
    log.info("Run complete - %d/%d signals retrieved, email sent: %s",
             TILES_WITH_DATA, TOTAL_TILES, "YES" if email_ok else "NO")

    # Dormant heartbeat ping (run-health-alerts). Off by default: only fires
    # once HEALTHCHECK_URL is configured (see the PR description for the
    # activation steps, which are NOT part of this change). Zero new
    # dependencies - plain urllib, already imported above - and wrapped in
    # try/except so a monitoring outage can never break the run.
    _hc_url = cfg("HEALTHCHECK_URL")
    if _hc_url:
        try:
            _hc_ping = _hc_url if email_ok else (_hc_url.rstrip("/") + "/fail")
            urllib.request.urlopen(_hc_ping, timeout=10)
            log.info("heartbeat ping sent: %s", "success" if email_ok else "fail")
        except Exception as _hc_ex:
            log.warning("heartbeat ping failed (non-fatal, run unaffected): %s", _hc_ex)

    # Exit code reflects the EMAIL outcome, not just data availability: the
    # graceful last-known-value fallback further up means a single dead data
    # source never fails the run, but a failed send is the one failure mode
    # this job exists to surface. Exit 1 on email failure so Actions marks
    # the run red and fires its built-in failure notification instead of
    # showing green on a run that never actually emailed anyone.
    sys.exit(0 if email_ok else 1)
