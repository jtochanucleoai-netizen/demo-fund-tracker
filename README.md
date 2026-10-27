# Demo Fund Tracker

Tracks a €50,000 demo stock portfolio built for a university financial-management course, against MSCI World in euros.

**Live page:** https://jtochanucleoai-netizen.github.io/demo-fund-tracker/

## How it works
- `data/tracker/fund.json` — the 13 holdings, share counts and rules.
- `scripts/fund_tracker.py` — buys at the official close of 27 Oct 2026, charges a 0.5% currency-conversion fee on non-EUR trades,
  credits dividends net of withholding tax on the ex-date, and values the fund every trading day (Yahoo Finance prices).
- `.github/workflows/update.yml` — runs twice per weekday (after the European and US closes), publishes `docs/index.html`
  and commits the daily values to `data/tracker/nav.csv`, so every valuation is timestamped in the git history.
- `data/tracker/entry.json` — the locked purchase prices, FX rates, fees and starting cash.

Run locally: `python3 scripts/fund_tracker.py` (Python 3.9+, standard library only).

Educational project, not investment advice.
