// Component gallery: every component in every state, for the UI check to screenshot.
//   /dev/gallery.html?theme=dark&lang=hi            staff frame, all components
//   /dev/gallery.html?frame=patient&lang=ur         patient frame and the surfaces patient pages use (nobody signed in)
//   /dev/gallery.html?frame=public                  the public frame: staff language toggle, DEMO strip, nobody signed in
//   &pop=1 opens the (i) popover, &dialog=1 opens the dialog, &menu=1 opens a menu
// The page talks to a fake /api (below), so the real api.js, frame.js and sse.js code runs.

const params = new URLSearchParams(location.search);
const theme = params.get("theme");
if (theme === "dark" || theme === "light") document.documentElement.setAttribute("data-theme", theme);

// ------------------------------------------------------------ fake backend
const NOW = "2026-10-19T04:40:00Z"; // Mon 19 Oct 2026, 10:10 IST
const realFetch = globalThis.fetch.bind(globalThis);
const ME = {
  role: "visitor",
  acting_as: "abc",
  display_name: "visitor:1",
  clinic_id: null,
  csrf: "gallery-csrf",
  mode: "demo",
  can_write: false,
};
const DEMO = {
  now: NOW,
  local_now: "2026-10-19T10:10:00+05:30",
  offset_seconds: 0,
  next_call_at: "2026-10-19T04:42:00Z",
  next_call_kind: "first",
  jump_targets: ["next_call", "evening_before_next_dose", "morning_after"],
};
globalThis.fetch = async (url, init = {}) => {
  const path = String(url);
  if (!path.startsWith("/api/")) return realFetch(url, init);
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const anonymous = ["patient", "public"].includes(params.get("frame"));
  if (path === "/api/mode") return json({ mode: "demo", basemap: "none", public_url: null, signed_in: !anonymous, view: anonymous ? null : "abc" });
  if (path === "/api/me") return anonymous ? json({ error: "signed_out", message: "Sign in to continue" }, 401) : json(ME);
  if (path === "/api/demo/state") return anonymous ? json({ error: "signed_out", message: "Sign in to continue" }, 401) : json(DEMO);
  if (path === "/api/demo/jump") return json({ error: "read_only", message: "Your role can't do this" }, 403);
  return json({ error: "not_found", message: "Not part of the gallery" }, 404);
};
globalThis.EventSource = class {
  constructor() {
    setTimeout(() => this.onopen?.(), 0);
  }
  addEventListener() {}
  close() {}
};

const { initI18n, t, PATIENT_LANGS } = await import("/js/core/i18n.js");
const frameMode = params.get("frame") ?? "staff";
const patientMode = frameMode === "patient";
const lang = params.get("lang") ?? (patientMode ? "hi" : "en");
const isPatientLang = PATIENT_LANGS.includes(lang) && !["en", "hi"].includes(lang);
await initI18n({ bundles: ["common"], audience: patientMode || isPatientLang ? "patient" : "staff", lang });

const { h, $ } = await import("/js/core/dom.js");
const ui = await import("/js/ui/index.js");
const { mountFrame } = await import("/js/core/frame.js");
const { formatShare, formatDateTime, sinceText, agoText, formatCount } = await import("/js/core/format.js");
const { mapView } = await import("/js/ui/mapView.js");
const { qrCode } = await import("/js/core/qr.js");
const { STATUS_DOMAINS } = await import("/js/ui/pill.js");
const { loadWards } = await import("/js/core/map.js");

await mountFrame({ mode: frameMode, pathname: "/team" });

const main = $("#main");
const section = (title, note, ...nodes) =>
  h("section", { class: "stack" }, h("h2", null, title), note ? h("p", { class: "gallery-note" }, note) : null, ...nodes);
const demo = (label, node) => h("div", { class: "stack stack--s" }, h("p", { class: "small faint" }, label), node);

