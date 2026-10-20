// O2 Weekly digest. One job: hand over the aggregates. A printable A4 sheet in English, Hindi or both, and the
// two CSV downloads (the register extract only for the roles the server allows, with the reason where it is
// missing). Counts and shares only: no phone number exists in anything here. DEMO is stamped by the frame.

import { api } from "/js/core/api.js";
import { bdi, h, uid } from "/js/core/dom.js";
import { AUTONYMS, currentLang, flatten, t } from "/js/core/i18n.js";
import { dataTable, emptyState, icon, toast } from "/js/ui/index.js";
import { boot, claimText, completion, count, countOrNone, dataView, serverNow, shareNotes, shareText, viewOf, windowText } from "./oversight/lib.js";
import { formatTime } from "/js/core/format.js";

const me = await boot(["oversight", "digest"], "digest.title");

const REGISTER_VIEWS = ["reviewer"]; // who the server lets download the register extract (Health staff); oversight gets the weekly figures only
const main = document.getElementById("main");
const heading = h("h1", null, t("digest.title"));
const body = h("div", { class: "stack stack--l" });
main.append(h("div", { class: "page-title dg-screen" }, heading), body);

// ------------------------------------------------------------- the sheet's own words, per language
const bundles = new Map();
async function translator(lang) {
  if (!bundles.has(lang)) {
    const parts = await Promise.all(
      ["common", "oversight", "digest"].map(async (name) => {
        const response = await fetch(`/i18n/${name}.${lang}.json`, { credentials: "same-origin" });
        if (!response.ok) throw new Error(`digest: cannot load ${name}.${lang}.json (${response.status})`);
        return flatten(await response.json());
      }),
    );
    bundles.set(lang, Object.assign({}, ...parts));
  }
  const table = bundles.get(lang);
  return (key, vars) => {
    const text = table[key];
    if (text === undefined) {
      console.error(`digest: missing key "${key}" for language "${lang}"`);
      return `[missing: ${key}]`;
    }
    return vars ? text.replace(/\{(\w+)\}/g, (whole, name) => (name in vars ? String(vars[name]) : `[?${name}]`)) : text;
  };
}

// ------------------------------------------------------------------------------------ one sheet
function sheet(lang, tr, s) {
  const c = completion(tr, lang, s);
  const cv = s.coverage;
  const titleId = uid("sheet");
  const notes = shareNotes(tr, lang, cv.city);
  const fig = (label, value, sub) =>
    h("div", { class: "sheet__fig" }, h("dt", null, label), h("dd", null, h("span", { class: "sheet__val" }, value), sub ? h("span", { class: "sheet__sub" }, sub) : null));
  const alertsSub = s.alerts_total === 0 ? null : s.alerts_acted === 0 ? tr("oversight.alertsNoneActed") : tr("oversight.alertsActed", { n: count(tr, lang, s.alerts_acted) });
  const table = dataTable({
    caption: tr("digest.sheet.areasCaption"),
    columns: [
      { key: "name", label: tr("digest.sheet.area"), render: (r) => bdi(r.name) },
      { key: "bites", label: tr("digest.sheet.bites"), numeric: true, render: (r) => count(tr, lang, r.bites) },
      { key: "street", label: tr("digest.sheet.street"), numeric: true, render: (r) => count(tr, lang, r.street_dog_bites) },
      { key: "share", label: tr("digest.sheet.share"), render: (r) => shareText(tr, lang, r.share) },
      { key: "unsure", label: tr("digest.sheet.unsure"), numeric: true, render: (r) => (r.share.status === "no_data" ? "" : count(tr, lang, r.share.unsure)) },
    ],
    rows: cv.clusters,
  });
  return h(
    "article",
    { class: "sheet", lang, dir: "ltr", "aria-labelledby": titleId },
    h(
      "header",
      { class: "sheet__head" },
      h("h2", { class: "sheet__title", id: titleId }, tr("digest.sheet.title")),
      h("p", { class: "sheet__window" }, windowText(tr, lang, s.window_start, s.window_end)),
      h("p", { class: "sheet__for" }, tr("digest.sheet.for")),
    ),
    h(
      "section",
      { class: "sheet__sec" },
      h("h3", null, tr("digest.sheet.figures")),
      h(
        "dl",
        { class: "sheet__figs" },
        fig(tr("oversight.completion.title"), c.rate ? `${c.rate}, ${c.interval}` : tr("oversight.noRate"), c.text),
        fig(tr("oversight.enrolled"), countOrNone(tr, lang, s.enrolled, "oversight.noEnrolled")),
        fig(tr("oversight.bites"), countOrNone(tr, lang, s.bites_inside_jmc, "oversight.noBites"), s.bites_with_place === 0 ? null : tr("oversight.bitesOf", { n: count(tr, lang, s.bites_with_place) })),
        fig(tr("oversight.alerts"), countOrNone(tr, lang, s.alerts_total, "oversight.noAlerts"), alertsSub),
      ),
    ),
    h(
      "section",
      { class: "sheet__sec" },
      h("h3", null, tr("share.label")),
      h("p", { class: "sheet__share" }, shareText(tr, lang, cv.city)),
      notes ? h("p", { class: "sheet__sub" }, notes) : null,
      h("p", { class: "sheet__sub" }, claimText(tr, cv)),
      table,
    ),
    h("p", { class: "sheet__foot" }, tr("digest.sheet.foot")),
  );
}

