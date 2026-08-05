"""One-line alert email sent by dashboard-watchdog.yml when dashboard.yml's
scheduled trigger didn't produce a successful run today. Stdlib only, per
project convention. Every failure path here is caught and logged, never
raised — the watchdog's re-trigger step must not depend on this succeeding.
"""
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.text import MIMEText

DEFAULT_RECIPIENTS = ["wolfgangduke@gmail.com"]


def recipients():
    """Same shape as short_dashboard.py's RECIPIENTS: default To + MAIL_TO extras."""
    out = list(DEFAULT_RECIPIENTS)
    extra = os.environ.get("MAIL_TO", "")
    for addr in extra.replace(";", ",").split(","):
        addr = addr.strip()
        if addr and addr not in out:
            out.append(addr)
    return out


def main():
    user = os.environ.get("GMAIL_USER")
    pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not pw:
        print("WATCHDOG ALERT SKIPPED: missing GMAIL_USER / GMAIL_APP_PASSWORD secret")
        return

    to = recipients()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    repo = os.environ.get("GITHUB_REPOSITORY", "wolfgangduke/short-dashboard")
    body = (
        "MacroSage dashboard.yml had no successful scheduled run today (%s UTC).\n\n"
        "dashboard-watchdog.yml re-triggered it via workflow_dispatch just now.\n"
        "Check the Actions tab to confirm the recovery run completed and the\n"
        "daily email actually landed:\n\n"
        "https://github.com/%s/actions/workflows/dashboard.yml\n"
    ) % (today, repo)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = "MacroSage watchdog - missed scheduled run (%s)" % today
    msg["From"] = user
    msg["To"] = ", ".join(to)

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as s:
            s.login(user, pw)
            s.sendmail(user, to, msg.as_string())
        print("WATCHDOG ALERT SENT to %s" % ", ".join(to))
    except Exception as e:
        print("WATCHDOG ALERT EMAIL FAILED: %s" % e)


if __name__ == "__main__":
    main()
