"""All Slack copy and Block Kit layouts live here, so wording and tone stay consistent."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import Config
from ..domain import State, charger_phase, queue_etas
from ..stats import user_stats

Names = Callable[[Optional[str]], str]

# Action ids.
A_JOIN = "cb_join"
A_CONFIRM = "cb_confirm"
A_SKIP = "cb_skip"
A_DONE = "cb_done"
A_UNPLUGGED = "cb_unplugged"
A_LEAVE = "cb_leave"
A_EXTEND = "cb_extend"
A_NUDGE = "cb_nudge"
A_RECLAIM = "cb_reclaim"
A_STATUS = "cb_status"
A_LINK = "cb_link"
A_PREF_EMAIL = "cb_pref_email"
A_SIGNOUT_ALL = "cb_signout_all"
A_MY_DASHBOARD = "cb_my_dashboard"
A_LOGIN_CODE = "cb_login_code"
A_ADMIN_OPEN = "cb_admin_open"
A_ADMIN_END = "cb_admin_end"
A_ADMIN_OOO = "cb_admin_ooo"
A_ADMIN_QUEUE = "cb_admin_queue"

USER_ACTION_IDS = {
    A_JOIN: "join", A_CONFIRM: "confirm", A_SKIP: "skip", A_DONE: "done", A_UNPLUGGED: "unplugged",
    A_LEAVE: "leave", A_EXTEND: "extend", A_NUDGE: "nudge", A_RECLAIM: "reclaim",
}

ADMIN_CALLBACK = "cb_admin_modal"
PANEL_CALLBACK = "cb_panel_modal"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def when(t: Optional[float], fmt: str = "{time}") -> str:
    """A time that Slack renders in each reader's own timezone."""
    if not t:
        return "soon"
    fallback = time.strftime("%H:%M UTC", time.gmtime(t))
    return f"<!date^{int(t)}^{fmt}|{fallback}>"


def mins(seconds: float) -> int:
    return max(0, int(round(seconds / 60)))


def duration(seconds: float) -> str:
    m = mins(seconds)
    if m >= 60 and m % 60 == 0:
        h = m // 60
        return f"{h} hour" + ("s" if h != 1 else "")
    return f"{m} min"


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def on_charger(cfg: Config, state: Optional[State], charger_id: Optional[str]) -> str:
    if not cfg.multi_charger or not charger_id:
        return ""
    charger = state.charger(charger_id) if state else None
    name = charger.name if charger else charger_id
    return f" on *{name}*"


