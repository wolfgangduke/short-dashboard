#!/usr/bin/env python3
"""SHORT macro dashboard -> colored HTML email.
Uses FMP /stable/ endpoints (Starter plan), FRED, Gmail SMTP. Keys from .env.
Rebuilt 2026-06-25: legacy /api/v3 endpoints were retired by FMP; this uses /stable/.
"""
import os, json, ssl, smtplib, urllib.request, urllib.error, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__))
RECIPIENTS = ["wolfgangduke@gmail.com", "richard.macrae.gordon@gmail.com"]

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
    return os.environ.get(k) or ENV.get(k, "")  # env vars (Actions secrets) take precedence
FMP = cfg("FMP_API_KEY").strip()
FRED = cfg("FRED_API_KEY").strip()

def get_json(url, timeout=15):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s" % e.code
    except Exception as e:
        return None, str(e)

def fmp(path):
    sep = "&" if "?" in path else "?"
    return get_json("https://financialmodelingprep.com/stable/%s%sapikey=%s" % (path, sep, FMP))

def fred_series(series, n=2):
    url = ("https://api.stlouisfed.org/fred/series/observations?series_id=%s"
           "&api_key=%s&file_type=json&sort_order=desc&limit=%d" % (series, FRED, n))
    d, _ = get_json(url)
    if d and d.get("observations"):
        out = []
        for o in d["observations"]:
            try:
                out.append(float(o["value"]))
            except (ValueError, KeyError):
                pass
        return out
    return None

# ---------- gather live data ----------
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

# sector breadth (may be gated on some plans)
sectors, _ = fmp("sector-performance-snapshot?date=%s" % datetime.date.today().isoformat())
if not sectors:
    sectors, _ = fmp("sector-performance-snapshot")

# net liquidity via FRED (WALCL millions; TGA + RRP billions)
netliq = None
walcl = fred_series("WALCL"); tga = fred_series("WTREGEN"); rrp = fred_series("RRPONTSYD")
if walcl and tga and rrp and len(walcl) >= 2 and tga and rrp:
    try:
        cur = walcl[0] / 1000.0 - tga[0] - rrp[0]
        prv = walcl[1] / 1000.0 - tga[min(1, len(tga)-1)] - rrp[min(1, len(rrp)-1)]
        netliq = (cur, prv)
    except Exception:
        netliq = None

# ---------- derive values ----------
def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

spy = D.get("spy", {})
spy_px = num(spy.get("price"))
spy_chg = num(spy.get("changePercentage"))
spx_proxy = spy_px * 10 if spy_px else None
vix_px = num(D.get("vix", {}).get("price"))
gold_px = num(D.get("gold", {}).get("price"))

t = D.get("treasury", {})
y2 = num(t.get("year2")); y10 = num(t.get("year10"))
spread_bps = round((y10 - y2) * 100) if (y2 is not None and y10 is not None) else None

rsi_val = None
r = D.get("rsi", {})
if isinstance(r, dict):
    rsi_val = num(r.get("rsi"))

def sector_chg(s):
    for f in ("changesPercentage", "averageChange", "changePercentage", "changePct"):
        if f in s:
            return num(s[f])
    return None

breadth = None; up = down = 0
if sectors and isinstance(sectors, list):
    for s in sectors:
        c = sector_chg(s)
        if c is None:
            continue
        if c >= 0: up += 1
        else: down += 1
    if up + down:
        breadth = round(up / (up + down) * 100)

netliq_dir = None
if netliq:
    netliq_dir = "declining" if netliq[0] < netliq[1] else "rising"

# ---------- verdicts ----------
breadth_red = breadth is not None and breadth < 50
netliq_decl = netliq_dir == "declining"
if breadth_red and netliq_decl:
    primary = "WATCHING - both triggers RED (confirm 3-day streak)"
else:
    primary = "WATCHING - Day 1 of 3"
layer2 = "WAIT"

# ---------- build 14 points ----------
PAL = {"red": ("#fcebeb", "#e24b4a", "#791f1f"),
       "amber": ("#faeeda", "#ef9f27", "#633806"),
       "green": ("#eaf3de", "#639922", "#27500a"),
       "gray": ("#f1efe8", "#888780", "#444441")}

def fmt_money(v):
    return "n/a" if v is None else ("{:,.0f}".format(v))

# ---------- extra feeds: credit, dollar, fiscal, sentiment, calendar ----------
hy = fred_series("BAMLH0A0HYM2", 2)
credit_sub, credit_col = "no data", "gray"
if hy and len(hy) >= 2:
    credit_sub = "HY OAS %.2f%% (%s)" % (hy[0], "widening" if hy[0] > hy[1] else "tightening")
    credit_col = "red" if hy[0] > hy[1] else "green"

