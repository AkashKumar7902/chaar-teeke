// O1 Summary. One job: show whether the pilot works. Course completion with its honest basis, who enrolled,
// bites inside JMC, alerts sent and acted on, and the coverage component (the notched share with its interval,
// the claimed band, and the areas). Live through the shared event stream; nothing here is patient-level.

import { api } from "/js/core/api.js";
import { bdi, h } from "/js/core/dom.js";
import { currentLang, t } from "/js/core/i18n.js";
import { emptyState, icon, infoButton, shareDisplay } from "/js/ui/index.js";
import { boot, claimText, completion, count, countOrNone, dataView, methodInfo, methodLink, serverNow, shareNotes, shareText, windowText } from "./oversight/lib.js";
import { formatTime } from "/js/core/format.js";

const me = await boot(["oversight"], "oversight.title");

const main = document.getElementById("main");
const windowLine = h("p", { class: "muted small ov-window" });
const heading = h("h1", null, t("oversight.title"));
const body = h("div", { class: "stack stack--l" });
main.append(h("div", { class: "page-title" }, heading, windowLine), body);

const tr = (key, vars) => t(key, vars);
const lang = () => currentLang();

// ---------------------------------------------------------------- the headline: course completion
function headline(s) {
  const c = completion(tr, lang(), s);
  return h(
    "section",
    { class: "card ov-headline", "aria-labelledby": "ov-completion" },
    h(
      "div",
      { class: "list-head" },
      h("h2", { class: "section-title", id: "ov-completion" }, t("oversight.completion.title")),
      methodInfo({ topic: t("oversight.completion.topic"), text: t("oversight.completion.about"), anchor: "measured" }),
    ),
    c.rate
      ? h("p", { class: "ov-rate" }, h("span", { class: "ov-rate__n" }, c.rate), h("span", { class: "ov-rate__ci" }, c.interval))
      : h("p", { class: "ov-rate ov-rate--none" }, icon("info"), t("oversight.noRate")),
    h("p", { class: "ov-basis" }, c.text),
  );
}

// ------------------------------------------------------------------------------------ the counts
function tile(label, value, sub) {
  return h("div", { class: "stat" }, h("span", { class: "stat__n" }, value), h("span", { class: "stat__label" }, label), sub ? h("span", { class: "small muted" }, sub) : null);
}

function tiles(s) {
  const l = lang();
  const alertsSub =
    s.alerts_total === 0 ? null : s.alerts_acted === 0 ? t("oversight.alertsNoneActed") : t("oversight.alertsActed", { n: count(tr, l, s.alerts_acted) });
  return h(
    "div",
    { class: "stats ov-tiles", role: "group", "aria-label": t("oversight.tiles") },
    tile(t("oversight.enrolled"), countOrNone(tr, l, s.enrolled, "oversight.noEnrolled"), null),
    tile(t("oversight.bites"), countOrNone(tr, l, s.bites_inside_jmc, "oversight.noBites"), s.bites_with_place === 0 ? null : t("oversight.bitesOf", { n: count(tr, l, s.bites_with_place) })),
    tile(t("oversight.alerts"), countOrNone(tr, l, s.alerts_total, "oversight.noAlerts"), alertsSub),
  );
}

// -------------------------------------------------------------------------------------- coverage
function areaRow(c) {
  const notes = shareNotes(tr, lang(), c.share);
  return h(
    "li",
    { class: "list__row area-row" },
    h(
      "div",
      { class: "list__main" },
      h("span", { class: "list__title" }, bdi(c.name)),
      h("span", { class: "list__meta" }, shareText(tr, lang(), c.share)),
      notes ? h("span", { class: "list__meta" }, notes) : null,
    ),
  );
}

/** The share component, with its (i) widened to say what the notch stands in for and to point at the method page. */
function share(cv) {
  const el = shareDisplay(cv.city, { claim: { band: cv.claim_band, labelKey: cv.claim_label_key } });
  const own = el.querySelector(".share__head .pop");
  const c = cv.city;
  const text = [t("oversight.coverage.about")];
  if (typeof c.bound_low === "number" && typeof c.bound_high === "number") text.push(t("oversight.bounds", { lo: Math.round(c.bound_low * 100), hi: Math.round(c.bound_high * 100) }));
  const about = methodInfo({ topic: t("oversight.coverage.topic"), text, anchor: "proxy" });
  if (own) own.replaceWith(about);
  return el;
}

function coverage(cv) {
  return h(
    "div",
    { class: "ov-split" },
    h(
      "section",
      { class: "stack stack--s ov-cov", "aria-labelledby": "ov-cov-h" },
      h("div", { class: "list-head" }, h("h2", { class: "section-title", id: "ov-cov-h" }, t("oversight.coverage.title"))),
      share(cv),
    ),
    h(
      "section",
      { class: "stack stack--s ov-areas", "aria-labelledby": "ov-areas-h" },
      h(
        "div",
        { class: "list-head" },
        h("h2", { class: "section-title", id: "ov-areas-h" }, t("oversight.areas.title")),
        infoButton({ topic: t("oversight.areas.topic"), title: t("buttons.about", { topic: t("oversight.areas.topic") }), content: h("p", null, t("oversight.areas.about")) }),
      ),
      h("ul", { class: "list" }, cv.clusters.map(areaRow)),
    ),
  );
}

function render({ summary: s }) {
  windowLine.textContent = windowText(tr, lang(), s.window_start, s.window_end);
  return h(
    "div",
    { class: "stack stack--l" },
    h("div", { class: "ov-top" }, headline(s), tiles(s)),
    coverage(s.coverage),
    h("p", { class: "ov-foot" }, methodLink("measured")),
  );
}

function isEmpty({ summary: s, now }) {
  windowLine.textContent = windowText(tr, lang(), s.window_start, s.window_end);
  const nothing = s.enrolled === 0 && s.bites_with_place === 0 && s.alerts_total === 0 && s.coverage.city.status === "no_data";
  if (!nothing) return null;
  return emptyState({
    title: t("oversight.empty.title"),
    body: [t("oversight.empty.body"), " ", t("states.lastChecked", { when: formatTime(now) })].join(""),
  });
}

dataView(body, {
  load: async () => {
    const [summary, now] = await Promise.all([api.get("/api/oversight/summary"), serverNow(me)]);
    return { summary, now };
  },
  render,
  isEmpty,
  events: ["report"],
  block: true,
});

document.addEventListener("ct:langchange", () => {
  heading.textContent = t("oversight.title");
});
