# Money Flow Snapshot

Cross-asset snapshot (S&P 500/ES futures, US 10Y yield, DXY, gold, Bitcoin) with
direction + magnitude tags, run 3x/day on US weekdays via GitHub Actions.

This solves the two problems the cloud-assistant version kept hitting:
- **No real internet access** in a locked-down sandbox -> GitHub's own runners have
  normal internet access, so `yfinance` can actually reach Yahoo's data.
- **Only daily snapshots, no real trend** -> the script pulls 15-minute intraday
  bars and reports whether each asset has been trending up or down over the last
  ~3 hours, computed from real bars instead of guessed from a day's range.

## Setup (5 minutes)

1. Create a new **private** GitHub repo and push this folder to it:
   ```bash
   cd money-flow-snapshot
   git remote add origin https://github.com/<your-username>/money-flow-snapshot.git
   git branch -M main
   git add -A
   git commit -m "Initial commit"
   git push -u origin main
   ```

2. (Optional but recommended) Get push notifications for free via
   [ntfy.sh](https://ntfy.sh) — no signup required:
   - Pick a topic name only you would guess, e.g. `bob-money-flow-8f3k2`.
   - Install the ntfy app (iOS/Android) or open `https://ntfy.sh/<your-topic>` in
     a browser and subscribe.
   - In your GitHub repo: **Settings -> Secrets and variables -> Actions -> New
     repository secret**, name it `NTFY_TOPIC`, value = your topic name.
   - Without this secret, the workflow still runs and prints the report to the
     Actions log / job summary — you just won't get a push notification.

3. The workflow (`.github/workflows/money-flow.yml`) is already scheduled 3x/day
   on weekdays. You can also trigger it manually any time from the repo's
   **Actions** tab -> "Money Flow Snapshot" -> **Run workflow**.

## Notes

- Cron times are UTC and don't auto-shift for US Daylight Saving Time — see the
  comment in the workflow file for the DST-adjusted times.
- `yfinance` pulls from Yahoo's public data; it's free and reliable enough for
  this kind of snapshot, but it is still an unofficial API, so an occasional
  failed run is possible. `workflow_dispatch` lets you re-run manually if one
  fails.
- Macro headlines (Fed speakers, CPI, geopolitical news) are **not** included —
  that requires a news API. The script only covers the 5-asset numeric
  snapshot with magnitude tags and the last-~3h trend.
- This is an informational tool, not financial advice.
