/* Main dashboard: charger cards, "your turn" hero, queue, and personal actions via magic link. */
(function () {
  "use strict";
  const { el } = CB;
  CB.initTheme("auto");
  const token = CB.takeParam("t");
  const main = document.getElementById("main");
  const foot = document.getElementById("foot");
  const banner = document.getElementById("banner");
  let lastKey = "";
  let busy = false;

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
    const [label, style] = LABELS[action];
    return el("button", {
      type: "button", class: "btn " + style, text: label,
      onclick: async (event) => {
        if (busy) return;
        busy = true;
        event.currentTarget.disabled = true;
        try {
          const res = await CB.post({ action, t: token });
          CB.toast(CB.message(res.code, res.ok));
        } catch (e) {
          CB.toast("Couldn't reach ChargeBot. Try again.");
        }
        busy = false;
        refresh();
      },
    });
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

    foot.replaceChildren(state.me
      ? "Signed in from your Slack link. It works for 24 hours."
      : el("span", {}, "Want in? Type ", el("b", { class: "mono", text: "/charge" }), " in Slack",
          state.slack_url ? el("span", {}, " or ", el("a", { href: state.slack_url, text: "open ChargeBot" })) : null, "."));
    CB.tickLive();
  }

  async function load() {
    render(await CB.getJSON("/api/state", { t: token }));
  }

  const refresh = CB.poll(load, 5000, (age) => {
    CB.liveLabel(document.getElementById("live"), age);
    banner.replaceChildren(age > 30 && age !== Infinity
      ? el("div", { class: "banner err", text: "Can't reach ChargeBot right now. The numbers below may be out of date." })
      : "");
  });
  setInterval(CB.tickLive, 1000);
})();
