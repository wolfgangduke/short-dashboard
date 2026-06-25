# SHORT Macro Dashboard — Cloud (GitHub Actions)

Runs the keyless SHORT dashboard on GitHub's servers on a schedule and emails it to you.
**Your PC does not need to be on.** Free on public or private repos (Actions free tier).

## What's here
- `short_dashboard_email.py` — the dashboard; pulls yfinance + FRED, emails via Gmail SMTP.
- `requirements.txt` — just `yfinance`.
- `.github/workflows/short-dashboard.yml` — the schedule (07:00 Perth, Mon–Fri).

## One-time setup (~10 min)

### 1. Make a Gmail App Password
A normal Gmail password won't work for SMTP. You need an **App Password**:
1. Google Account → Security → enable **2-Step Verification** (required first).
2. Then Security → **App passwords** → create one (name it "short-dashboard").
3. Copy the 16-character code (no spaces).

### 2. Create the repo
- New GitHub repo (private is fine), e.g. `short-dashboard`.
- Upload these three files, keeping the `.github/workflows/` folder structure.

### 3. Add the secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add:
| Name | Value |
|------|-------|
| `GMAIL_USER` | wolfgangduke@gmail.com |
| `GMAIL_APP_PASSWORD` | the 16-char app password from step 1 |
| `MAIL_TO` | wolfgangduke@gmail.com, richard.macrae.gordon@gmail.com |

### 4. Turn it on / test
- Repo → **Actions** tab → enable workflows if prompted.
- Open **SHORT Macro Dashboard** → **Run workflow** to fire a test now.
- Check both inboxes. After that it runs automatically each weekday morning.

## Adjusting the time
Edit the `cron` line in the workflow. It's **UTC**. Current `0 23 * * 0-4` = 07:00 Perth Mon–Fri.
- US pre-open feel (08:00 ET) → `0 12 * * 1-5`.

## Notes
- F&G (CNN) and NAAIM may show "manual" — same scrape limits as before; everything else is live.
- Verify the 2026 FOMC dates in the script against federalreserve.gov once.
