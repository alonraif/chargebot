# ChargeBot ⚡

A Slack bot and web dashboard for sharing the office EV charger fairly. People get in line with one command or one tap, get a DM when it's their turn, confirm they've plugged in, and get reminded when time's up. Nobody holds the charger without showing up.

## How it works for people

- **`/charge`**: claim the charger if it's free, otherwise join the queue. Every reply is visible only to you, in any channel.
- **Your turn**: you get a DM with **I'm plugged in** and **Skip my turn**. Confirm within **10 minutes** or you lose your turn. Your 120-minute clock starts when you confirm, or after the 5-minute grace period at the latest. You get alerts at 5 and 8 minutes, and the next person gets a heads-up before the queue moves.
- **While charging**: 10 minutes before the end you get **I'm done** and, if nobody's waiting, **Extend 30 min**.
- **Time's up**: the next person is offered the charger right away. If your car is still plugged in, they can tap **Nudge** to ask you politely.
- **Missed your turn with nobody waiting?** Tap **Reclaim** to get it back.
- **App Home**: open ChargeBot in Slack and pick the **Home** tab for live status, one-tap buttons, your stats, and a calendar-reminder toggle.
- **Live status**: one pinned message in the status channel, edited in place.
- **Shortcut**: search "EV Charger" in Slack's shortcuts menu for a status and actions panel. It works well on mobile.

Other subcommands: `/charge status`, `/charge done`, `/charge skip`, `/charge leave`, `/charge extend`, `/charge help`.
The old commands (`/checkin`, `/request`, `/endcharge`, `/exitqueue`, `/chargestatus`) still work.

**Admins** (listed in `ADMIN_USER_IDS`): `/charge admin` opens a panel to end a session, reorder or remove people in the queue, and mark the charger out of order. `/charge admin out <reason>` and `/charge admin back` do the same from the command line.

## Web pages

| Page | What it is |
|---|---|
| `/` (also `/dashboard`) | Live dashboard. Opened from a Slack DM link, it shows your own buttons, including confirming from your phone. |
| `/kiosk` | TV mode for a screen near the charger. `/kiosk?k=<KIOSK_KEY>` adds a big **I'm plugged in** button for whoever's turn it is. |
| `/stats` | Totals only: sessions per day, average wait, no-show rate, busiest hours. |
| `/healthz` | `200` when Slack is connected and the clock loop is running, `503` otherwise. Point the keep-alive cron here. |
| `/api/state`, `/api/stats` | JSON behind the pages. No Slack IDs or emails. |

**Signing in.** The *Open dashboard* buttons in Slack carry a signed link that's valid for 24 hours. The first time it's opened, the dashboard swaps it for a pass kept on that device for `DEVICE_LINK_DAYS` (90 by default), so the plain address keeps showing your buttons. Each device has its own pass. *Sign out* on the page forgets it on that device, and *Sign out everywhere* in Slack's Home tab cancels every pass that person was given. Changing `WEB_SECRET` signs everyone out.

**Install as an app.** The dashboard is an installable web app. In Chrome, click *Install app* in the header (or the install icon in the address bar). On Android, use the menu → *Install app*. On iPhone, use Safari → Share → *Add to Home Screen*. It opens in its own window with the Juice icon.

## Rolling out the new version

### Stage 1: Slack changes that are safe before deploying

At api.slack.com/apps, open the ChargeBot app.

1. **Settings → App Manifest**: copy the current YAML somewhere as a backup.
2. **OAuth & Permissions → Bot Token Scopes**: add `groups:history` and `pins:write`.
3. Click **Reinstall to Workspace** in the banner. Check that the Bot User OAuth Token is unchanged; if it changed, update `SLACK_BOT_TOKEN` on Render.
4. **App Home**: turn on **Home Tab**.
5. **Event Subscriptions**: turn on, subscribe to the bot event `app_home_opened`, and save.
6. **Interactivity & Shortcuts**: turn on and save.
7. Create a private channel `#chargebot-state` and invite the bot. Invite the bot to the status channel too.
8. Collect each admin's member ID: their profile → **⋮** → **Copy member ID**.

