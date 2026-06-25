#!/usr/bin/env python3
"""
SHORT — Macro Dashboard (cloud / GitHub Actions edition)
========================================================
Keyless data (yfinance + FRED public CSV). Emails the dashboard via Gmail SMTP.
Runs unattended on GitHub Actions — your PC does not need to be on.

Required environment variables (set as GitHub repo Secrets):
  GMAIL_USER          e.g. wolfgangduke@gmail.com
  GMAIL_APP_PASSWORD  16-char Google App Password (NOT your normal password)
  MAIL_TO             comma-separated recipients

If email creds are absent it just prints the report (handy for local testing).
"""
import io, os, csv, ssl, json, smtplib, datetime as dt, urllib.request
from email.mime.text import MIMEText

SECTOR_ETFS = ["XLB","XLC","XLY","XLP","XLE","XLF","XLV","XLI","XLRE","XLK","XLU"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
FOMC_2026 = ["2026-01-28","2026-03-18","2026-04-29","2026-06-17",
             "2026-07-29","2026-09-16","2026-10-28","2026-12-09"]


def next_weekday(today, wd):
    return today + dt.timedelta(days=(wd - today.weekday() - 1) % 7 + 1)
def next_business_day(today):
    d = today + dt.timedelta(days=1)
    while d.weekday() >= 5: d += dt.timedelta(days=1)
    return d
def next_quarter_start(today):
    qm = ((today.month - 1)//3 + 1)*3 + 1
    y = today.year + (1 if qm > 12 else 0); qm = 1 if qm > 12 else qm
    return dt.date(y, qm, 1)
def due(d): return f"(next due {d:%a %d %b})"


def fred_csv(series, n=400):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    raw = urllib.request.urlopen(url, timeout=25).read().decode()   # no UA: FRED CDN dislikes it
    out = []
    for d, v in list(csv.reader(io.StringIO(raw)))[1:]:
        if v not in (".", ""):
            try: out.append((d, float(v)))
            except ValueError: pass
    return out[-n:]

def net_liquidity():
    walcl, tga, rrp = dict(fred_csv("WALCL")), dict(fred_csv("WTREGEN")), dict(fred_csv("RRPONTSYD"))
    s = [(d, w/1000.0 - tga[d]/1000.0 - rrp[d]) for d, w in sorted(walcl.items()) if d in tga and d in rrp]
    recent = s[-3:]
    return recent, (len(recent) == 3 and recent[0][1] > recent[1][1] > recent[2][1])

def treasury_2s10s():
    y2, y10 = fred_csv("DGS2")[-1][1], fred_csv("DGS10")[-1][1]
    return y2, y10, round((y10 - y2) * 100)

def hy_oas(): return fred_csv("BAMLH0A0HYM2")[-1]

def quotes():
    import yfinance as yf
    out = {}
    for s in ["^GSPC","^VIX","^VVIX","HG=F","GC=F"]:
        try:
            h = yf.download(s, period="5d", progress=False, threads=False)["Close"].dropna()
            out[s] = float(h.iloc[-1]) if len(h) else None
        except Exception:
            out[s] = None
    return out

def breadth_3day():
    import yfinance as yf
    px = yf.download(SECTOR_ETFS, period="7d", progress=False, threads=False)["Close"].dropna()
    pct = px.pct_change().dropna()
    days = [(str(dte.date()), int((row > 0).sum()), round(100*int((row > 0).sum())/len(SECTOR_ETFS)))
            for dte, row in pct.tail(3).iterrows()]
    return days, (len(days) == 3 and all(d[2] < 50 for d in days))

def fear_greed():
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15))
        fg = d["fear_and_greed"]; return round(float(fg["score"])), fg.get("rating")
    except Exception as e:
        return None, f"blocked ({getattr(e,'code',type(e).__name__)})"

def naaim():
    try:
        import re
        url = "https://naaim.org/programs/naaim-exposure-index/"
        h = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read().decode("utf-8","ignore")
        m = re.search(r'NAAIM (?:Number|Exposure Index)[^0-9\-]{0,40}(-?\d{1,3}\.\d{1,2})', h)
        return float(m.group(1)) if m else None
    except Exception:
        return None

def calendar_gate(today):
    fomc = next((d for d in FOMC_2026 if 0 <= (dt.date.fromisoformat(d) - today).days <= 5), None)
    first = today.replace(day=1)
    fri = [first + dt.timedelta(days=i) for i in range(31)
           if (first + dt.timedelta(days=i)).month == today.month and (first + dt.timedelta(days=i)).weekday() == 4]
    opex = fri[2]
    return fomc, opex, (opex if 0 <= (opex - today).days <= 3 else None)


