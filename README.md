# InstaGrow

A self-hosted Instagram growth manager. Auto-unfollow people who don't follow back, slowly auto-follow accounts with mutual followers, blacklist, activity log.

Runs locally on your computer — your credentials never leave your machine, and Instagram is much less likely to flag your account than when running from a cloud datacenter IP.

## Run on Windows

Pick **one** option:

### Option A: Docker (recommended)

1. Install [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/).
2. Clone the repo, then in PowerShell or CMD:
   ```
   cd InstaGrow
   docker compose up -d
   ```
3. Open http://localhost:8000

The container auto-restarts when you reboot. Database and Instagram session persist in the `./data` folder.

To stop: `docker compose down`
To update after `git pull`: `docker compose up -d --build`

### Option B: Python directly

1. Install [Python 3.10+](https://www.python.org/downloads/) — make sure "Add Python to PATH" is checked.
2. Double-click `start.bat` (or run it from CMD).
3. Open http://localhost:8000

You need to keep the terminal window open while it's running.

## Run on Mac / Linux

```bash
./start.sh
```

Or use Docker exactly as above.

## How it works

- **Dashboard** — counts of following / not-following-back / following-back, daily progress
- **Following** — sync your following list from Instagram, filter, bulk unfollow
- **Auto-Unfollow** — toggle on, set "after X days" and a daily cap. Runs every 30 minutes within active hours.
- **Auto-Follow** — toggle on, set daily cap and delay range. Pulls from the Follow Queue.
- **Follow Queue** — populated by the Followers-of-Following scanner. Adjust mutual threshold + scan depth in Auto-Follow settings.
- **Blacklist** — accounts you never want to follow.
- **Activity Log** — every action is logged.

## Detection-avoidance defaults

- Daily limits capped at 150 follows / 200 unfollows
- Random delays (45–180s by default) between actions
- Active hours window (9 AM – 10 PM UTC default) — no 3 AM activity
- Session is saved and reused, no repeated logins
- Runs from your home IP, not a cloud datacenter

## ⚠️ Disclaimer

Instagram's Terms of Service prohibit automation. Use at your own risk. Conservative limits and delays exist to minimize that risk, but no tool can guarantee your account won't be flagged. Don't use this on an account you can't afford to lose.
