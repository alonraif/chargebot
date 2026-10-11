"""Slack entry points: slash commands, buttons, App Home, shortcut and admin modal."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from slack_sdk.errors import SlackApiError

from ..config import Config
from ..domain import Result
from ..engine import Engine
from ..web.login_codes import LoginCodes
from . import blocks as B
from .notifier import Notifier
from .users import UserDirectory

log = logging.getLogger(__name__)

SHORTCUT_CALLBACK = "ev_charger_open"

SUBCOMMANDS = {
    "": "join", "join": "join", "in": "join", "request": "join", "checkin": "join",
    "confirm": "confirm", "plugged": "confirm",
    "done": "done", "end": "done", "stop": "done",
    "unplugged": "unplugged",
    "leave": "leave", "exit": "leave",
    "skip": "skip", "pass": "skip",
    "extend": "extend", "more": "extend",
    "nudge": "nudge",
    "reclaim": "reclaim",
}

LEGACY_COMMANDS = {
    "/checkin": "", "/request": "", "/endcharge": "done", "/exitqueue": "leave", "/chargestatus": "status",
}


def register(app: Any, cfg: Config, engine: Engine, notifier: Notifier, users: UserDirectory,
             codes: Optional[LoginCodes] = None) -> None:
    names = users.name
    codes = codes or LoginCodes()

    # -- helpers -------------------------------------------------------------------

    def reply_text(result: Result) -> str:
        state, _ = engine.view()
        return B.reply(result.code, result.data, cfg, state, names)

    def reply_blocks(text: str, user_id: str, extra: Optional[str] = None):
        state, _ = engine.view()
        return B.compact([
            B.section(text),
            B.viewer_buttons(state, cfg, user_id, notifier.link_for(user_id)),
            B.context(extra) if extra else None,
        ])

    def charger_from(tokens: list) -> tuple:
        """'/charge admin out 2 Broken cable' -> ('c2', 'Broken cable') when several chargers exist."""
        state, _ = engine.view()
        charger_id = state.chargers[0].id if state.chargers else "c1"
        if tokens and tokens[0].isdigit() and cfg.multi_charger:
            charger_id = f"c{int(tokens.pop(0))}"
        return charger_id, " ".join(tokens)

    def run(sub: str, rest: str, body: Dict[str, Any], respond: Any, client: Any, legacy: bool) -> None:
        user = body["user_id"]
        tip = "Tip: `/charge` now does it all. Try `/charge help`." if legacy else None

        if sub in ("dashboard", "web", "link"):
            link = notifier.link_for(user)
            if not link:
                respond(text="The dashboard address isn't configured yet (PUBLIC_BASE_URL).")
                return
            respond(text="Your dashboard link", blocks=B.my_dashboard(link, cfg.device_link_days))
            return

        if sub in ("login", "code", "signin"):
            text = B.login_code_text(codes.issue(user))
            respond(text=text, blocks=[B.section(text)])
            return

        if sub == "help":
            text = B.help_text(cfg) + ("\n\n" + B.ADMIN_HELP if cfg.is_admin(user) else "")
            respond(text=text, blocks=[B.section(text)])
            return

        if sub == "status":
            state, now = engine.view()
            respond(text="Charger status",
                    blocks=B.compact([*B.status_blocks(state, now, cfg, names, user, notifier.link_for(user)),
                                      B.context(tip) if tip else None]))
            return

        if sub == "admin":
            if not cfg.is_admin(user):
                respond(text=B.reply("not_admin", {}, cfg, None, names))
                return
            tokens = rest.split()
            verb = tokens.pop(0).lower() if tokens else ""
            if verb in ("out", "ooo", "broken"):
                charger_id, reason = charger_from(tokens)
                result = engine.set_out_of_order(charger_id, reason or f"Marked by {names(user)}")
                respond(text=reply_text(result))
            elif verb in ("back", "fixed", "resume"):
                charger_id, _ = charger_from(tokens)
                respond(text=reply_text(engine.set_out_of_order(charger_id, None)))
            else:
                state, now = engine.view()
                client.views_open(trigger_id=body["trigger_id"], view=B.admin_modal(state, now, cfg, names))
            return

        action = SUBCOMMANDS.get(sub)
        if action is None:
            respond(text=f"I don't know `{sub}`.\n\n{B.HELP}")
            return
        result = engine.act(action, user)
        text = reply_text(result)
        respond(text=text, blocks=reply_blocks(text, user, tip))

    # -- slash commands ------------------------------------------------------------

    @app.command("/charge")
    def charge_command(ack, body, respond, client):
        ack()
        sub, _, rest = (body.get("text") or "").strip().partition(" ")
        run(sub.lower(), rest.strip(), body, respond, client, legacy=False)

    def legacy_handler(sub: str):
        def handler(ack, body, respond, client):
            ack()
            run(sub, "", body, respond, client, legacy=True)
        return handler

    for command, sub in LEGACY_COMMANDS.items():
        app.command(command)(legacy_handler(sub))

    # -- buttons -------------------------------------------------------------------

    def respond_to_action(body: Dict[str, Any], respond: Any, client: Any, user: str,
                          result: Result) -> None:
        text = reply_text(result)
        container = body.get("container") or {}
        view = body.get("view") or {}

        if container.get("type") == "view":
            if view.get("type") == "home":
                notifier.publish_home(user, notice=text)
            else:
                state, now = engine.view()
                client.views_update(view_id=view["id"], view=B.panel_modal(
                    state, now, cfg, names, user, notifier.link_for(user), notice=text))
            return

        state, _ = engine.view()
        is_live_message = container.get("message_ts") and container.get("message_ts") == state.meta.get("status_ts")
        if container.get("is_ephemeral"):
            respond(replace_original=True, text=text, blocks=reply_blocks(text, user))
        elif result.ok and not is_live_message:
            respond(replace_original=True, text=text, blocks=[B.context(text)])
        else:
            respond(replace_original=False, response_type="ephemeral", text=text,
                    blocks=reply_blocks(text, user))

    def user_action_handler(action_name: str):
        def handler(ack, body, respond, client):
            ack()
            user = body["user"]["id"]
            result = engine.act(action_name, user)
            respond_to_action(body, respond, client, user, result)
        return handler

    for action_id, action_name in B.USER_ACTION_IDS.items():
        app.action(action_id)(user_action_handler(action_name))

    @app.action(B.A_LINK)
    def link_clicked(ack):
        ack()

    @app.action(B.A_PREF_EMAIL)
    def pref_email(ack, body, action):
        ack()
        user = body["user"]["id"]
        on = action.get("value") == "on"
        engine.set_pref(user, "email_reminder", on)
        notifier.publish_home(user, notice="📅 Calendar reminders " + ("on." if on else "off."))

    @app.action(B.A_MY_DASHBOARD)
    def my_dashboard(ack, body, respond):
        ack()
        user = body["user"]["id"]
        link = notifier.link_for(user)
        if link:
            respond(replace_original=False, response_type="ephemeral", text="Your dashboard link",
                    blocks=B.my_dashboard(link, cfg.device_link_days))

    @app.action(B.A_LOGIN_CODE)
    def login_code(ack, body, client):
        ack()
        code = codes.issue(body["user"]["id"])
        client.views_open(trigger_id=body["trigger_id"], view=B.login_code_modal(code))

    @app.action(B.A_SIGNOUT_ALL)
    def signout_all(ack, body):
        ack()
        user = body["user"]["id"]
        engine.set_pref(user, "token_gen", int(engine.pref(user, "token_gen", 0)) + 1)
        notifier.publish_home(user, notice="🔑 Signed out of the dashboard on every device.")

    # -- App Home and shortcut --------------------------------------------------------

    @app.event("app_home_opened")
    def home_opened(event):
        if event.get("tab", "home") != "home":
            return
        notifier.note_home_open(event["user"])
        notifier.publish_home(event["user"])

    @app.shortcut(SHORTCUT_CALLBACK)
    def open_panel(ack, body, client):
        ack()
        user = body["user"]["id"]
        state, now = engine.view()
        client.views_open(trigger_id=body["trigger_id"],
                          view=B.panel_modal(state, now, cfg, names, user, notifier.link_for(user)))

    # -- admin -------------------------------------------------------------------------

    def admin_refresh(client: Any, body: Dict[str, Any], notice: str) -> None:
        state, now = engine.view()
        try:
            client.views_update(view_id=body["view"]["id"],
                                view=B.admin_modal(state, now, cfg, names, notice=notice))
        except SlackApiError as e:
            log.warning("Admin modal update failed: %s", e.response.get("error"))

    def require_admin(body: Dict[str, Any]) -> Optional[str]:
        user = body["user"]["id"]
        return user if cfg.is_admin(user) else None

    @app.action(B.A_ADMIN_OPEN)
    def admin_open(ack, body, client):
        ack()
        if not require_admin(body):
            return
        state, now = engine.view()
        client.views_open(trigger_id=body["trigger_id"], view=B.admin_modal(state, now, cfg, names))

    @app.action(B.A_ADMIN_END)
    def admin_end(ack, body, action, client):
        ack()
        admin = require_admin(body)
        if not admin:
            return
        admin_refresh(client, body, reply_text(engine.admin_end(action["value"], by=admin)))

    @app.action(B.A_ADMIN_OOO)
    def admin_ooo(ack, body, action, client):
        ack()
        admin = require_admin(body)
        if not admin:
            return
        verb, charger_id = action["value"].split(":", 1)
        reason = f"Marked by {names(admin)}" if verb == "out" else None
        admin_refresh(client, body, reply_text(engine.set_out_of_order(charger_id, reason)))

    @app.action(B.A_ADMIN_QUEUE)
    def admin_queue(ack, body, action, client):
        ack()
        admin = require_admin(body)
        if not admin:
            return
        verb, target = action["selected_option"]["value"].split(":", 1)
        result = engine.admin_move_up(target) if verb == "up" else engine.admin_remove(target, by=admin)
        admin_refresh(client, body, reply_text(result))
