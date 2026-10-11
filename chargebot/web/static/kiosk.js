/* Kiosk / TV mode: glanceable from across the room. ?k=<KIOSK_KEY> enables the confirm button. */
(function () {
  "use strict";
  const { el } = CB;
  CB.initTheme("dark", false);
  const key = CB.takeParam("k");
  const kmain = document.getElementById("kmain");
  const kside = document.getElementById("kside");
  let lastKey = "";

  async function confirmFor(chargerId, button) {
    button.disabled = true;
    try {
      const res = await CB.post({ action: "confirm", charger_id: chargerId, k: key });
      CB.toast(res.ok ? "Charging! Enjoy the juice." : CB.message(res.code, false));
    } catch (e) {
      CB.toast("Couldn't reach ChargeBot.");
    }
    refresh();
  }

  function chargerView(c, state, compact) {
    const wrap = el("div", { class: compact ? "card kcard" : "" });
    if (state.multi) wrap.append(el("div", { class: "label", style: "font-weight:700;margin-bottom:6px", text: c.name }));
    if (c.phase === "charging") {
      wrap.append(
        el("span", { class: "chip charging", text: "Charging" }),
        el("div", { class: "kname", style: "margin-top:1vh", text: c.user.name }),
        el("div", { class: "ktime", "data-until": c.ends_at }),
        el("div", { class: "battery" }, el("div", { class: "fill", "data-from": c.charge_start, "data-to": c.ends_at })),
        el("div", { class: "label", style: "margin-top:1vh", text: "Done at " + CB.clock(c.ends_at) }));
    } else if (c.phase === "offered") {
      wrap.append(
        el("span", { class: "chip offered", text: "Plug in now" }),
        el("div", { class: "kname", style: "margin-top:1vh", text: c.user.name + ", it's your turn" }),
        el("div", { class: "ktime pink", "data-until": c.confirm_deadline }),
        el("div", { class: "label", text: "left to confirm" }),
        state.kiosk_can_confirm ? el("div", { class: "btns", style: "justify-content:flex-start" },
          el("button", { type: "button", class: "btn primary kbtn", text: "⚡ I'm plugged in",
            onclick: (e) => confirmFor(c.id, e.currentTarget) })) : null);
    } else if (c.phase === "out_of_order") {
      wrap.append(el("span", { class: "chip out_of_order", text: "Out of order" }),
        el("div", { class: "kname", style: "margin-top:1vh;white-space:normal", text: c.out_of_order || "Taking a break" }));
    } else {
      wrap.append(el("span", { class: "chip free", text: "Free" }),
        el("div", { class: "ktime", style: "font-family:var(--font-display);font-weight:800", text: "Free to plug" }),
        el("div", { class: "label", text: "Type /charge in Slack to grab it." }));
    }
    if (c.unplug_user && c.phase !== "charging") {
      wrap.append(el("div", { class: "label", style: "margin-top:1.5vh",
        text: "🚗 " + c.unplug_user.name + "'s car may still be plugged in." }));
    }
    return wrap;
  }

  function sideView(state) {
    const nodes = [];
    const [first, second] = state.queue;
    nodes.push(el("section", { class: "card kcard" },
      el("div", { class: "label", text: "Up next" }),
      first
        ? el("div", { class: "who", style: "margin-top:1vh" }, CB.avatar(first.user), el("span", { class: "val", text: first.user.name }))
        : el("div", { class: "val", style: "margin-top:.5vh", text: "Nobody's waiting" }),
      second ? el("div", { class: "label", style: "margin-top:1.5vh", text: "then " + second.user.name +
        (state.queue.length > 2 ? ` and ${state.queue.length - 2} more` : "") }) : null));

    const qr = el("div", { class: "qr", id: "qr", "aria-hidden": "true" });
    nodes.push(el("section", { class: "card kcard", style: "display:flex;gap:1.5vw;align-items:center" },
      state.slack_url ? qr : null,
      el("div", {}, el("div", { class: "val", text: "Want in?" }),
        el("div", { class: "label", text: state.slack_url ? "Scan to open ChargeBot in Slack, or type /charge" : "Type /charge in Slack" }))));
    return { nodes, qr, url: state.slack_url };
  }

  function render(state) {
    const k = JSON.stringify([state.version, state.kiosk_can_confirm]);
    if (k === lastKey) return;
    lastKey = k;
    if (state.chargers.length === 1) {
      kmain.replaceChildren(chargerView(state.chargers[0], state, false));
    } else {
      kmain.replaceChildren(el("div", { class: "kgrid" }, state.chargers.map((c) => chargerView(c, state, true))));
    }
    const side = sideView(state);
    kside.replaceChildren(...side.nodes);
    if (side.url && window.QRCode) {
      new QRCode(side.qr, { text: side.url, width: 256, height: 256, correctLevel: QRCode.CorrectLevel.M });
    }
    CB.tickLive();
  }

  const refresh = CB.poll(async () => render(await CB.getJSON("/api/state", { k: key })), 5000,
    (age) => CB.liveLabel(document.getElementById("live"), age));
  setInterval(CB.tickLive, 1000);

  /* Nudge the whole layout a few pixels every minute so static pixels don't burn in. */
  const kiosk = document.getElementById("kiosk");
  setInterval(() => {
    const dx = Math.round(Math.random() * 8 - 4), dy = Math.round(Math.random() * 8 - 4);
    kiosk.style.transform = `translate(${dx}px, ${dy}px)`;
  }, 60000);
  /* Reload a few times a day to pick up new versions of the page. */
  setTimeout(() => location.reload(), 6 * 3600 * 1000);
})();