// ----------------------------------------------------------- 1. type
function typeSection() {
  const sample = (code, text, name) =>
    h("div", { class: "card stack stack--s", lang: code }, h("p", { class: "small faint", lang: "en" }, name), h("h3", null, text.head), h("p", null, text.body), h("p", { class: "small" }, text.small));
  return section(
    "Type and scripts",
    "Atkinson Hyperlegible Next for Latin and digits, Noto Sans Devanagari for Hindi and Dogri, Noto Nastaliq Urdu for Urdu. Floor 14 px.",
    h(
      "div",
      { class: "grid" },
      sample("en", { head: "Next dose on Thu 22 Oct", body: "Come to the clinic. Call the clinic if you cannot come.", small: "Smallest text is 14 px: 0123456789" }, "English"),
      sample("hi", { head: "अगली खुराक गुरुवार 22 अक्टूबर को", body: "क्लिनिक आइए। न आ सकें तो क्लिनिक को फ़ोन करें।", small: "सबसे छोटा अक्षर 14 px का है: 0123456789" }, "Hindi"),
      sample("doi", { head: "अगली खुराक बीरवार 22 अक्तूबर गी", body: "क्लिनिक च आओ। नेईं आई सको तां क्लिनिक गी फोन करो।", small: "सब्भें थमां छोटा अक्खर 14 px दा ऐ: 0123456789" }, "Dogri (machine-written, needs a native check)"),
      sample("ur", { head: "اگلی خوراک جمعرات ۲۲ اکتوبر کو", body: "کلینک آئیں۔ نہ آ سکیں تو کلینک کو فون کریں۔", small: "سب سے چھوٹا حرف ۱۴ پکسل کا ہے: 0123456789" }, "Urdu (machine-written, needs a native check)"),
    ),
  );
}

// -------------------------------------------------------- 2. colour
function colourSection() {
  const sw = (name) => h("div", { class: "stack stack--s" }, h("span", { class: "swatch", style: { "--sw": `var(${name})` } }), h("span", { class: "small" }, name));
  const names = [
    "--paper", "--surface", "--surface-2", "--line", "--line-strong", "--ink", "--ink-2", "--ink-3",
    "--primary", "--primary-tint", "--attention", "--attention-tint", "--attention-line", "--alarm", "--alarm-tint",
    "--estimate-line", "--estimate-tint", "--demo-bg", "--map-bg", "--map-nodata", "--ramp-1", "--ramp-2", "--ramp-3", "--ramp-4", "--ramp-5",
  ];
  return section("Colour tokens", "Primary is the action. Amber is attention. Red is a system fault only. Dashed is an estimate or simulated.", h("div", { class: "grid" }, names.map(sw)));
}

// ------------------------------------------------------- 3. buttons
function buttonSection() {
  const row = (...n) => h("div", { class: "row" }, ...n);
  return section(
    "Buttons, chips, toggles",
    "One filled button per screen. Targets are 44 px or taller.",
    row(
      h("button", { type: "button", class: "btn btn--primary" }, "Mark dose given"),
      h("button", { type: "button", class: "btn" }, "Stop reminders"),
      h("button", { type: "button", class: "btn btn--quiet" }, "Cancel"),
      h("button", { type: "button", class: "btn btn--primary", "aria-busy": "true" }, "Saving"),
      h("button", { type: "button", class: "btn", disabled: true }, "Disabled"),
      h("button", { type: "button", class: "btn btn--primary", disabled: true }, "Disabled filled"),
    ),
    row(h("button", { type: "button", class: "btn btn--primary btn--lg" }, "Large"), h("button", { type: "button", class: "btn btn--icon", "aria-label": t("buttons.close") }, ui.icon("x")), h("button", { type: "button", class: "btn" }, ui.icon("refresh"), t("buttons.retry"))),
    row(
      h("button", { type: "button", class: "chip", "aria-pressed": "true" }, ui.icon("check"), "Due today"),
      h("button", { type: "button", class: "chip", "aria-pressed": "false" }, "Missed"),
      h("div", { class: "seg", role: "group", "aria-label": "Segmented" }, h("button", { type: "button", class: "seg__btn", "aria-pressed": "true" }, "12 weeks"), h("button", { type: "button", class: "seg__btn", "aria-pressed": "false" }, "4 weeks")),
    ),
  );
}

// --------------------------------------------------------- 4. pills
function pillSection() {
  const groups = Object.entries(STATUS_DOMAINS).map(([domain, values]) =>
    demo(
      `statusPill("${domain}", ...)`,
      h("div", { class: "row" }, Object.keys(values).map((v) => ui.statusPill(domain, v, domain === "clinicRow" && v === "tried" ? { count: 2 } : undefined))),
    ),
  );
  return section("Status pills", "Icon, word and tone. Late and no-answer are amber. Red is only for a system fault. Dashed is estimate or simulated.", ...groups);
}

