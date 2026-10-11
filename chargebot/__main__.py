"""Entry point: ``python -m chargebot`` (or ``python chargingbot.py``)."""

from __future__ import annotations

import dataclasses
import logging
import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .config import Config
from .engine import Engine
from .reminders.gmail import GmailSender
from .scheduler import Scheduler
from .slack.handlers import register
from .slack.notifier import Notifier
from .slack.users import UserDirectory
from .store import FileStore, MemoryStore, SlackStore
from .web.server import WebApp, start_server

log = logging.getLogger("chargebot")


def _resolve_slack_ids(cfg: Config, client) -> Config:
    """Fill in the app and team ids (used for the dashboard's 'open in Slack' QR code)."""
    if cfg.slack_app_id and cfg.slack_team_id:
        return cfg
    try:
        auth = client.auth_test()
        app_id = cfg.slack_app_id
        if not app_id and auth.get("bot_id"):
            app_id = client.bots_info(bot=auth["bot_id"])["bot"].get("app_id")
        return dataclasses.replace(cfg, slack_app_id=app_id, slack_team_id=cfg.slack_team_id or auth.get("team_id"))
    except Exception as e:
        log.info("Could not look up Slack app id (%s); the dashboard won't show a Slack QR code.", e)
        return cfg


def _make_store(cfg: Config, client):
    if cfg.state_channel_id:
        log.info("Persisting state in Slack channel %s.", cfg.state_channel_id)
        return SlackStore(client, cfg.state_channel_id)
    if cfg.state_file:
        log.info("Persisting state in %s.", cfg.state_file)
        return FileStore(cfg.state_file)
    log.warning("No STATE_CHANNEL_ID or STATE_FILE set: the queue will be lost on every restart.")
    return MemoryStore()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s")
    cfg = Config.from_env()
    if not cfg.slack_bot_token or not cfg.slack_app_token:
        log.error("SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set.")
        sys.exit(1)

    app = App(token=cfg.slack_bot_token)
    cfg = _resolve_slack_ids(cfg, app.client)

    store = _make_store(cfg, app.client)
    engine = Engine(cfg, store)
    engine.load()

    users = UserDirectory(app.client)
    gmail = GmailSender(cfg)
    log.info("Calendar reminders by email are %s.", "on" if gmail.ready else "off (Gmail settings incomplete)")
    notifier = Notifier(cfg, app.client, engine, users, store, gmail)
    engine.set_dispatcher(notifier.dispatch)
    notifier.start()

    register(app, cfg, engine, notifier, users)
    handler = SocketModeHandler(app, cfg.slack_app_token)

    start_server(WebApp(cfg, engine, users.get, socket_connected=handler.client.is_connected), cfg.port)
    Scheduler(engine, cfg.tick_s).start()

    log.info("ChargeBot starting with %d charger(s).", len(cfg.chargers))
    try:
        handler.start()
    finally:
        store.flush()
        log.info("ChargeBot stopped.")


if __name__ == "__main__":
    main()
