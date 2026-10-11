/* Usage stats: KPIs, sessions per day, and a weekday x hour heatmap. */
(function () {
  "use strict";
  const { el } = CB;
  CB.initTheme("auto");
  const main = document.getElementById("main");
  // Python weekday numbers (Mon=0); shown Sunday-first.
  const DAYS = [[6, "Sun"], [0, "Mon"], [1, "Tue"], [2, "Wed"], [3, "Thu"], [4, "Fri"], [5, "Sat"]];
  const HOURS = Array.from({ length: 16 }, (_, i) => i + 6); // 06:00-21:00

  function kpi(cls, value, label) {
    return el("div", { class: "card kpi " + cls }, el("div", { class: "num", text: value }), el("div", { class: "lbl", text: label }));
  }

  function barChart(perDay) {
    const counts = perDay.map((d) => d.count);
    const peak = Math.max(0, ...counts);
    const top = Math.max(2, peak + (peak % 2)); // even number so the middle gridline is a whole number
    const active = counts.filter((c) => c > 0);
    const avg = active.length ? (active.reduce((a, b) => a + b, 0) / active.length).toFixed(1) : "0";
    const date = (iso, opts) => new Date(iso + "T12:00:00").toLocaleDateString([], opts);
    const long = (d) => `${date(d.date, { weekday: "short", month: "short", day: "numeric" })}: ` +
      (d.count === 1 ? "1 session" : `${d.count} sessions`);

    const readout = el("div", { class: "readout", "aria-live": "polite" });
    const showDefault = () => readout.replaceChildren(
      el("b", { text: `Peak ${peak} a day` }), ` · ${avg} on an average day with charging. Tap a bar for details.`);
    showDefault();

    const bars = el("div", { class: "bars", role: "list", "aria-label": `Sessions per day, last ${perDay.length} days` });
    for (const d of perDay) {
      const cls = d.count === 0 ? "b zero" : d.count === peak ? "b peak" : "b";
      const show = () => {
        bars.querySelectorAll(".col.on").forEach((n) => n.classList.remove("on"));
        col.classList.add("on");
        readout.replaceChildren(el("b", { text: long(d) }));
      };
      const col = el("button", { type: "button", class: "col", role: "listitem", "aria-label": long(d),
        onmouseenter: show, onfocus: show, onclick: show },
        el("span", { class: "v", text: d.count ? String(d.count) : "" }),
        el("span", { class: cls, style: `height:calc((100% - 16px) * ${d.count / top})` }));
      bars.append(col);
    }
    bars.addEventListener("mouseleave", () => {
      bars.querySelectorAll(".col.on").forEach((n) => n.classList.remove("on"));
      showDefault();
    });

    const mid = Math.round(perDay.length / 2);
    return el("section", { class: "card" },
      el("h2", { text: "Sessions per day" }),
      el("div", { class: "chart" },
        el("div", { class: "yaxis", "aria-hidden": "true" },
          el("span", { text: String(top) }), el("span", { text: String(top / 2) }), el("span", { text: "0" })),
        el("div", { class: "plot" },
          el("i", { class: "grid g1" }), el("i", { class: "grid g2" }), bars)),
      el("div", { class: "axis" },
        el("span", { text: date(perDay[0].date, { month: "short", day: "numeric" }) }),
        el("span", { text: date(perDay[mid].date, { month: "short", day: "numeric" }) }),
        el("span", { text: "today" })),
      readout);
  }

  function heatmap(heat, days) {
    const max = Math.max(1, ...heat.flat());
    const grid = el("div", { class: "heat", role: "img", "aria-label": "Charger use by weekday and hour" }, el("span"));
    for (const h of HOURS) grid.append(el("span", { class: "h", text: h % 3 === 0 ? String(h) : "" }));
    let busiest = null;
    for (const [idx, name] of DAYS) {
      grid.append(el("span", { text: name }));
      for (const h of HOURS) {
        const v = heat[idx][h];
        if (!busiest || v > busiest.v) busiest = { v, name, h };
        grid.append(el("span", { class: "c", title: `${name} ${h}:00 · ${v} min`, style: `opacity:${(0.08 + (v / max) * 0.92).toFixed(2)}` }));
      }
    }
    const tip = busiest && busiest.v > 0 ? `Busiest: ${busiest.name} around ${busiest.h}:00.` : "Not enough data yet.";
    return el("section", { class: "card" }, el("h2", { text: "When it's busiest" }), grid,
      el("div", { class: "legend" }, el("span", { text: "less" }), el("i", { style: "opacity:.15" }), el("i", { style: "opacity:.5" }),
        el("i"), el("span", { text: "more" }), el("span", { style: "margin-left:auto", text: `${tip} Last ${days} days.` })));
  }

  async function load() {
    try {
      const s = await CB.getJSON("/api/stats");
      const pct = s.no_show_rate === null ? "–" : Math.round(s.no_show_rate * 100) + "%";
      main.replaceChildren(
        el("div", { class: "kpis" },
          kpi("k1", String(s.sessions), `sessions, last ${s.days} days`),
          kpi("k2", s.avg_wait_min === null ? "–" : s.avg_wait_min + "m", "average wait"),
          kpi("k3", pct, "no-shows"),
          kpi("k4", String(s.hours_charged) + "h", "charged, last " + s.days + " days")),
        el("div", { class: "grid" }, barChart(s.per_day), heatmap(s.heatmap, s.heatmap_days)));
    } catch (e) {
      main.replaceChildren(el("div", { class: "banner err", text: "Couldn't load stats. Refresh to try again." }));
    }
  }
  load();
  setInterval(load, 5 * 60 * 1000);
})();