// ------------------------------------------------- 5. (i) and menus
function infoSection() {
  const info = ui.infoButton({ topic: "the notched share", title: "How to read this", content: h("p", null, t("share.explain")) });
  if (params.get("pop") === "1") requestAnimationFrame(() => info.controller.open());
  const menu = ui.menuButton({ label: "Menu", items: [{ id: "a", label: "Next call" }, { id: "b", label: "Evening before", current: true }, { id: "c", label: "Disabled", disabled: true }], onSelect: () => {} });
  if (params.get("menu") === "1") requestAnimationFrame(() => menu.controller.open());
  return section(
    "(i) button and menu",
    "The (i) is a real button named for its topic. Esc closes it.",
    h("div", { class: "row" }, h("span", null, "The notched share"), info, menu),
  );
}

// ---------------------------------------------- 6. cards, lists, tables
function listSection() {
  const rows = [
    { reg: "A-2041", dose: 3, last4: "4821", state: "due_today" },
    { reg: "A-2038", dose: 2, last4: "0457", state: "tried", count: 2 },
    { reg: "A-2030", dose: 4, last4: "9916", state: "patient_says_taken" },
    { reg: "A-2012", dose: 2, last4: null, state: "late_minor" },
  ];
  const list = h(
    "div",
    { class: "list" },
    rows.map((r) =>
      h(
        "a",
        { class: "list__row list__row--link", href: "#" },
        h("div", { class: "list__main" }, h("span", { class: "list__title num" }, `${r.reg} · ${t("format.dose", { n: r.dose })}`), h("span", { class: "list__meta" }, r.last4 ? t("format.phoneMasked", { last4: r.last4 }) : t("format.phonePurged"))),
        h("div", { class: "list__side" }, ui.statusPill("clinicRow", r.state, r.count ? { count: r.count } : undefined)),
      ),
    ),
  );
  const table = ui.dataTable({
    caption: "Bites by place type, last 4 weeks",
    columns: [
      { key: "ward", label: "Ward" },
      { key: "school", label: "School", numeric: true },
      { key: "hospital", label: "Hospital", numeric: true },
      { key: "bus", label: "Bus stand or market", numeric: true },
      { key: "state", label: "Status", render: (r) => r.state },
    ],
    rows: [
      { ward: "Ward 12, Gandhi Nagar", school: 7, hospital: "<5", bus: 9, state: ui.statusPill("location", "matched") },
      { ward: "Ward 31, Bakshi Nagar", school: "<5", hospital: "<5", bus: 14, state: ui.statusPill("location", "needs_review") },
    ],
  });
  return section(
    "Cards, stats, lists, tables",
    "Cards only where a thing needs a boundary. Counts below 5 show as <5. The table stacks on a phone.",
    h("div", { class: "stats" }, ["Due today|8", "Missed|3", "Waiting for first call|2"].map((s) => h("div", { class: "stat" }, h("span", { class: "stat__n" }, s.split("|")[1]), h("span", { class: "stat__label" }, s.split("|")[0]))), h("div", { class: "stat stat--attention" }, h("span", { class: "stat__n" }, "2"), h("span", { class: "stat__label" }, "Late, to review"))),
    h("div", { class: "list-head" }, h("h3", null, "Today")),
    list,
    table,
    h("div", { class: "timeline" }, [1, 2, 3, 4].map((n) => h("div", { class: "timeline__step" }, h("span", { class: "timeline__no" }, t("format.dose", { n })), h("span", { class: "small" }, formatDateTime(`2026-10-${19 + n * 2}T04:30:00Z`).split(",")[0]), ui.statusPill("dose", ["given", "given", "due", "due"][n - 1])))),
  );
}

