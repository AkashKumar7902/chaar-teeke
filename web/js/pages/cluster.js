// A2 Area detail. One area: its share (with interval, n, could-not-see), bites and place types, the wards in
// it with counts only (never shares), worked wards with an early signal, and the team's own sight counts
// beside the bite-based share. Counts under 5 arrive from the server as "<5".

import { ApiError, api } from "/js/core/api.js";
import { bdi, h } from "/js/core/dom.js";
import { formatCount, formatDate } from "/js/core/format.js";
import { t } from "/js/core/i18n.js";
import { dataTable, emptyState, infoButton, pill, shareDisplay } from "/js/ui/index.js";
import { boot, dataView, factLine, refreshTitle, viewOf } from "./team/lib.js";

let areaName = null;
const me = await boot(["team", "cluster"], () => areaName ?? t("team.title"));
const RECORD_VIEWS = new Set(["abc", "vet", "admin"]);

const id = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() ?? "");
const main = document.getElementById("main");
const body = h("div", { class: "stack stack--l" });
main.append(body);

async function loadAll() {
  let detail;
  try {
    detail = await api.get(`/api/clusters/${encodeURIComponent(id)}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return { missing: true };
    throw err;
  }
  // The team's own rounds are readable by the ABC team, vets and admin; other roles have no rounds section.
  const rounds = RECORD_VIEWS.has(viewOf(me)) ? await api.get("/api/sight-counts") : null;
  return { detail, rounds };
}

const back = () => h("a", { class: "cluster-back", href: "/team" }, t("cluster.back"));

function sectionHead(title, topic, about, extra = null) {
  return h("div", { class: "list-head" }, h("h2", { class: "section-title" }, title), extra, about ? infoButton({ topic, title: topic, content: h("p", null, about) }) : null);
}

/** The table's caption names it for screen readers; the section heading already names it on screen. */
function quietCaption(node) {
  node.querySelector?.("caption")?.classList.add("visually-hidden");
  return node;
}

function stat(label, value) {
  return h("div", { class: "stat" }, h("span", { class: "stat__n" }, value), h("span", { class: "stat__label" }, label));
}

function roundsSection(d, rounds) {
  const mine = new Set(d.ward_nos);
  const rows = rounds.filter((r) => mine.has(r.ward_no));
  const head = sectionHead(t("cluster.rounds.title"), t("cluster.rounds.topic"), t("cluster.rounds.about"));
  if (rows.length === 0) return h("section", { class: "stack stack--s" }, head, h("p", { class: "muted" }, t("cluster.rounds.none")));
  const sum = (k) => rows.reduce((a, r) => a + r[k], 0);
  return h(
    "section",
    { class: "stack stack--s" },
    head,
    h(
      "dl",
      { class: "kv" },
      h("dt", null, t("cluster.rounds.notched")),
      h("dd", null, formatCount(sum("notched"))),
      h("dt", null, t("cluster.rounds.unnotched")),
      h("dd", null, formatCount(sum("unnotched"))),
      h("dt", null, t("cluster.rounds.unsure")),
      h("dd", null, formatCount(sum("unsure"))),
    ),
    h("p", { class: "faint small" }, t("cluster.rounds.wards", { count: rows.length })),
  );
}

/** On a phone each ward is one row with its counts in a line, instead of a table that stacks seven lines per ward. */
function wardsList(d) {
  return h(
    "div",
    { class: "list ward-list" },
    d.wards.map((w) =>
      h(
        "div",
        { class: "list__row ward-row" },
        h(
          "div",
          { class: "list__main" },
          h("span", { class: "list__title" }, `${w.ward_no} `, bdi(w.ward_name ?? "")),
          h("span", { class: "list__meta" }, factLine([
            `${t("cluster.wards.bites")} ${formatCount(w.bites)}`,
            `${t("placeType.school")} ${formatCount(w.school)}`,
            `${t("placeType.hospital")} ${formatCount(w.hospital)}`,
            `${t("placeType.bus_market")} ${formatCount(w.bus_market)}`,
            `${t("cluster.wards.alerts")} ${formatCount(w.alerts)}`,
          ])),
          h("span", { class: "list__meta" }, `${t("cluster.wards.worked")}: ${w.worked_on ? formatDate(w.worked_on) : t("cluster.wards.never")}`),
        ),
      ),
    ),
  );
}

function wardsBlock(d) {
  return h("div", null, h("div", { class: "wards-wide" }, wardsTable(d)), h("div", { class: "wards-narrow" }, wardsList(d)));
}

function wardsTable(d) {
  const num = (k) => (w) => formatCount(w[k]);
  return quietCaption(dataTable({
    caption: t("cluster.wards.caption"),
    columns: [
      { key: "ward", label: t("cluster.wards.ward"), render: (w) => h("span", null, `${w.ward_no} `, bdi(w.ward_name ?? "")) },
      { key: "bites", label: t("cluster.wards.bites"), numeric: true, render: num("bites") },
      { key: "school", label: t("placeType.school"), numeric: true, render: num("school") },
      { key: "hospital", label: t("placeType.hospital"), numeric: true, render: num("hospital") },
      { key: "bus_market", label: t("placeType.bus_market"), numeric: true, render: num("bus_market") },
      { key: "alerts", label: t("cluster.wards.alerts"), numeric: true, render: num("alerts") },
      { key: "worked_on", label: t("cluster.wards.worked"), render: (w) => (w.worked_on ? formatDate(w.worked_on) : t("cluster.wards.never")) },
    ],
    rows: d.wards,
  }));
}

function workedTable(d) {
  if (d.worked_wards.length === 0) return h("p", { class: "muted" }, t("cluster.worked.none"));
  const weeks = (n) => t("cluster.worked.weeks", { count: n });
  return quietCaption(dataTable({
    caption: t("cluster.worked.caption"),
    columns: [
      { key: "ward", label: t("cluster.wards.ward"), render: (w) => h("span", null, `${w.ward_no} `, bdi(w.ward_name ?? "")) },
      { key: "on", label: t("cluster.worked.on"), render: (w) => formatDate(w.worked_on) },
      { key: "before", label: t("cluster.worked.before"), numeric: true, render: (w) => `${formatCount(w.bites_before)} · ${weeks(w.before_weeks)}` },
      {
        key: "after",
        label: t("cluster.worked.after"),
        numeric: true,
        render: (w) => (w.bites_after === null ? t("cluster.worked.tooEarly") : `${formatCount(w.bites_after)} · ${weeks(w.after_weeks)}`),
      },
    ],
    rows: d.worked_wards,
  }));
}

/**
 * Bites each week as one row per week: the date, a bar and the count in words. A count under 5 or one the server
 * holds back ("hidden") is never drawn as a length: it gets a hatched stub and its own word. The newest week
 * ("partial") has a dashed outline and says it is not over.
 */
function weeklyChart(weeks) {
  const numbers = weeks.filter((w) => Number.isInteger(w.bites)).map((w) => w.bites);
  const top = Math.max(1, ...numbers);
  const rows = weeks.map((w) => {
    const counted = Number.isInteger(w.bites);
    const bar = h("span", { class: `weekly__bar${counted ? "" : " weekly__bar--hatched"}${w.partial ? " weekly__bar--partial" : ""}` });
    if (counted) bar.style.setProperty("--len", String(w.bites / top));
    const word = formatCount(w.bites);
    return h(
      "li",
      { class: "weekly__row" },
      h("span", { class: "weekly__date" }, formatDate(w.week_start)),
      h("span", { class: "weekly__track", "aria-hidden": "true" }, bar),
      h("span", { class: "weekly__n" }, w.partial ? t("cluster.weekly.soFar", { n: word }) : word),
    );
  });
  return h(
    "figure",
    { class: "weekly" },
    h("figcaption", { class: "faint small" }, t("cluster.weekly.legend")),
    h("ol", { class: "weekly__rows", "aria-label": t("cluster.weekly.title") }, rows),
  );
}

function render(data) {
  if (data.missing) {
    return h("div", { class: "stack" }, back(), emptyState({ title: t("team.areaGone"), iconName: "map", action: h("a", { class: "btn", href: "/team" }, t("cluster.back")) }));
  }
  const { detail: d, rounds } = data;
  areaName = d.name;
  refreshTitle();
  // Six cells that add up to the bites: the five place types and the bites whose place was not said.
  const places = [
    ...["home_street", "school", "hospital", "bus_market", "other"].map((k) => stat(t(`placeType.${k}`), formatCount(d.place_types[k]))),
    stat(t("cluster.places.unknown"), formatCount(d.place_types.unknown)),
  ];
  const stats = [stat(t("cluster.bites"), formatCount(d.bites))];
  if (d.bites_per_10000 !== null) stats.push(stat(t("cluster.perTenK"), String(d.bites_per_10000)));
  return h(
    "div",
    { class: "stack stack--l" },
    h(
      "div",
      { class: "stack stack--s" },
      back(),
      h("h1", null, bdi(d.name)),
      h("p", { class: "muted" }, t("cluster.window", { from: formatDate(d.window_start), to: formatDate(d.window_end) })),
    ),
    h(
      "div",
      { class: "cluster-top" },
      h("div", { class: "card" }, shareDisplay(d.share)),
      h(
        "div",
        { class: "stack" },
        h("div", { class: "stats" }, stats),
        d.wards_without_population.length ? h("p", { class: "faint small" }, t("cluster.popMissing", { count: d.wards_without_population.length })) : null,
        rounds ? roundsSection(d, rounds) : null,
      ),
    ),
    h("section", { class: "stack stack--s" }, sectionHead(t("cluster.places.title"), t("cluster.places.title"), t("cluster.places.about")), h("div", { class: "stats" }, places)),
    h("section", { class: "stack stack--s" }, sectionHead(t("cluster.weekly.title"), t("cluster.weekly.title"), t("cluster.weekly.about")), weeklyChart(d.weekly_bites)),
    h("section", { class: "stack stack--s" }, sectionHead(t("cluster.wards.title"), null, null, h("span", { class: "muted small" }, t("cluster.wards.countsOnly"))), wardsBlock(d)),
    h("section", { class: "stack stack--s" }, sectionHead(t("cluster.worked.title"), t("cluster.worked.topic"), t("cluster.worked.about"), pill({ tone: "estimate", text: t("cluster.worked.signal") })), workedTable(d)),
  );
}

dataView(body, { load: loadAll, render, events: ["report", "alert"], block: true });