def button(text: str, action_id: str, value: str = "x", style: Optional[str] = None,
           url: Optional[str] = None, confirm: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    b: Dict[str, Any] = {"type": "button", "text": {"type": "plain_text", "text": text, "emoji": True},
                         "action_id": action_id, "value": value}
    if style:
        b["style"] = style
    if url:
        b["url"] = url
    if confirm:
        b["confirm"] = confirm
    return b


def confirm_dialog(title: str, text: str, ok: str) -> Dict[str, Any]:
    return {"title": {"type": "plain_text", "text": title}, "text": {"type": "mrkdwn", "text": text},
            "confirm": {"type": "plain_text", "text": ok}, "deny": {"type": "plain_text", "text": "Cancel"}}


def section(text: str, accessory: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    if accessory:
        block["accessory"] = accessory
    return block


def context(text: str) -> Dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def actions(*elements: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    els = [e for e in elements if e]
    return {"type": "actions", "elements": els} if els else None


def header(text: str) -> Dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def divider() -> Dict[str, Any]:
    return {"type": "divider"}


def compact(blocks: List[Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return [b for b in blocks if b]


def dashboard_button(url: Optional[str]) -> Optional[Dict[str, Any]]:
    return button("Open dashboard", A_LINK, url=url) if url else None


# ---------------------------------------------------------------------------
# DM notifications
# ---------------------------------------------------------------------------


def notification(kind: str, data: Dict[str, Any], cfg: Config, state: Optional[State], names: Names,
                 link: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    on = on_charger(cfg, state, data.get("charger_id"))
    confirm_btn = button("✅ I'm plugged in", A_CONFIRM, style="primary")
    skip_btn = button("⏭ Skip my turn", A_SKIP)
    done_btn = button("🏁 I'm done and unplugged", A_DONE)
    unplugged_btn = button("🔌 I've unplugged", A_UNPLUGGED, style="primary")

    if kind == "offered":
        prev = data.get("previous_user_id")
        text = f"🔌 Your turn! The charger is yours{on}."
        blocks = compact([
            section(f"*🔌 Your turn! The charger is yours{on}.*\n"
                    f"Plug in and tap *I'm plugged in* by {when(data['confirm_deadline'])} to keep your turn. "
                    f"Your {duration(cfg.charge_s)} clock starts when you confirm, "
                    f"or at {when(data['grace_ends_at'])} at the latest."),
            context(f"🚗 {names(prev)}'s session just ended. If their car's still plugged in, tap *Nudge*.")
            if prev else None,
            actions(confirm_btn, skip_btn, button("🔔 Nudge", A_NUDGE) if prev else None, dashboard_button(link)),
        ])
        return text, blocks

    if kind == "confirm_alert":
        deadline = data["confirm_deadline"]
        if data.get("final"):
            text = "🚨 Last call: confirm now or you'll lose your turn."
            body = (f"*🚨 Last call.* Confirm by {when(deadline)} or the charger goes to the next person.")
        elif data.get("clock_started"):
            text = "⏰ Your charging clock has started."
            body = (f"*⏰ Your charging clock has started.* Tap *I'm plugged in* by {when(deadline)} "
                    "or you'll lose your turn.")
        else:
            text = "⏰ Reminder: confirm you've plugged in."
            body = f"*⏰ Reminder:* confirm by {when(deadline)} to keep your turn."
        return text, compact([section(body), actions(confirm_btn, skip_btn, dashboard_button(link))])

    if kind == "maybe_soon":
        text = f"👀 You might be up in about {data['minutes']} min."
        return text, [section(f"👀 *Heads-up:* the person ahead of you hasn't plugged in yet. "
                              f"You might be up in about {data['minutes']} min.")]

    if kind == "lost_turn":
        if data.get("reclaimable"):
            text = "⌛ Your turn passed, but the charger is still free."
            return text, compact([
                section("*⌛ Your turn passed* because it wasn't confirmed in time. "
                        "Nobody else is waiting, so you can still take it."),
                actions(button("🔁 I'm here, reclaim it", A_RECLAIM, style="primary")),
            ])
        text = "⌛ Your turn passed and went to the next person."
        return text, compact([
            section("*⌛ Your turn passed* because it wasn't confirmed in time, so the charger went to the next "
                    "person."),
            actions(button("📋 Join the queue again", A_JOIN)),
        ])

    if kind == "charging":
        text = f"⚡ You're charging{on}. Done at {when(data['ends_at'])}."
        return text, compact([
            section(f"*⚡ You're charging{on}.* Done at {when(data['ends_at'])}."),
            context(f"I'll remind you {duration(cfg.warn_before_end_s)} before the end."
                    + (" A calendar reminder is on its way to your inbox." if cfg.gmail_ready
                       and state and state.wants_email(data.get('user_id', '')) else "")),
            actions(done_btn, dashboard_button(link)),
        ])

    if kind == "warn_end":
        text = f"⏳ {duration(cfg.warn_before_end_s)} left{on}."
        return text, compact([
            section(f"*⏳ {duration(cfg.warn_before_end_s)} left{on}.* Your session ends at {when(data['ends_at'])}."),
            actions(done_btn, button(f"➕ Extend {duration(cfg.extend_s)}", A_EXTEND)
                    if data.get("can_extend") else None),
        ])

    if kind == "time_up":
        nxt = data.get("next_user_id")
        tail = f" *{names(nxt)}* is up next." if nxt else ""
        text = f"🏁 Time's up{on}. Unplug and move your car."
        return text, compact([section(f"*🏁 Time's up{on}.* Unplug and move your car.{tail}"),
                              actions(unplugged_btn)])

    if kind == "nudge":
        who = names(data.get("from_user_id"))
        text = f"🔔 {who} is waiting for the charger."
        return text, compact([section(f"*🔔 {who} is waiting for the charger* and says your car is still "
                                      "plugged in. Could you move it?"), actions(unplugged_btn)])

    if kind == "next_up_soon":
        text = "🔜 You're up soon."
        return text, compact([section(f"*🔜 You're up soon.* The charger should free up around "
                                      f"{when(data['eta'])}. I'll DM you when it's yours."),
                              actions(button("Leave the queue", A_LEAVE))])

    if kind == "kicked":
        text = "🛑 An admin ended your charging session."
        return text, [section(f"🛑 {names(data.get('by'))} (admin) ended your charging session.")]

    if kind == "removed":
        text = "An admin removed you from the charging queue."
        return text, [section(f"{names(data.get('by'))} (admin) removed you from the charging queue.")]

    return kind, [section(kind)]


CLOSE_TEXT = {
    "confirmed": "✅ Confirmed at {at}.",
    "no_show": "⌛ Turn passed at {at}.",
    "skipped": "⏭ You skipped your turn.",
    "completed": "🏁 Session ended at {at}.",
    "done": "🏁 You ended your session at {at}. Thanks!",
    "ended_early": "🏁 You ended your session at {at}. Thanks!",
    "kicked": "🛑 Session ended by an admin.",
    "extended": "➕ Extended until {ends_at}.",
    "handed_over": "👍 The next person has plugged in. Thanks!",
    "unplugged": "👍 Thanks for unplugging!",
    "reclaimed": "🔁 Reclaimed at {at}.",
    "reclaim_expired": "Someone else has the charger now.",
}


def closed(reason: str, data: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    template = CLOSE_TEXT.get(reason, "Done.")
    text = template.format(at=when(data.get("at")), ends_at=when(data.get("ends_at")))
    return text, [context(text)]


# ---------------------------------------------------------------------------
# Replies to commands and buttons
# ---------------------------------------------------------------------------


def reply(code: str, data: Dict[str, Any], cfg: Config, state: Optional[State], names: Names) -> str:
    on = on_charger(cfg, state, data.get("charger_id"))
    eta = data.get("eta")
    eta_text = when(eta, "{date_short_pretty} at {time}") if eta else "as soon as the charger frees up"
    texts = {
        "offered": f"🔌 The charger is yours{on}. Plug in and confirm by {when(data.get('confirm_deadline'))}. "
                   "Your buttons are in our DM.",
        "queued": f"📋 You're *#{data.get('position')}* in line. Estimated turn: {eta_text}. "
                  "I'll DM you when it's yours.",
        "already_active": (f"⚡ You're already charging{on}." if data.get("confirmed")
                           else f"🔌 The charger is already reserved for you{on}. Confirm in our DM."),
        "already_queued": f"📋 You're already *#{data.get('position')}* in line. Estimated turn: {eta_text}.",
        "confirmed": f"⚡ Confirmed. Charging{on} until {when(data.get('ends_at'))}.",
        "already_confirmed": f"⚡ You're already confirmed. Charging until {when(data.get('ends_at'))}.",
        "not_offered": "It's not your turn yet. I'll DM you when it is.",
        "skipped": "⏭ Turn skipped. The next person has been notified.",
        "done": "🏁 Session ended. Thanks for unplugging!",
        "unplugged": "👍 Thanks for moving your car!",
        "nothing_to_end": ("You're in the queue, not charging. Use `/charge leave` to leave it."
                           if data.get("queued") else "You don't have an active session."),
        "nothing_to_unplug": "There's nothing to unplug.",
        "left": "👋 You've left the queue.",
        "not_queued": ("You're not in the queue; you have the charger. Use `/charge done` when you're finished."
                       if data.get("has_session") else "You're not in the queue."),
        "extended": f"➕ Extended until {when(data.get('ends_at'))}.",
        "queue_waiting": f"Can't extend: {plural(data.get('waiting', 1), 'person is', 'people are')} waiting.",
        "max_extensions": "You've reached the extension limit for this session.",
        "not_charging": "You're not charging right now.",
        "nudged": f"🔔 Nudged {names(data.get('target_user_id'))}.",
        "nudge_too_soon": f"You nudged recently. Try again in {max(1, mins(data.get('wait_s', 60)))} min.",
        "nothing_to_nudge": "There's nobody to nudge right now.",
        "reclaimed": f"🔁 It's yours again. Charging until {when(data.get('ends_at'))}.",
        "reclaim_expired": "Too late: someone else has the charger. Use `/charge` to join the queue.",
        "ended": f"Ended {names(data.get('user_id'))}'s session.",
        "no_session": "There's no session to end.",
        "removed": f"Removed {names(data.get('user_id'))} from the queue.",
        "moved": "Moved up.",
        "already_first": "Already first in line.",
        "out_of_order": "Marked out of order. Nobody will be offered the charger until it's back.",
        "back_in_service": "Back in service.",
        "no_charger": "Unknown charger.",
        "pref_saved": "Saved.",
        "not_admin": "Only ChargeBot admins can do that.",
    }
    return texts.get(code, code)


def my_dashboard(link: str, days: int) -> List[Dict[str, Any]]:
    return compact([
        section(f"📱 Here's your personal dashboard link. It signs you in, and the page then remembers you on "
                f"that device for {days} days."),
        actions(button("Open my dashboard", A_LINK, url=link, style="primary")),
        context("Installed the dashboard on your home screen? Use `/charge login` for a sign-in code instead."),
    ])


def login_code_text(code: str) -> str:
    return (f"🔑 Your dashboard sign-in code is *`{code}`*\n"
            "On the dashboard, tap *Sign in* and enter it within 10 minutes. It works once.")


def login_code_modal(code: str) -> Dict[str, Any]:
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Dashboard sign-in"},
        "close": {"type": "plain_text", "text": "Done"},
        "blocks": [
            section(f"Your sign-in code:\n\n*`{code}`*"),
            context("On the dashboard (or the Juice app on your home screen), tap *Sign in* and enter this code "
                    "within 10 minutes. It works once, and then that device stays signed in."),
        ],
    }


HELP = (
    "*⚡ ChargeBot*\n"
    "• `/charge`: claim the charger, or join the queue if it's busy\n"
    "• `/charge status`: who's charging and who's waiting\n"
    "• `/charge done`: end your session early (or say you've unplugged)\n"
    "• `/charge skip`: pass your turn to the next person\n"
    "• `/charge leave`: leave the queue\n"
    "• `/charge extend`: add time when nobody's waiting\n"
    "• `/charge dashboard`: your personal dashboard link\n"
    "• `/charge login`: a sign-in code for the dashboard app\n"
    "Tip: open ChargeBot's *Home* tab for live status and one-tap buttons."
)

ADMIN_HELP = (
    "*Admin*\n"
    "• `/charge admin`: open the admin panel\n"
    "• `/charge admin out <reason>` / `/charge admin back`: mark the charger out of order, or back in service"
)


# ---------------------------------------------------------------------------
# Status (ephemeral, channel message, modal, App Home)
# ---------------------------------------------------------------------------


def charger_lines(state: State, now: float, cfg: Config, names: Names) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for charger in state.chargers:
        label = f"*{charger.name}* · " if cfg.multi_charger else ""
        phase = charger_phase(charger, now)
        s = charger.session
        if phase == "out_of_order":
            line = f"{label}🛠 *Out of order*: {charger.out_of_order}"
        elif phase == "free":
            line = f"{label}🟢 *Free.* Claim it with `/charge`."
        elif phase == "offered":
            line = (f"{label}🟡 Waiting for *{names(s.user_id)}* to plug in "
                    f"(confirm by {when(s.confirm_deadline)})")
        else:
            left = mins((s.ends_at or now) - now)
            line = f"{label}⚡ *{names(s.user_id)}* is charging · {left} min left · done at {when(s.ends_at)}"
        blocks.append(section(line))
        if charger.unplug and phase != "charging":
            blocks.append(context(f"🚗 {names(charger.unplug.user_id)}'s car may still be plugged in."))
    return blocks


def queue_lines(state: State, now: float, cfg: Config, names: Names, viewer: Optional[str] = None) -> str:
    if not state.queue:
        return "📋 *Queue:* nobody's waiting."
    etas = queue_etas(cfg, state, now)
    rows = []
    for i, (entry, eta) in enumerate(zip(state.queue, etas)):
        who = "*you*" if entry.user_id == viewer else names(entry.user_id)
        est = f" · est. {when(eta)}" if eta else ""
        rows.append(f"{i + 1}. {who}{est}")
    return f"📋 *Queue ({len(state.queue)})*\n" + "\n".join(rows)


def viewer_buttons(state: State, cfg: Config, viewer: str, link: Optional[str] = None) -> Optional[Dict[str, Any]]:
    charger, session = state.session_of(viewer)
    btns: List[Optional[Dict[str, Any]]] = []
    if session is not None and not session.confirmed:
        btns += [button("✅ I'm plugged in", A_CONFIRM, style="primary"), button("⏭ Skip my turn", A_SKIP)]
        if charger.unplug:
            btns.append(button("🔔 Nudge", A_NUDGE))
    elif session is not None:
        btns.append(button("🏁 I'm done and unplugged", A_DONE))
        if not state.queue and session.extensions < cfg.max_extensions:
            btns.append(button(f"➕ Extend {duration(cfg.extend_s)}", A_EXTEND))
    elif state.queue_position(viewer) is not None:
        btns.append(button("👋 Leave the queue", A_LEAVE, style="danger"))
    else:
        if any(c.reclaim and c.reclaim.user_id == viewer and c.session is None for c in state.chargers):
            btns.append(button("🔁 Reclaim my turn", A_RECLAIM, style="primary"))
        free = state.free_chargers() and not state.queue
        btns.append(button("🔌 Claim the charger" if free else "📋 Join the queue", A_JOIN,
                           style=None if btns else "primary"))
    if any(c.unplug and c.unplug.user_id == viewer for c in state.chargers):
        btns.append(button("🔌 I've unplugged", A_UNPLUGGED))
    btns.append(dashboard_button(link))
    return actions(*btns)


def status_blocks(state: State, now: float, cfg: Config, names: Names, viewer: Optional[str] = None,
                  link: Optional[str] = None) -> List[Dict[str, Any]]:
    return compact([
        *charger_lines(state, now, cfg, names),
        section(queue_lines(state, now, cfg, names, viewer)),
        viewer_buttons(state, cfg, viewer, link) if viewer else None,
    ])


def live_message(state: State, now: float, cfg: Config, names: Names,
                 dashboard: Optional[str]) -> Tuple[str, List[Dict[str, Any]]]:
    title = "⚡ EV chargers" if cfg.multi_charger else "⚡ EV charger"
    blocks = compact([
        header(title),
        *charger_lines(state, now, cfg, names),
        section(queue_lines(state, now, cfg, names)),
        actions(button("🔌 Get in line", A_JOIN, style="primary"),
                button("📱 My dashboard", A_MY_DASHBOARD) if dashboard else None),
        context(f"Live status, updated {when(now)}. Use `/charge help` for commands."),
    ])
    summary = "; ".join(f"{c.name}: {charger_phase(c, now)}" for c in state.chargers)
    return f"{title}: {summary}; {len(state.queue)} waiting", blocks


def panel_modal(state: State, now: float, cfg: Config, names: Names, viewer: str,
                link: Optional[str], notice: Optional[str] = None) -> Dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": PANEL_CALLBACK,
        "title": {"type": "plain_text", "text": "EV charger"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": compact([context(notice) if notice else None,
                           *status_blocks(state, now, cfg, names, viewer, link)]),
    }


def home_view(state: State, now: float, cfg: Config, names: Names, viewer: str, link: Optional[str],
              session_log: List[Dict[str, Any]], notice: Optional[str] = None) -> Dict[str, Any]:
    stats = user_stats(session_log, viewer)
    email_on = state.wants_email(viewer)
    blocks: List[Optional[Dict[str, Any]]] = [
        header("⚡ EV chargers" if cfg.multi_charger else "⚡ EV charger"),
        context(notice) if notice else None,
        *charger_lines(state, now, cfg, names),
        section(queue_lines(state, now, cfg, names, viewer)),
        viewer_buttons(state, cfg, viewer, link),
        context(f"Updated {when(now, '{time}')}. This page refreshes when something changes."),
        divider(),
        section("*Your settings*"),
    ]
    if cfg.gmail_ready:
        blocks.append(section(
            "📅 *Calendar reminder by email* when your session starts: " + ("*on*" if email_on else "*off*"),
            button("Turn off" if email_on else "Turn on", A_PREF_EMAIL, value="off" if email_on else "on")))
    if cfg.public_base_url:
        blocks.append(section(
            f"🔑 *Dashboard sign-in:* the dashboard remembers you on each phone or computer for "
            f"{cfg.device_link_days} days. Using the app on your home screen? Sign it in with a code."))
        blocks.append(actions(
            button("Get sign-in code", A_LOGIN_CODE),
            button("Sign out everywhere", A_SIGNOUT_ALL, confirm=confirm_dialog(
                "Sign out everywhere?", "Every dashboard you've signed in to will need to sign in again.",
                "Sign out"))))
    if stats["sessions"]:
        blocks.append(context(
            f"Your stats: {plural(stats['sessions'], 'session', 'sessions')} · "
            f"average wait {stats['avg_wait_min']} min · {plural(stats['no_shows'], 'no-show', 'no-shows')}"))
    else:
        blocks.append(context("No sessions yet. Your stats will show up here."))
    if cfg.is_admin(viewer):
        blocks += [divider(), section("*Admin*", button("Open admin panel", A_ADMIN_OPEN))]
    blocks.append(context("Commands: `/charge`, `/charge status`, `/charge done`, `/charge leave`, `/charge help`"))
    return {"type": "home", "blocks": compact(blocks)}


def admin_modal(state: State, now: float, cfg: Config, names: Names, notice: Optional[str] = None) -> Dict[str, Any]:
    blocks: List[Optional[Dict[str, Any]]] = [context(notice) if notice else None]
    for charger in state.chargers:
        s = charger.session
        label = charger.name if cfg.multi_charger else "Charger"
        if s:
            state_text = f"{names(s.user_id)} · {'charging' if s.confirmed else 'waiting to plug in'}"
        else:
            state_text = "free"
        if charger.out_of_order:
            state_text += f" · 🛠 out of order ({charger.out_of_order})"
        blocks.append(section(f"*{label}*: {state_text}"))
        blocks.append(actions(
            button("End session", A_ADMIN_END, value=charger.id, style="danger",
                   confirm=confirm_dialog("End session?", f"End {names(s.user_id)}'s session now?", "End it"))
            if s else None,
            button("Back in service", A_ADMIN_OOO, value=f"back:{charger.id}") if charger.out_of_order
            else button("Mark out of order", A_ADMIN_OOO, value=f"out:{charger.id}"),
        ))
    blocks.append(divider())
    if not state.queue:
        blocks.append(section("*Queue*: empty"))
    else:
        blocks.append(section("*Queue*"))
        for i, entry in enumerate(state.queue):
            options = []
            if i > 0:
                options.append({"text": {"type": "plain_text", "text": "Move up"}, "value": f"up:{entry.user_id}"})
            options.append({"text": {"type": "plain_text", "text": "Remove"}, "value": f"remove:{entry.user_id}"})
            blocks.append(section(f"{i + 1}. {names(entry.user_id)}",
                                  {"type": "overflow", "action_id": A_ADMIN_QUEUE, "options": options}))
    blocks.append(context("Custom out-of-order reason: `/charge admin out <reason>`"))
    return {
        "type": "modal",
        "callback_id": ADMIN_CALLBACK,
        "title": {"type": "plain_text", "text": "Charger admin"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": compact(blocks),
    }