// ------------------------------------------------------ 7. states
function stateSection() {
  return section(
    "Empty, error, offline, forbidden, loading",
    "Empty says what is missing. Errors are amber, in plain words, with one Retry. The skeleton appears after 300 ms.",
    h(
      "div",
      { class: "grid" },
      demo("Empty", ui.emptyState({ title: "No one is due today", body: "Next due Thu 22 Oct.", iconName: "inbox" })),
      demo("Empty, no calls", ui.emptyState({ title: t("states.noCalls"), body: t("states.lastChecked", { when: "10:10" }), iconName: "phone" })),
      demo("Error with Retry", ui.errorState({ message: "Could not load today's list", onRetry: () => {}, why: "#" })),
      demo("Offline", ui.errorState({ message: t("states.offline"), onRetry: () => {} })),
      demo("Forbidden", ui.emptyState({ title: t("states.forbidden"), iconName: "info" })),
      demo("System fault (red)", ui.errorState({ message: "Alert not delivered to vet on duty", tone: "alarm", onRetry: () => {} })),
      demo("Skeleton", ui.skeleton({ lines: 4 })),
    ),
    demo("Banners", h("div", { class: "stack stack--s" }, ui.banner({ text: t("states.offline") }), ui.banner({ text: "Dongle offline: calls are not going out", tone: "alarm" }), ui.banner({ text: "Calls are not going out: the phone line is down." }), ui.banner({ text: "Read-only view", tone: "neutral" }))),
  );
}

// ------------------------------------------------- 8. overlays, tabs
function overlaySection() {
  const open = h("button", { type: "button", class: "btn", onClick: openConfirm }, "Open dialog");
  async function openConfirm() {
    const yes = await ui.confirmDialog({ title: "Stop reminders for A-2041?", body: "No more calls will be made to this patient.", confirmLabel: "Stop reminders" });
    ui.toast(yes ? "Reminders stopped" : "Nothing changed");
  }
  if (params.get("dialog") === "1") requestAnimationFrame(openConfirm);
  const tabs = ui.tabs({
    label: "Admin sections",
    items: [
      { id: "clinics", label: "Clinics", render: () => h("p", null, "Clinics and regimens.") },
      { id: "people", label: "People", render: () => h("p", null, "People and roles.") },
      { id: "prompts", label: "Prompts", render: () => h("p", null, "Prompt audio by language.") },
      { id: "retention", label: "Retention", render: () => h("p", null, "Deletion log.") },
      { id: "system", label: "System", render: () => h("p", null, "Checks.") },
    ],
    selected: "people",
  });
  return section(
    "Dialog, toast, tabs",
    null,
    h("div", { class: "row" }, open, h("button", { type: "button", class: "btn", onClick: () => ui.toast("Saved") }, "Show toast"), h("button", { type: "button", class: "btn", onClick: () => ui.toast("Your role can't do this", { tone: "attention" }) }, "Show amber toast")),
    tabs.el,
  );
}

// ------------------------------------------- 9. stepper, keypad
function inputSection() {
  const hints = patientMode ? {} : { 0: "hear again", 9: "withdraw" };
  return section(
    "Stepper and keypad",
    "Big - and + for the sight count. The keypad never mirrors and its digits are the same in every script.",
    h(
      "div",
      { class: "row row--top" },
      ui.stepper({ label: "Notched", value: 3, min: 0, max: 99, onChange: () => {} }).el,
      ui.stepper({ label: "Not notched", value: 0, min: 0, max: 99 }).el,
      ui.stepper({ label: "Could not see", value: 99, min: 0, max: 99 }).el,
    ),
    keypadRow(hints),
  );
}
function keypadRow(hints) {
  return h("div", { class: "grid" }, demo("Keypad", ui.keypad({ onKey: () => {}, hints, label: "Keypad" }).el), demo("Keypad, disabled", ui.keypad({ onKey: () => {}, hints, disabled: true, label: "Keypad, disabled" }).el));
}

