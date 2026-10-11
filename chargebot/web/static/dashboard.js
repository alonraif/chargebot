/* Main dashboard: charger cards, "your turn" hero, queue, and personal actions via magic link. */
(function () {
  "use strict";
  const { el } = CB;
  CB.initTheme("auto");
  const signingIn = CB.signIn();
  const main = document.getElementById("main");
  const foot = document.getElementById("foot");
  const banner = document.getElementById("banner");
  const signinBox = document.getElementById("signin");
  const signinBtn = document.getElementById("signinBtn");
  const notifyBox = document.getElementById("notify");
  let notifyKey = "";
  let lastKey = "";
  let busy = false;
  let expired = false;
  let chargerFree = false;

  const LABELS = {
    confirm: ["⚡ I'm plugged in", "primary"],
    skip: ["Skip my turn", ""],
    nudge: ["🔔 Nudge previous driver", ""],
    done: ["🏁 I'm done and unplugged", ""],
    extend: ["➕ Extend", ""],
    leave: ["Leave the queue", "danger"],
    join: ["🔌 Get in line", "primary"],
    unplugged: ["🔌 I've unplugged", ""],
    reclaim: ["🔁 Reclaim my turn", "primary"],
  };

  function actionButton(action) {
    let [label, style] = LABELS[action];
    if (action === "join" && chargerFree) label = "🔌 Claim the charger";
    return el("button", {
      type: "button", class: "btn " + style, text: label,
      onclick: async (event) => {
        if (busy) return;
        busy = true;
        event.currentTarget.disabled = true;
        try {
          const res = await CB.post({ action });
          CB.toast(CB.message(res.code, res.ok));
        } catch (e) {
          CB.toast("Couldn't reach ChargeBot. Try again.");
        }
        busy = false;
        refresh();
      },
    });
  }

  function openSignIn() {
    if (signinBox.firstChild) {
      signinBox.querySelector("input").focus();
      return;
    }
    const input = el("input", { type: "text", inputmode: "text", autocomplete: "one-time-code", autocapitalize: "characters",
      spellcheck: "false", maxlength: "9", placeholder: "K7Q-4ZP", "aria-label": "Sign-in code" });
    const err = el("p", { class: "err", role: "alert" });
    const submit = el("button", { type: "submit", class: "btn primary", text: "Sign in" });
    const form = el("form", { class: "card signin", "aria-label": "Sign in" },
      el("h2", { text: "Sign in with a code" }),
      el("p", { class: "muted", style: "margin:0" }, "In Slack, open ChargeBot's ", el("b", { text: "Home" }),
        " tab and tap ", el("b", { text: "Get sign-in code" }), ", or type ", el("b", { class: "mono", text: "/charge login" }), "."),
      input, err,
      el("div", { class: "btns" }, submit,
        el("button", { type: "button", class: "btn", text: "Cancel", onclick: () => signinBox.replaceChildren() })));
    input.addEventListener("input", () => { err.textContent = ""; });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const code = input.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
      if (code.length !== 6) {
        err.textContent = "Enter the 6-character code from Slack.";
        input.focus();
        return;
      }
      submit.disabled = true;
      try {
        const res = await CB.signInWithCode(code);
        if (res.ok) {
          signinBox.replaceChildren();
          expired = false;
          CB.linkStatus = "ok";
          lastKey = "";
          CB.toast("Signed in. This device will remember you.");
          refresh();
          return;
        }
        err.textContent = res.code === "too_many_attempts"
          ? "Too many tries. Wait a few minutes, then try again."
          : "That code didn't work. Codes last 10 minutes and work once, so get a fresh one in Slack.";
      } catch (e) {
        err.textContent = "Couldn't reach ChargeBot. Check your connection and try again.";
      }
      submit.disabled = false;
      input.select();
    });
    signinBox.replaceChildren(form);
    input.focus();
  }
  if (signinBtn) signinBtn.addEventListener("click", openSignIn);

  async function renderNotify(state) {
    const support = state.me && state.push_key ? CB.pushSupport() : "off";
    const sub = support === "ok" ? await CB.pushSubscription().catch(() => null) : null;
    const dismissed = CB.store.get("localStorage", "cb-push-dismissed") === "1";
    const key = [support, !!sub, dismissed].join("|");
    if (key === notifyKey) return;
    notifyKey = key;

    if (support === "ok" && sub) {
      notifyBox.replaceChildren(el("p", { class: "notify-line" }, "🔔 Notifications are on for this device. ",
        el("a", { href: "#", text: "Turn off", onclick: async (e) => {
          e.preventDefault();
          await CB.disablePush().catch(() => null);
          CB.toast("Notifications off for this device.");
          notifyKey = "";
          renderNotify(state);
        } })));
      return;
    }
    if (dismissed || support === "off" || support === "unsupported") {
      notifyBox.replaceChildren(dismissed && support === "ok"
        ? el("p", { class: "notify-line" }, el("a", { href: "#", text: "🔔 Turn on notifications", onclick: (e) => {
            e.preventDefault();
            CB.store.remove("localStorage", "cb-push-dismissed");
            notifyKey = "";
            renderNotify(state);
          } }))
        : "");
      return;
    }
    const later = el("button", { type: "button", class: "btn", text: "Not now", onclick: () => {
      CB.store.set("localStorage", "cb-push-dismissed", "1");
      notifyKey = "";
      renderNotify(state);
    } });
    let body, buttons;
    if (support === "ios-install") {
      body = el("p", { class: "muted", style: "margin:0" }, "On iPhone, notifications work in the Juice app: in Safari, tap ",
        el("b", { text: "Share" }), " → ", el("b", { text: "Add to Home Screen" }),
        ", open Juice from your Home Screen, sign in, and turn notifications on there.");
      buttons = [later];
    } else if (support === "denied") {
      body = el("p", { class: "muted", style: "margin:0" },
        "Notifications are blocked for this site. Allow them in your browser's site settings, then come back here.");
      buttons = [later];
    } else {
      body = el("p", { class: "muted", style: "margin:0" },
        "Get your turn, the reminders and time's up on this device, even when the app is closed.");
      const on = el("button", { type: "button", class: "btn primary", text: "🔔 Turn on notifications", onclick: async () => {
        on.disabled = true;
        let res;
        try {
          res = await CB.enablePush(state.push_key);
        } catch (e) {
          res = { ok: false };
        }
        CB.toast(res.ok ? "Notifications on. A test one is on its way."
          : res.code === "denied" ? "Notifications are blocked for this site."
          : res.code === "dismissed" ? "No problem. You can turn them on any time."
          : "Couldn't turn on notifications. Try again.");
        notifyKey = "";
        renderNotify(state);
      } });
      buttons = [on, later];
    }
    notifyBox.replaceChildren(el("section", { class: "card signin", "aria-label": "Notifications" },
      el("h2", { text: "🔔 Get notified on this device" }), body, el("div", { class: "btns" }, buttons)));
  }

  function personRow(person, sub) {
    return el("div", { class: "who" }, CB.avatar(person),
      el("div", { style: "min-width:0" }, el("b", { text: person.is_me ? "You" : person.name }),
        sub ? el("div", { class: "muted small", text: sub }) : null));
  }

  function chargerCard(c, state) {
    const card = el("section", { class: "card", "aria-label": c.name });
    if (state.multi) card.append(el("div", { class: "charger-name", text: c.name }));

    if (c.phase === "charging") {
      const left = el("div", {},
        el("span", { class: "chip charging", text: "Charging" }),
        personRow(c.user, "plugged in at " + CB.clock(c.charge_start)));
      const right = el("div", {},
        el("div", { class: "big-time", "data-until": c.ends_at }),
        el("div", { class: "muted small", style: "text-align:right", text: "left · done at " + CB.clock(c.ends_at) }));
      const labelId = "pct-" + c.id;
      card.append(
        el("div", { class: "charger-head" }, left, right),
        el("div", { class: "battery", role: "img", "aria-label": "Session progress" },
          el("div", { class: "fill", "data-from": c.charge_start, "data-to": c.ends_at, "data-label": labelId })),
        el("div", { class: "scale" }, el("span", { text: "0" }), el("span", { id: labelId }),
          el("span", { text: CB.minutes(c.ends_at - c.charge_start) + " min" })));
    } else if (c.phase === "offered") {
      card.append(
        el("div", { class: "charger-head" },
          el("div", {}, el("span", { class: "chip offered", text: c.user.is_me ? "Your turn" : "Waiting to plug in" }),
            personRow(c.user, "has until " + CB.clock(c.confirm_deadline) + " to confirm")),
          el("div", {}, el("div", { class: "big-time", "data-until": c.confirm_deadline }),
            el("div", { class: "muted small", style: "text-align:right", text: "to confirm" }))),
        el("div", { class: "bar" }, el("i", {
          "data-from": c.offered_at, "data-to": c.confirm_deadline, "data-invert": "1" })));
    } else if (c.phase === "out_of_order") {
      card.append(el("span", { class: "chip out_of_order", text: "Out of order" }),
        el("p", { style: "font-size:20px;font-weight:800;margin:12px 0 0", text: c.out_of_order || "Taking a break" }));
    } else {
      card.append(
        el("div", { class: "charger-head" },
          el("div", {}, el("span", { class: "chip free", text: "Free" }),
            el("p", { style: "font-size:26px;font-weight:800;margin:10px 0 0;letter-spacing:-.02em", text: "Free to plug" })),
          el("div", { class: "muted small", style: "text-align:right;max-width:220px",
            text: "Type /charge in Slack to grab it." })),
        el("div", { class: "battery empty" }, el("div", { class: "fill", style: "width:0" })));
    }
    if (c.unplug_user && c.phase !== "charging") {
      card.append(el("p", { class: "muted small", style: "margin:12px 0 0",
        text: "🚗 " + (c.unplug_user.is_me ? "Your" : c.unplug_user.name + "'s") + " car may still be plugged in." }));
    }
    return card;
  }

  function heroCard(c, actions) {
    return el("section", { class: "card hero", "aria-label": "Your turn" },
      el("span", { class: "chip offered", text: "Your turn" }),
      el("h2", { text: "Plug in and tap the button" }),
      el("div", { class: "muted" }, "Confirm within ",
        el("span", { class: "mono", style: "color:var(--pink);font-weight:700;font-size:18px", "data-until": c.confirm_deadline }),
        " to keep your spot"),
      el("div", { class: "bar" }, el("i", { "data-from": c.offered_at, "data-to": c.confirm_deadline, "data-invert": "1" })),
      el("div", { class: "btns" }, actions.filter((a) => ["confirm", "skip", "nudge"].includes(a)).map(actionButton)));
  }

  function queueCard(state) {
    const card = el("section", { class: "card", "aria-label": "Queue" },
      el("div", { class: "queue-head" }, el("h2", { text: "Up next" }),
        el("span", { class: "chip waiting", text: state.queue.length ? state.queue.length + " waiting" : "nobody waiting" })));
    if (!state.queue.length) {
      card.append(el("div", { class: "empty", text: "No line. A free charger is a happy charger." }));
      return card;
    }
    const myLeave = state.me && state.me.actions.includes("leave");
    for (const q of state.queue) {
      const eta = q.eta ? "around " + CB.clock(q.eta) : "when a charger frees up";
      card.append(el("div", { class: "qrow" + (q.user.is_me ? " me" : "") },
        el("span", { class: "pos", text: String(q.position).padStart(2, "0") }),
        CB.avatar(q.user, true),
        el("div", { class: "grow" }, el("b", { text: q.user.is_me ? "You" : q.user.name }),
          el("span", { class: "muted small", text: (q.user.is_me ? "your turn " : "") + eta })),
        q.user.is_me && myLeave ? actionButton("leave") : null));
    }
    return card;
  }

  function render(state) {
    const key = JSON.stringify([state.version, state.me, state.chargers.map((c) => c.phase)]);
    if (key === lastKey) return;
    lastKey = key;
    chargerFree = state.chargers.some((c) => c.phase === "free") && !state.queue.length;

    const actions = (state.me && state.me.actions) || [];
    const mine = state.chargers.find((c) => c.user && c.user.is_me && c.phase === "offered");
    const nodes = [];
    const hero = mine && actions.includes("confirm");
    if (hero) nodes.push(heroCard(mine, actions));
    // With one charger the hero already says everything the charger card would.
    const cards = hero && !state.multi ? [] : state.chargers.map((c) => chargerCard(c, state));
    if (cards.length) nodes.push(el("div", { class: "grid" }, cards));

    const extra = actions.filter((a) => !["confirm", "skip", "nudge", "leave"].includes(a));
    if (state.me && extra.length) {
      nodes.push(el("section", { class: "card", "aria-label": "Your actions" },
        el("div", { class: "who", style: "margin:0" }, CB.avatar(state.me.person),
          el("b", { text: "Hi " + state.me.person.name.split(" ")[0] + "!" })),
        el("div", { class: "btns", style: "justify-content:flex-start" }, extra.map(actionButton))));
    }
    nodes.push(queueCard(state));
    main.replaceChildren(...nodes);

    const signOut = el("a", { href: "#", text: "Sign out", onclick: (e) => {
      e.preventDefault();
      CB.signOut();
      CB.store.remove("localStorage", "cb-last");
      lastKey = "";
      refresh();
    } });
    foot.replaceChildren(state.me
      ? el("span", {}, `Signed in as ${state.me.person.name} on this device. `, signOut)
      : el("span", {},
          CB.linkStatus === "rejected" || CB.linkStatus === "shown"
            ? el("b", { text: "That Slack link has expired or is no longer valid. " })
            : expired ? "Your sign-in expired. " : "",
          "To get your buttons here, ",
          el("a", { href: "#", text: "sign in with a code", onclick: (e) => { e.preventDefault(); openSignIn(); } }),
          " from ChargeBot in Slack, or type ", el("b", { class: "mono", text: "/charge" }), " in Slack",
          state.slack_url ? el("span", {}, " (", el("a", { href: state.slack_url, text: "open ChargeBot" }), ")") : null,
          "."));
    if (signinBtn) signinBtn.hidden = !!state.me;
    CB.tickLive();
  }

  // Show the last known status right away, so opening the app never starts from a blank page.
  try {
    const last = JSON.parse(CB.store.get("localStorage", "cb-last") || "null");
    if (last && last.chargers) render(last);
  } catch (e) { /* ignore a bad saved copy */ }

  async function load() {
    await signingIn;
    const state = await CB.getJSON("/api/state");
    CB.store.set("localStorage", "cb-last", JSON.stringify(state));
    if (CB.token && !state.me) {
      // The pass expired or was signed out from Slack.
      CB.signOut();
      expired = true;
    }
    render(state);
    renderNotify(state);
    if (CB.linkStatus === "rejected" && !state.me && !signinBox.firstChild) {
      CB.linkStatus = "shown";
      openSignIn();
    }
  }

  const refresh = CB.poll(load, 5000, (age) => {
    CB.liveLabel(document.getElementById("live"), age);
    banner.replaceChildren(age > 30 && age !== Infinity
      ? el("div", { class: "banner err", text: "Can't reach ChargeBot right now. The numbers below may be out of date." })
      : "");
  });
  setInterval(CB.tickLive, 1000);
})();
