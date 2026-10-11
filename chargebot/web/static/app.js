/* Shared helpers for the ChargeBot pages. Everything user-provided goes through textContent. */
(function () {
  "use strict";
  const CB = {};
  const params = new URLSearchParams(location.search);

  CB.store = {
    get(store, key) { try { return window[store].getItem(key); } catch (e) { return null; } },
    set(store, key, value) { try { window[store].setItem(key, value); } catch (e) { /* storage blocked */ } },
  };

  CB.el = function (tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "style") node.setAttribute("style", value);
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  };

  /* Theme: auto (system), light or dark; remembered per browser. */
  CB.initTheme = function (fallback, remembered = true) {
    const forced = params.get("theme");
    let choice = forced || (remembered && CB.store.get("localStorage", "cb-theme")) || fallback || "auto";
    const apply = (value) => {
      if (value === "light" || value === "dark") document.documentElement.setAttribute("data-theme", value);
      else document.documentElement.removeAttribute("data-theme");
    };
    apply(choice);
    const seg = document.getElementById("themeSeg");
    if (!seg) return;
    const options = [["auto", "Auto"], ["light", "Light"], ["dark", "Dark"]];
    seg.replaceChildren(...options.map(([value, label]) => CB.el("button", {
      type: "button", text: label, "aria-pressed": String(value === choice),
      onclick: () => {
        choice = value;
        CB.store.set("localStorage", "cb-theme", value);
        apply(value);
        for (const b of seg.children) b.setAttribute("aria-pressed", String(b.textContent === label));
      },
    })));
  };

  /* Read a secret from the URL once, keep it for this tab, and remove it from the address bar. */
  CB.takeParam = function (name) {
    let value = params.get(name);
    if (value) {
      CB.store.set("sessionStorage", "cb-" + name, value);
      params.delete(name);
      const qs = params.toString();
      history.replaceState(null, "", location.pathname + (qs ? "?" + qs : "") + location.hash);
    } else {
      value = CB.store.get("sessionStorage", "cb-" + name);
    }
    return value || null;
  };

  /* Server clock. */
  let offset = 0;
  CB.now = () => Date.now() / 1000 + offset;

  CB.getJSON = async function (path, query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query || {})) if (v) qs.set(k, v);
    const res = await fetch(path + (qs.toString() ? "?" + qs : ""), { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (typeof data.now === "number") offset = data.now - Date.now() / 1000;
    return data;
  };

  CB.post = async function (body) {
    const res = await fetch("/api/action", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* empty body */ }
    return data;
  };

  /* Formatting. */
  CB.clock = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  CB.countdown = function (seconds) {
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
  };
  CB.minutes = (seconds) => Math.max(0, Math.round(seconds / 60));

  CB.avatar = function (person, small) {
    const node = CB.el("span", { class: "ava" + (small ? " sm" : ""), style: `--h:${person.hue}`, "aria-hidden": "true" });
    const initials = () => { node.textContent = person.initials; };
    if (person.avatar) {
      const img = CB.el("img", { src: person.avatar, alt: "", loading: "lazy", referrerpolicy: "no-referrer" });
      img.addEventListener("error", initials, { once: true });
      node.append(img);
    } else {
      initials();
    }
    return node;
  };

  /* Live elements: [data-until] shows a countdown, [data-from][data-to] sets width as progress. */
  CB.tickLive = function () {
    const now = CB.now();
    document.querySelectorAll("[data-until]").forEach((node) => {
      node.textContent = CB.countdown(Number(node.dataset.until) - now);
    });
    document.querySelectorAll("[data-from][data-to]").forEach((node) => {
      const from = Number(node.dataset.from), to = Number(node.dataset.to);
      let pct = to > from ? (now - from) / (to - from) : 1;
      if (node.dataset.invert) pct = 1 - pct;
      node.style.width = Math.max(0, Math.min(100, pct * 100)).toFixed(1) + "%";
      const label = node.dataset.label && document.getElementById(node.dataset.label);
      if (label) label.textContent = Math.round(Math.max(0, Math.min(1, pct)) * 100) + "% of the session";
    });
  };

  let toastTimer = null;
  CB.toast = function (message) {
    const node = document.getElementById("toast");
    if (!node) return;
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove("show"), 3200);
  };

  CB.MESSAGES = {
    offered: "The charger is yours! Plug in and confirm.",
    queued: "You're in line. We'll ping you in Slack.",
    confirmed: "Charging! Enjoy the juice.",
    skipped: "Turn skipped. The next person got it.",
    done: "Session ended. Thanks for unplugging!",
    unplugged: "Thanks for moving your car!",
    left: "You left the queue.",
    extended: "Extended. More juice for you.",
    nudged: "Nudge sent.",
    reclaimed: "It's yours again!",
    already_active: "You already have the charger.",
    already_queued: "You're already in line.",
    already_confirmed: "You're already charging.",
    not_offered: "It's not your turn right now.",
    queue_waiting: "Can't extend while people are waiting.",
    max_extensions: "You've hit the extension limit.",
    not_charging: "You're not charging right now.",
    nudge_too_soon: "You nudged recently. Give them a minute.",
    nothing_to_nudge: "There's nobody to nudge.",
    reclaim_expired: "Too late: someone else has it.",
    nothing_to_end: "Nothing to end.",
    not_queued: "You're not in the queue.",
    bad_token: "This link has expired. Open a fresh one from Slack.",
    forbidden: "That kiosk key isn't right.",
  };
  CB.message = (code, ok) => CB.MESSAGES[code] || (ok ? "Done." : "Something went wrong. Try again.");

  /* Poll while the tab is visible; report freshness through onStatus(ok, ageSeconds). */
  CB.poll = function (load, intervalMs, onStatus) {
    let lastOk = 0, timer = null;
    const run = async () => {
      clearTimeout(timer);
      try {
        await load();
        lastOk = Date.now();
      } catch (e) {
        console.warn("Refresh failed", e);
      }
      onStatus(lastOk ? (Date.now() - lastOk) / 1000 : Infinity);
      timer = setTimeout(run, document.hidden ? intervalMs * 6 : intervalMs);
    };
    document.addEventListener("visibilitychange", () => { if (!document.hidden) run(); });
    setInterval(() => onStatus(lastOk ? (Date.now() - lastOk) / 1000 : Infinity), 1000);
    run();
    return run;
  };

  CB.liveLabel = function (node, age) {
    if (!node) return;
    const stale = age > 20;
    node.classList.toggle("stale", stale);
    const text = age === Infinity ? "Connecting…" : stale ? "Reconnecting…" : `Live · ${Math.round(age)}s ago`;
    node.replaceChildren(CB.el("i"), text);
  };

  window.CB = CB;
})();