### Stage 2: deploy

On Render, open the service, go to **Environment**, and add these variables. Then commit and deploy.

| Variable | Value |
|---|---|
| `STATE_CHANNEL_ID` | `C0C8H4UAK1S` (`#chargebot-state`) |
| `STATUS_CHANNEL_ID` | `C08RLHL052Q` (`#liveu-electric-cars`) |
| `ADMIN_USER_IDS` | comma-separated member IDs |
| `PUBLIC_BASE_URL` | `https://chargebot-tliw.onrender.com` |
| `WEB_SECRET` | a long random string, for example from `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `KIOSK_KEY` | another random string |

The start command can stay `python chargingbot.py`, and the build command stays `pip install -r requirements.txt`.

### Stage 3: right after the deploy

1. **Slash Commands → Create New Command**: `/charge`, description "Get in line for the EV charger", usage hint `[status | done | leave | skip | extend | help]`.
2. **Interactivity & Shortcuts → Create New Shortcut → Global**: name "EV Charger", callback ID `ev_charger_open`.
3. Optionally, rename the old commands' descriptions to "Old, use /charge".
4. Point the keep-alive cron at `/healthz` so it also alerts on outages.

`slack-manifest.yaml` describes the finished configuration. Use it to create a separate dev app, or to restore the app. Pasting it replaces the whole configuration.

## Configuration

See [.env.example](.env.example) for every setting. Timing is in minutes: `CHARGE_MIN`, `GRACE_MIN`, `CONFIRM_MIN`, `CONFIRM_ALERT_AT_MIN`, `WARN_BEFORE_END_MIN`, `EXTEND_MIN`, `MAX_EXTENSIONS`, `NEXT_UP_HEADS_UP_MIN` and `NUDGE_COOLDOWN_MIN`.

**Adding a second charger:** set `CHARGERS="Charger 1,Charger 2"` (any names) and redeploy. There's one shared queue, and the next person gets whichever charger frees up first. With one charger, names never appear anywhere.

### Where state lives

Render's free tier has no disk, so the bot keeps its memory in the private Slack channel `STATE_CHANNEL_ID`:

- One message whose hidden metadata holds the current state. It's edited on every change. **Don't delete or edit it.**
- One short message per finished session, which doubles as an audit log and feeds the stats.

On restart the bot reads them back and carries on, and missed deadlines fire once. Without `STATE_CHANNEL_ID` (or `STATE_FILE` locally), the state is kept in memory only.

### Calendar reminders (optional)

When a session is confirmed, the bot emails a calendar invite timed for the end of the session. The invite is cancelled if the session ends early and updated if it's extended. People can turn it off in App Home. Set the `GMAIL_*` variables to enable it. In Google Cloud, set the OAuth consent screen to **In production**: in "Testing" mode, refresh tokens expire after 7 days.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
ruff check chargebot tests chargingbot.py
```

**Preview the dashboard with fake data, no Slack needed.** The command prints personal and kiosk links:

```bash
python -m chargebot.devserver --port 8080
```

**Run the real bot against a dev Slack app**, keeping state in a local file:

```bash
STATE_FILE=dev.state.json python -m chargebot
```

### Layout

```text
chargebot/
  domain.py        pure state machine: queue, sessions, deadlines (no I/O; fully unit-tested)
  engine.py        owns the state; applies rules under a lock, then saves and sends effects outside it
  scheduler.py     one clock loop that drives every deadline
  config.py        settings from environment variables
  stats.py         usage statistics from the session log
  store/           persistence: Slack channel, local file, memory
  slack/           handlers (commands, buttons, Home, shortcut, admin), message copy, notifier, user cache
  reminders/       Gmail calendar invites
  web/             HTTP server, signed links, static pages (Volt theme, light and dark)
  devserver.py     dashboard preview with fake data
chargingbot.py     compatibility entry point (python chargingbot.py)
tests/             domain timelines, Slack handlers against a fake Slack, web API, store and stats
```