def build_report():
    today = dt.date.today()
    q = quotes(); nl_recent, nl_decl = net_liquidity()
    y2, y10, spread = treasury_2s10s(); hy_d, hy_v = hy_oas()
    bdays, breadth_3x = breadth_3day(); fg_score, fg_rating = fear_greed(); naaim_v = naaim()
    fomc_hit, opex, opex_hit = calendar_gate(today)
    d_daily, d_netliq = next_business_day(today), next_weekday(today, 3)
    d_naaim, d_cot, d_struct = next_weekday(today, 2), next_weekday(today, 4), next_quarter_start(today)

    primary = "INITIATE SHORT" if (breadth_3x and nl_decl) else f"WATCHING — Day {sum(1 for d in bdays if d[2] < 50)} of 3"
    layer2 = "CALENDAR GATE" if (fomc_hit or opex_hit) else "WAIT"

    L = ["SHORT — MACRO DASHBOARD", f"{today:%d %b %Y}  (cloud build)", "",
         f"PRIMARY VERDICT: {primary}", f"LAYER 2 VERDICT: {layer2}", "", "--- LIVE (auto) ---",
         f"SPX:   {q['^GSPC']}    {due(d_daily)}",
         f"VIX:   {q['^VIX']}   VVIX: {q['^VVIX']}   {due(d_daily)}"]
    if q['HG=F'] and q['GC=F']:
        L.append(f"Copper/Gold: {q['HG=F']/q['GC=F']:.5f}  (Cu {q['HG=F']:.2f}/Au {q['GC=F']:.0f})  {due(d_daily)}")
    L += [f"HY OAS (credit): {hy_v:.2f}%  [{hy_d}]   {due(d_daily)}",
          f"2s10s: {spread} bps (2Y {y2}/10Y {y10}) {'INVERTED' if spread<0 else 'positive'}  {due(d_daily)}",
          "", f"Net Liquidity (WALCL-TGA-RRP, $bn)   {due(d_netliq)}"]
    L += [f"   {d}: {v:,.0f}" for d, v in nl_recent]
    L += [f"   declining 3 prints: {nl_decl}", "", f"Breadth (sector ETFs advancing)   {due(d_daily)}"]
    L += [f"   {d}: {adv}/11 = {pct}%" for d, adv, pct in bdays]
    L += [f"   <50% for 3 closes: {breadth_3x}", "", "--- SENTIMENT (scrape w/ fallback) ---"]
    L.append(f"Fear & Greed: {fg_score} ({fg_rating})   {due(d_daily)}" if fg_score is not None
             else f"Fear & Greed: manual [{fg_rating}] cnn.com/markets/fear-and-greed   {due(d_daily)}")
    L.append(f"NAAIM Exposure: {naaim_v}   {due(d_naaim)}" if naaim_v is not None
             else f"NAAIM Exposure: manual  naaim.org/programs/naaim-exposure-index/  {due(d_naaim)}")
    L += ["", "--- CALENDAR GATE ---", f"   FOMC within 5d: {fomc_hit or 'clear'}",
          f"   OpEx this month: {opex} (within 3d: {opex_hit or 'no'})",
          "", "--- MANUAL (no free feed) ---",
          f"   GEX gamma flip : spotgamma.com / squeezemetrics.com   {due(d_daily)}",
          f"   McClellan Osc  : mcoscillator.com/market_breadth/daily/   {due(d_daily)}",
          f"   COT (S&P futs) : cftc.gov/dea/options/deacmesf.htm   {due(d_cot)}",
          "", f"--- STRUCTURAL (quarterly context)   {due(d_struct)} ---",
          "   Private Debt/GDP, Credit Impulse, Maturity Wall, Policy Distortion"]
    return today, primary, layer2, "\n".join(L)


def send_email(subject, body):
    user, pw, to = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD"), os.environ.get("MAIL_TO")
    if not (user and pw and to):
        print("[no email creds — printing only]\n"); print(body); return
    recipients = [a.strip() for a in to.split(",") if a.strip()]
    msg = MIMEText(body); msg["Subject"] = subject; msg["From"] = user; msg["To"] = ", ".join(recipients)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pw); s.sendmail(user, recipients, msg.as_string())
    print(f"[sent] {subject} -> {recipients}")


if __name__ == "__main__":
    today, primary, layer2, report = build_report()
    send_email(f"SHORT — Macro Dashboard, {today:%d %b %Y}  [{primary} | {layer2}]", report)