// --------------------------------------------------- 10. share display
const ok = { n: 112, notched: 70, unnotched: 42, unsure: 14, status: "ok", share: 0.625, ci_low: 0.54, ci_high: 0.7, bound_low: 0.41, bound_high: 0.78, needed: null };
const claim = { band: [0.78, 0.8], labelKey: "share.claim" };
function shareSection() {
  const cases = [
    ["ok, with reference band", ui.shareDisplay(ok, { claim })],
    ["bracketed (25 to 49 answers)", ui.shareDisplay({ ...ok, n: 38, notched: 24, unnotched: 14, unsure: 9, status: "bracketed", share: 0.63, ci_low: 0.47, ci_high: 0.77 }, { compact: true })],
    ["too few answers", ui.shareDisplay({ n: 11, notched: 6, unnotched: "<5", unsure: 7, status: "too_few", share: null, ci_low: null, ci_high: null, bound_low: null, bound_high: null, needed: 14 }, { compact: true })],
    ["no data", ui.shareDisplay({ n: 0, notched: 0, unnotched: 0, unsure: 0, status: "no_data", share: null, ci_low: null, ci_high: null, needed: 25 }, { lastChecked: t("states.lastChecked", { when: "10:10" }), compact: true })],
    ["simulated", ui.shareDisplay({ ...ok, n: 30, notched: 18, unnotched: 12, unsure: 3, share: 0.6, ci_low: 0.42, ci_high: 0.75, status: "bracketed" }, { compact: true, simulated: true })],
  ];
  return section(
    "Share display",
    "A percentage never appears without its interval, n and the could-not-see count. Below the minimum there is no percentage at all.",
    h("div", { class: "grid" }, cases.map(([label, node]) => h("div", { class: "card stack stack--s" }, h("p", { class: "small faint" }, label), node))),
    h("p", { class: "gallery-note" }, `Text forms: ${formatShare(ok, { withN: true })} · ${formatShare({ n: 11, status: "too_few", needed: 14 })}`),
  );
}

// ------------------------------------------------------ 11. alerts
function alertSection() {
  const base = { id: "alr_1", ward_no: 12, ward_name: "Gandhi Nagar", cluster_id: "C3", place_type: "bus_market", local_day: "2026-10-19", status: "new", claimed_by: null, claimed_at: null, outcome_note: null, created_at: "2026-10-19", made_at: null, callback_status: null, simulated: false, claimed_by_me: false, location_status: "matched" };
  const act = () => {};
  return section(
    "Alert cards",
    "Amber, titled by where it happened. A machine-matched ward carries the dashed \"estimate\" pill; only a reviewer-decided ward (the acted card) is bare. No phone number. One filled button. Who has it reads \"You are on it\" or \"Taken by <name>\".",
    h(
      "div",
      { class: "grid" },
      ui.alertCard(base, { onClaim: act, onCallback: act }),
      ui.alertCard({ ...base, id: "alr_2", status: "claimed", claimed_by: "vet", claimed_by_me: true, ward_no: 31, ward_name: "Bakshi Nagar", place_type: "school" }, { onActed: act, onNotActed: act }),
      ui.alertCard({ ...base, id: "alr_5", status: "claimed", claimed_by: "Dr Rao", ward_no: 7, ward_name: "Rani Bagh", place_type: "home_street" }),
      ui.alertCard({ ...base, id: "alr_3", status: "acted", claimed_by: "Dr Rao", outcome_note: "Dog caught and handed over", local_day: "2026-10-18", callback_status: "done", location_status: "decided" }),
      ui.alertCard({ ...base, id: "alr_4", simulated: true }, { onClaim: act }),
      ui.alertFaultCard({ message: "Alert not delivered to vet on duty", onRetry: act }),
    ),
  );
}

// --------------------------------------------------------- 12. forms
function formSection() {
  const name = ui.field({ label: "Register number", hint: "As written in the clinic register", control: ui.textInput({ name: "reg", value: "A-2041" }) });
  const phone = ui.field({ label: "Phone number", control: ui.textInput({ name: "phone", type: "tel", inputmode: "numeric" }), error: "Enter a 10-digit number" });
  const lang = ui.field({ label: "Language", control: ui.selectInput({ name: "lang", value: "hi", options: [{ value: "hi", label: "Hindi" }, { value: "doi", label: "Dogri" }, { value: "ur", label: "Urdu" }] }) });
  const summary = ui.errorSummary([{ id: phone.control.id, message: "Phone number: enter a 10-digit number" }]);
  return section(
    "Form fields",
    "Errors are amber with an icon and words. Typed values stay when a send fails.",
    h("div", { class: "card stack" }, summary, name.el, phone.el, lang.el, ui.checkbox({ name: "guardian", label: "Patient is under 18: guardian present and consented" }), ui.unsentNote(), h("div", { class: "row" }, h("button", { type: "button", class: "btn btn--primary" }, t("buttons.confirm")))),
  );
}

