# SHORT dashboard — how it runs and how to keep it healthy

Plain-English guide. You do **not** need to be a programmer to follow this.

## What it does

Every US trading day, after the New York market closes, GitHub runs a Python
script that pulls live market data, scores 13 indicators, and emails a
colour-coded dashboard to:

- wolfgangduke@gmail.com
- richard.macrae.gordon@gmail.com

It runs in the cloud on GitHub's servers (GitHub Actions) — your PC does not
need to be on.

## When it runs

- **Schedule:** 22:00 UTC, Monday–Friday.
  - That is just after the 4:00pm New York close (5–6pm New York time).
  - In Perth that is about **6:00am the next morning** (Tue–Sat AWST).
- You can also run it any time by hand: open the repo on GitHub →
  **Actions** tab → **MacroSage SHORT dashboard** → **Run workflow**.

GitHub's scheduler always uses UTC and can be a few minutes late at busy times.
It does **not** know about US public holidays, so on a US market holiday the
script still runs but adds a small "US market holiday" note to the email,
because the figures will be from the previous session.

## The 5 GitHub Secrets it uses

These are already set up in this repo (Settings → Secrets and variables →
Actions). You only need to touch them if something changes (e.g. a new
password). **Names must match exactly:**

| Secret name           | What it is                                            |
|-----------------------|-------------------------------------------------------|
| `FMP_API_KEY`         | Financial Modeling Prep API key                       |
| `FRED`                | FRED (St. Louis Fed) API key                          |
| `GMAIL_USER`          | The Gmail address that sends the email                 |
| `GMAIL_APP_PASSWORD`  | A Gmail **App Password** (see below) — NOT your normal password |
| `MAIL_TO`             | Optional extra recipients (the two above are always included) |

### Important about the Gmail password

`GMAIL_APP_PASSWORD` must be a Google **App Password**, not your normal Gmail
password. A normal password will be rejected and the email will fail.

To create one:
1. Turn on 2-Step Verification on the sending Google account.
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password (any name, e.g. "short dashboard").
4. Copy the 16-character code and paste it as the `GMAIL_APP_PASSWORD` secret.
   Spaces don't matter — the script strips them.

### One thing worth fixing

The `FRED` secret currently has a stray space at the front of its value. The
script now trims spaces automatically so it still works, but it is cleaner to
re-paste the FRED key with no leading space:
Settings → Secrets and variables → Actions → `FRED` → Update.

## How to tell if a run worked

Open the repo on GitHub → **Actions** tab. Each run is a row:

- **Green tick** = ran fine.
- **Red cross** = something failed; click in to read the log.

The log is written in plain steps. The **last line** always summarises the run:

```
Run complete - 13/13 signals retrieved, email sent: YES
```

- `13/13` means all indicators were fetched. A lower number (e.g. `11/13`)
  means a couple of data sources were down — the script used the last known
  values instead and kept going.
- `email sent: YES/NO` tells you if the email went out. If `NO`, the lines
  just above it say exactly why (almost always the Gmail App Password).

## Why it won't randomly break anymore

- Every data source has a **timeout and 3 retries**. A slow or flaky API is
  retried instead of crashing the run.
- Every number is **range-checked**. If a source returns nonsense (it has
  happened — net liquidity once came back wildly wrong), it's rejected and the
  last good value is used, with a warning in the log.
- The last good values are saved in **`state.json`** and committed back to the
  repo automatically, so they survive between runs.
- A **missed email no longer marks the whole run red** — the log still records
  it clearly, but a single bad night won't look like a catastrophic failure.
- The script uses **only Python's built-in tools**, so there are no third-party
  packages that can break on an update.

## The "13 vs 20" point

You may have thought of this as a "20-point" model. The live engine in this
repo computes **13 indicator tiles** (Equities, Volatility, Rates, Credit,
Commodities, Dollar, Breadth, Net liquidity, Positioning/COT, VVIX, Sector
rotation, Calendar gate, Fiscal impulse). The summary line reflects the real
13. If you want it expanded to a full 20, that's a separate piece of work —
just say the word.

## Delivery / output format (HTML email)

The daily run delivers the dashboard as a **compact, card-style HTML email** so
it renders identically on iPhone Mail and on a wide desktop mail client. A
plain-text version is always attached as the body **fallback** for clients that
strip HTML.

Formatting rules the script follows (see how `html` is built in
`short_dashboard.py`):

- **Fixed-width centered card**, `max-width:440px`, centered on the page, so it
  never stretches or wraps on a wide desktop screen.
- **Table-based layout with fully inline CSS only** (no `<style>` blocks, no
  flexbox/grid) for Gmail / Outlook / iPhone Mail compatibility.
- **Dark theme:** page background `#0f1115`, card panels `#12161c` / `#161a20`,
  borders `#262c36`, primary text `#f4f6fa`, muted text `#8b95a5`.
- **Sections, in order:** (1) header with date + next session, (2) hero SPX
  block (level, day %, vs-200DMA, vs-50DMA), (3) verdict banner pill
  (STAND DOWN / WATCH / INITIATE) + 200DMA-gate note + one-line summary,
  (4) PRIMARY + LAYER 2 verdict rows, (5) the indicator grid where each row has
  a coloured status dot (green `#4caf7d`, yellow `#e0a72d`, red `#e0533d`), a
  label and a short right-aligned value, (6) a red / yellow / green tally line,
  (7) a Layer 2 tactical block, (8) a "⚠ DATA FLAGS" strip, (9) a footer
  with sources.
- **Status-dot colours and every value are generated from the live data** the
  script already computes: each indicator's green / amber / red / gray verdict
  key is mapped to the matching dot colour. Nothing about the numbers is
  hardcoded.

The data-gathering, verdict logic, calendar gates and recipient list are
unchanged — only the email formatting/rendering was updated.
