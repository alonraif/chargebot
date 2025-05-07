# 🔌 ChargingBot

**ChargingBot** is a Slack bot that manages an electric vehicle (EV) charger queue at your workplace. It helps users check in, join a queue, get notified when it's their turn, and see the current charger status — all from Slack.

---

## 🚀 Features

- `/checkin` — Start a new charging session (if charger is free).
- `/request` — Join the queue if someone else is charging.
- `/endcharge` — End your charging session early.
- `/exitqueue` — Leave the waiting queue.
- `/chargestatus` — Check who’s charging and view the current queue.

---

## ⚙️ Requirements

- Python 3.8+
- Slack App with:
  - **Socket Mode enabled**
  - `commands` and `chat:write` scopes
  - Slash commands set up for `/checkin`, `/request`, etc.
- Slack tokens:
  - `SLACK_BOT_TOKEN` (starts with `xoxb-`)
  - `SLACK_APP_TOKEN` (starts with `xapp-`)

Install required Python packages:

```bash
pip install slack_bolt