dxy = fred_series("DTWEXBGS", 2)
usd_sub, usd_col = "no data", "gray"
if dxy and len(dxy) >= 2:
    usd_sub = "Broad $ %.1f (%s)" % (dxy[0], "rising" if dxy[0] > dxy[1] else "falling")
    usd_col = "red" if dxy[0] > dxy[1] else "green"

mts = fred_series("MTSDS133FMS", 12)
fisc_sub, fisc_col = "no data", "gray"
if mts and len(mts) >= 12:
    deficit = -sum(mts[:12]) / 1e6  # millions -> $T, deficit positive
    fisc_col = "red" if deficit > 2.0 else ("amber" if deficit > 1.5 else "green")
    fisc_sub = "12M deficit $%.2fT" % deficit

import calendar as _cal
def third_friday(y, m):
    fr = [d for d in _cal.Calendar().itermonthdates(y, m) if d.month == m and d.weekday() == 4]
    return fr[2]
_t = datetime.date.today()
opex_days = abs((third_friday(_t.year, _t.month) - _t).days)
fomc_days = None
ec, _ = fmp("economic-calendar?from=%s&to=%s" % (_t, _t + datetime.timedelta(days=10)))
if ec and isinstance(ec, list):
    for ev in ec:
        nm = (ev.get("event") or "").lower(); ct = (ev.get("country") or "")
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

p = []
p.append(("1. Equities (S&P via SPY)",
          ("SPY %.2f (%+.2f%%)" % (spy_px, spy_chg)) if spy_px is not None else "unavailable",
          "amber" if spy_px is not None else "gray"))
p.append(("2. Volatility (VIX)",
          ("%.1f" % vix_px) if vix_px is not None else "unavailable",
          "amber" if vix_px is not None else "gray"))
p.append(("3. Rates / yield curve",
          ("2s10s %+d bps" % spread_bps) if spread_bps is not None else "unavailable",
          ("green" if (spread_bps is not None and spread_bps >= 0) else ("red" if spread_bps is not None else "gray"))))
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
p.append(("9. Positioning (COT)", "Premium-gated", "gray"))
p.append(("10. VVIX divergence", "Premium-gated", "gray"))
p.append(("11. Sector rotation",
          ("defensive tilt" if breadth_red else "broad") if breadth is not None else "unavailable",
          ("red" if breadth_red else ("green" if breadth is not None else "gray"))))
p.append(("12. Calendar gate", cal_sub, cal_col))
p.append(("13. Fiscal impulse", fisc_sub, fisc_col))

# ---------- build HTML ----------
now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
today = datetime.date.today().strftime("%B %d, %Y")

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
    right = tile(*p[i+1]) if i + 1 < len(p) else '<td width="50%"></td>'
    rows += "<tr>" + left + right + "</tr>"

final_signal = ("No short tonight. " +
    ("Breadth is sub-50% with a defensive tilt - the one bearish tell - but it is a single session and the liquidity/positioning confirms are not aligned. "
     if breadth_red else
     "Breadth is holding above 50% and the curve is not inverted, so there is no edge to press here. ") +
    "Stay flat; watch breadth and net liquidity.")

html = (
'<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;color:#2c2c2a;">'
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
'<span style="color:#888780;">&#9632;</span> unavailable</div></div>')

plain = ("MacroSage SHORT signal - %s\nPRIMARY VERDICT: %s\nLAYER 2 VERDICT: %s\n\n%s\n"
         % (now, primary, layer2, final_signal))

# save a timestamped report
try:
    rdir = os.path.join(HERE, "reports")
    os.makedirs(rdir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    with open(os.path.join(rdir, "short_%s.html" % stamp), "w", encoding="utf-8") as fh:
        fh.write(html)
except Exception as ex:
    print("report save warning:", ex)

# ---------- email ----------
def send_email():
    user = cfg("GMAIL_USER")
    pw = cfg("GMAIL_APP_PASSWORD").replace(" ", "")
    if not user or not pw:
        print("EMAIL SKIPPED: missing GMAIL_USER / GMAIL_APP_PASSWORD")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "SHORT Signal - %s Post-Market (graphic)" % today
    msg["From"] = user
    msg["To"] = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(user, pw)
        s.sendmail(user, RECIPIENTS, msg.as_string())
    return True

if __name__ == "__main__":
    print("=== SHORT dashboard ===", now)
    print("SPX(SPYx10):", spx_card, "| VIX:", vix_card, "| 2s10s:", sp_card, "| Breadth:", br_card)
    print("PRIMARY:", primary, "| LAYER2:", layer2)
    try:
        ok = send_email()
        print("EMAIL SENT to %s" % ", ".join(RECIPIENTS) if ok else "EMAIL NOT SENT")
    except Exception as ex:
        print("EMAIL ERROR:", ex)
