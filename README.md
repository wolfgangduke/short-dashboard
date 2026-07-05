# short-dashboard — SNP 500 Crash Alert

A scheduled Python job (run on GitHub Actions) that pulls live market data, scores a set of crash/leading-indicator signals, and emails a color-coded HTML dashboard on US trading days.

## What it does

- Runs automatically Monday–Friday shortly after the US market close (see the schedule in `.github/workflows/dashboard.yml`).
- - Pulls data from Financial Modeling Prep and FRED.
  - - Scores indicator tiles (equities, volatility, rates, credit, commodities, dollar, breadth, net liquidity, positioning, leading signals, etc.) and produces a STAND DOWN / WATCH / INITIATE verdict.
    - - Sends the result as a card-style HTML email, with a plain-text fallback and an attached interactive HTML report.
      - - Persists last-known values to `state.json` after each run so a bad data source doesn't break the next run.
       
        - ## Recipients
       
        - - **To:** the primary recipient, configured via `DEFAULT_RECIPIENTS` in `short_dashboard.py`.
          - - **Cc:** any additional recipients supplied via the `MAIL_TO` secret/env var (read live at send time — never hardcoded).
            - - The envelope send still targets the full union of To + Cc, so everyone on the list receives the email.
             
              - ## Running it manually
             
              - Repo → **Actions** tab → **MacroSage SHORT dashboard** → **Run workflow** → pick a branch → **Run workflow**.
             
              - ## Configuration
             
              - See [SETUP.md](./SETUP.md) for the full plain-English guide, including the GitHub Secrets this project needs (`FMP_API_KEY`, `FRED`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `MAIL_TO`), how to create a Gmail App Password, and how to read a run's log output.
             
              - ## Files
             
              - | File | Purpose |
              - |---|---|
              - | `short_dashboard.py` | Main script: fetches data, scores indicators, builds and sends the email |
              - | `fmp_client.py` | Thin client for the Financial Modeling Prep API |
              - | `run_fmp.py` | Standalone macro snapshot / crash-signal summary |
              - | `sim_drill.py` | Simulation / escalation drill helper |
              - | `test_harness.py` | Scenario-based tests for the verdict engine |
              - | `state.json` | Last-known signal values, committed back by the workflow |
              - | `SETUP.md` | Full setup and operations guide |