// ------------------------------------------------- 13. map and QR
async function mapSection() {
  const g = await loadWards();
  const centre = (f) => {
    const ring = f.geometry.type === "Polygon" ? f.geometry.coordinates[0] : f.geometry.coordinates[0][0];
    return [ring.reduce((a, p) => a + p[0], 0) / ring.length, ring.reduce((a, p) => a + p[1], 0) / ring.length];
  };
  const sorted = g.features.map((f) => ({ no: f.properties.ward_no, c: centre(f) })).sort((a, b) => b.c[1] - a.c[1] || a.c[0] - b.c[0]);
  const per = Math.ceil(sorted.length / 8);
  const shares = [
    { status: "ok", share: 0.82, look: false },
    { status: "ok", share: 0.74, look: false, sim: 2 },
    { status: "ok", share: 0.66, look: false },
    { status: "ok", share: 0.58, look: false },
    { status: "bracketed", share: 0.36, look: true },
    { status: "ok", share: 0.47, look: true },
    { status: "too_few" },
    { status: "no_data" },
  ];
  const clusters = shares.map((s, i) => ({
    id: `C${i + 1}`,
    name: `Gallery area ${i + 1}`,
    ward_nos: sorted.slice(i * per, (i + 1) * per).map((w) => w.no),
    share: { status: s.status, share: s.share ?? null },
    look_here: Boolean(s.look),
    simulated_bites: s.sim ?? 0,
  }));
  const view = mapView({ onSelect: () => {} });
  view.setData({ clusters });
  const qr = qrCode(`${location.origin}/phone?s=gallery`);
  return section(
    "Ward map and QR",
    "Gallery clusters are arbitrary (sorted by latitude), only to show every fill. Ward polygons are real. Dashed outline = includes simulated. Amber outline = look here.",
    view.el,
    demo("QR (the server route is not part of the gallery, so the plain link shows)", qr),
  );
}

// -------------------------------------------------- 14. formats + RTL
function formatSection() {
  const now = NOW;
  return section(
    "Formats (IST, Western digits)",
    null,
    h("div", { class: "kv" }, ...[
      ["Time", formatDateTime("2026-10-19T05:10:00Z")],
      ["Since", sinceText("2026-10-19T05:10:00Z", now)],
      ["Ago", agoText("2026-10-19T04:20:00Z", now)],
      ["Count", `${formatCount(123456)} · ${formatCount("<5")}`],
    ].flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)])),
    h(
      "div",
      { class: "card", dir: "rtl", lang: "ur" },
      h("div", { class: "list__row" }, h("div", { class: "list__main" }, h("span", { class: "list__title" }, "اگلی خوراک"), h("span", { class: "list__meta" }, "جمعرات ۲۲ اکتوبر")), h("span", { class: "pill pill--primary" }, ui.icon("check"), "درج")),
      h("button", { type: "button", class: "btn btn--primary" }, "ٹھیک ہے"),
    ),
  );
}

// ---------------------------------------------------------- assemble
function patientSurfaces() {
  return [
    typeSection(),
    section("Patient surfaces", "What the phone and privacy pages use.", h("div", { class: "grid" }, demo("Loading", ui.skeleton({ lines: 3 })), demo("Error", ui.errorState({ message: t("errors.network"), onRetry: () => {} }))), keypadRow({}), h("div", {}, qrCode(`${location.origin}/phone?s=gallery`))),
  ];
}

main.append(
  h("div", { class: "page-title" }, h("h1", null, "Component gallery"), h("span", { class: "pill pill--estimate" }, "dev page, not shipped to users")),
  h(
    "div",
    { class: "row" },
    h("div", { class: "seg", role: "group", "aria-label": "Theme" }, ["auto", "light", "dark"].map((m) =>
        h(
          "button",
          { type: "button", class: "seg__btn", "aria-pressed": String((theme ?? "auto") === m), onClick: () => (location.search = new URLSearchParams({ ...Object.fromEntries(params), theme: m }).toString()) },
          m,
        ),
      )),
  ),
);
if (patientMode || isPatientLang) {
  main.append(...patientSurfaces());
} else {
  main.append(typeSection(), colourSection(), buttonSection(), pillSection(), infoSection(), listSection(), stateSection(), overlaySection(), inputSection(), shareSection(), alertSection(), formSection(), formatSection());
  main.append(await mapSection());
}
document.documentElement.dataset.galleryReady = "1";