// --------------------------------------------------------------------------------- the controls
let choice = null; // "en" | "hi" | "both"; null follows the page language
const sheetLangs = () => {
  const c = choice ?? currentLang();
  return c === "both" ? ["en", "hi"] : [c];
};

/** A download link. It is a real link; a click fetches the file so a refusal is said here, not in a saved page. */
function download({ href, label, status }) {
  const link = h("a", { class: "btn", href, download: "" }, icon("inbox"), label);
  link.addEventListener("click", async (event) => {
    event.preventDefault();
    if (link.getAttribute("aria-busy") === "true") return;
    link.setAttribute("aria-busy", "true");
    status(null);
    try {
      let response;
      try {
        response = await fetch(href, { credentials: "same-origin" });
      } catch {
        status(t("errors.network"));
        return;
      }
      if (response.status === 401) {
        location.replace(`/signin?next=${encodeURIComponent(location.pathname)}`);
        return;
      }
      if (!response.ok) {
        let message = t("errors.server");
        try {
          message = (await response.json()).message ?? message;
        } catch {
          /* the server's own words are used when it sends them */
        }
        status(message);
        return;
      }
      const name = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") ?? "")?.[1] ?? href.split("/").pop();
      const url = URL.createObjectURL(await response.blob());
      const a = h("a", { href: url, download: name });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      toast(t("digest.saved", { name }));
    } finally {
      link.removeAttribute("aria-busy");
    }
  });
  return link;
}

let controls = null;
let controlsLang = null;
let sheetHost = null;
let repaintSheets = () => {};

function buildControls() {
  const msg = h("p", { class: "dg-msg", role: "status" });
  const status = (text) => {
    msg.replaceChildren(...(text ? [icon("alert"), text] : []));
  };
  const seg = h("div", { class: "seg", role: "group", "aria-label": t("digest.sheetLang") });
  const options = [
    ["en", AUTONYMS.en, "en"],
    ["hi", AUTONYMS.hi, "hi"],
    ["both", t("digest.both"), null],
  ];
  const buttons = options.map(([value, label, code]) =>
    h("button", { type: "button", class: "seg__btn", lang: code, "aria-pressed": "false", dataset: { value }, onClick: () => { choice = value; mark(); repaintSheets(); } }, label),
  );
  const mark = () => {
    const now = choice ?? currentLang();
    for (const b of buttons) b.setAttribute("aria-pressed", String(b.dataset.value === now));
  };
  mark();
  seg.append(...buttons);

  const canRegister = REGISTER_VIEWS.includes(viewOf(me));
  const reasonId = uid("why");
  const register = canRegister
    ? download({ href: "/api/exports/register.csv", label: t("digest.register"), status })
    : h("button", { type: "button", class: "btn", "aria-disabled": "true", "aria-describedby": reasonId }, icon("inbox"), t("digest.register"));
  return h(
    "section",
    { class: "dg-controls dg-screen", "aria-label": t("digest.downloads") },
    h("div", { class: "dg-lang" }, h("span", { class: "dg-lang__label" }, t("digest.sheetLang")), seg),
    h(
      "div",
      { class: "dg-actions" },
      h("button", { type: "button", class: "btn btn--primary", onClick: () => window.print() }, t("digest.print")),
      download({ href: "/api/exports/weekly.csv", label: t("digest.weekly"), status }),
      register,
    ),
    canRegister ? null : h("p", { class: "dg-why muted small", id: reasonId }, icon("info"), t("digest.registerMissing")),
    msg,
  );
}

// ---------------------------------------------------------------------------------------- the page
const wrap = h("div", { class: "stack stack--l" });
sheetHost = h("div", { class: "sheets" });

function render({ summary, tr }) {
  if (controls === null || controlsLang !== currentLang()) {
    controls = buildControls();
    controlsLang = currentLang();
  }
  repaintSheets = () => sheetHost.replaceChildren(...sheetLangs().map((lang) => sheet(lang, tr[lang], summary)));
  repaintSheets();
  if (wrap.firstChild !== controls) wrap.replaceChildren(controls, sheetHost);
  return wrap;
}

function isEmpty({ summary: s, now }) {
  const nothing = s.enrolled === 0 && s.bites_with_place === 0 && s.alerts_total === 0 && s.coverage.city.status === "no_data";
  if (!nothing) return null;
  return emptyState({ title: t("digest.empty.title"), body: `${t("digest.empty.body")} ${t("states.lastChecked", { when: formatTime(now) })}` });
}

dataView(body, {
  load: async () => {
    const [summary, now, en, hi] = await Promise.all([api.get("/api/oversight/summary"), serverNow(me), translator("en"), translator("hi")]);
    return { summary, now, tr: { en, hi } };
  },
  render,
  isEmpty,
  events: ["report"],
  block: true,
});

document.addEventListener("ct:langchange", () => {
  heading.textContent = t("digest.title");
});
