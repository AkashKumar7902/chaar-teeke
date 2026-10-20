// A4 Worked wards. Record that the team worked wards on a date: pick wards from the 75 (search by name or
// number), pick the day, record. The recent list shows what was recorded. The form is kept apart from the
// lists, so a lost connection never wipes what was chosen: it stays, marked "Not sent".

import { api } from "/js/core/api.js";
import { bdi, h } from "/js/core/dom.js";
import { formatCount, formatDate } from "/js/core/format.js";
import { t } from "/js/core/i18n.js";
import { emptyState, field, icon, textInput, toast, unsentNote } from "/js/ui/index.js";
import { bindAttr, bindText, boot, canRecord, dataView, isOffline, loadWards, lockForm, onLang, todayTracker, wardLabel } from "./team/lib.js";

const me = await boot(["worked"], "worked.title");
const today = todayTracker(me);
await today.ready;
const writable = canRecord(me);

const state = { selected: new Set(), query: "", wards: [] };

// ----------------------------------------------------------------- the form
const dateInput = h("input", { class: "input", type: "date", name: "worked_on", max: today.get(), value: today.get(), required: true });
const dateField = field({ label: t("worked.date"), control: dateInput });
bindText(dateField.el.querySelector(".field__label"), () => t("worked.date"));

const search = textInput({ name: "q", type: "search", autocomplete: "off" });
search.setAttribute("enterkeyhint", "search");
const searchField = field({ label: t("worked.search"), control: search });
bindText(searchField.el.querySelector(".field__label"), () => t("worked.search"));

const pickerHost = h("div", { class: "picker-host" });
const summary = h("p", { class: "picker-summary", role: "status" });
const statusHost = h("div", { class: "stack stack--s", role: "status" });
const effectText = h("span", null);
bindText(effectText, () => t("worked.effect"));
// How many patients would hear it, from the server (an aggregate: a whole number or "<5"). Blank until a ward is chosen.
const effectCount = h("span", { class: "worked-effect__count", role: "status" });
const effect = h("p", { class: "muted worked-effect" }, effectText, effectCount);

const sendLabel = h("span", null);
bindText(sendLabel, () => t("worked.record"));
const sendBtn = h("button", { type: "submit", class: "btn btn--primary btn--lg" }, sendLabel);

const selectShownBtn = h("button", { type: "button", class: "btn", onClick: selectShown }, h("span", null));
bindText(selectShownBtn.firstChild, () => t("worked.selectShown"));
const resetBtn = h("button", { type: "button", class: "btn btn--quiet", onClick: () => { state.selected.clear(); syncChecks(); updateSummary(); } }, h("span", null));
bindText(resetBtn.firstChild, () => t("worked.reset"));

const form = h(
  "form",
  { class: "card stack worked-form", novalidate: true, onSubmit: (e) => { e.preventDefault(); send(); } },
  dateField.el,
  searchField.el,
  h("div", { class: "row row--between" }, summary, h("div", { class: "row" }, selectShownBtn, resetBtn)),
  pickerHost,
  effect,
  statusHost,
  h("div", { class: "row" }, sendBtn),
);

// -------------------------------------------------------------- the picker
let items = []; // {ward, label, el, input}

function matches(w, q) {
  if (!q) return true;
  const hay = `${w.ward_no} ${w.ward_name ?? ""}`.toLowerCase();
  return q
    .toLowerCase()
    .split(/\s+/)
    .every((part) => hay.includes(part));
}

function applyFilter() {
  let any = false;
  for (const it of items) {
    const show = matches(it.ward, state.query);
    it.el.hidden = !show;
    any ||= show;
  }
  noMatch.hidden = any;
}

function syncChecks() {
  for (const it of items) it.input.checked = state.selected.has(it.ward.ward_no);
}

function selectShown() {
  for (const it of items) if (!it.el.hidden) state.selected.add(it.ward.ward_no);
  syncChecks();
  updateSummary();
}

function updateSummary() {
  summary.textContent = state.selected.size ? t("worked.selected", { count: state.selected.size }) : t("worked.noneSelected");
  showUnsent();
  askAffected();
}
onLang(updateSummary);

// ---------------------------------------------- who would hear it (GET /api/worked/affected)
let affectedTicket = 0;
let affectedTimer = null;
function showAffected(n) {
  effectCount.replaceChildren(n === null ? "" : h("span", { class: "nw" }, t("worked.effectCount", { n: formatCount(n) })));
}
function askAffected() {
  clearTimeout(affectedTimer);
  const wards = [...state.selected].sort((a, b) => a - b);
  const mine = (affectedTicket += 1);
  if (wards.length === 0) {
    showAffected(null);
    return;
  }
  // one request for a burst of taps; a late answer for an older choice is dropped
  affectedTimer = setTimeout(async () => {
    try {
      const { courses_with_doses_ahead } = await api.get("/api/worked/affected", { ward_nos: wards.join(",") }, { quiet: true });
      if (mine === affectedTicket) showAffected(courses_with_doses_ahead);
    } catch (err) {
      if (mine === affectedTicket) effectCount.replaceChildren(h("span", { class: "field__error" }, icon("alert"), err.message));
    }
  }, 250);
}

const noMatch = h("p", { class: "muted picker__none", hidden: true });
bindText(noMatch, () => t("worked.noMatch"));

function renderPicker(wards) {
  state.wards = wards;
  items = wards.map((w) => {
    const input = h("input", {
      type: "checkbox",
      checked: state.selected.has(w.ward_no) || null,
      onChange: () => {
        if (input.checked) state.selected.add(w.ward_no);
        else state.selected.delete(w.ward_no);
        updateSummary();
      },
    });
    const meta = w.worked_on ? h("span", { class: "picker__meta" }, t("worked.lastWorked", { date: formatDate(w.worked_on) })) : null;
    const el = h("label", { class: "check picker__item" }, input, h("span", { class: "picker__name" }, h("span", { class: "num" }, `${w.ward_no} `), bdi(w.ward_name ?? ""), meta));
    return { ward: w, el, input };
  });
  const node = h("div", { class: "picker" }, h("div", { class: "picker__list", role: "group", "aria-label": t("worked.wards") }, items.map((i) => i.el)), noMatch);
  if (!writable) lockForm(node);
  queueMicrotask(() => { applyFilter(); updateSummary(); });
  return node;
}

// "Select shown" needs a search: with none it would pick all 75 wards in one tap.
function syncShownBtn() {
  selectShownBtn.disabled = !writable || state.query === "";
}
search.addEventListener("input", () => {
  state.query = search.value.trim();
  applyFilter();
  syncShownBtn();
});
syncShownBtn();

const wardsView = dataView(pickerHost, { load: loadWards, render: renderPicker, lines: 6, keepOnOffline: true });

// ---------------------------------------------------------------- sending
function showUnsent() {
  if (statusHost.dataset.error) return;
  statusHost.replaceChildren(isOffline() && state.selected.size ? unsentNote() : "");
}

async function send() {
  statusHost.dataset.error = "";
  delete statusHost.dataset.error;
  dateField.setError(null);
  const day = dateInput.value;
  let bad = false;
  if (!day) {
    dateField.setError(t("worked.needDate"));
    bad = true;
  } else if (day > today.get()) {
    dateField.setError(t("worked.futureDate"));
    bad = true;
  }
  if (state.selected.size === 0) {
    statusHost.dataset.error = "1";
    statusHost.replaceChildren(h("p", { class: "field__error" }, icon("alert"), t("worked.needWard")));
    bad = true;
  }
  if (bad) return;
  sendBtn.disabled = true;
  sendBtn.setAttribute("aria-busy", "true");
  try {
    await api.post("/api/worked", { ward_nos: [...state.selected].sort((a, b) => a - b), worked_on: day });
    state.selected.clear();
    syncChecks();
    statusHost.replaceChildren();
    updateSummary();
    toast(t("worked.saved", { date: formatDate(day) }));
    recentView.reload({ quiet: true });
    wardsView.reload({ quiet: true });
  } catch (err) {
    // The choice stays on screen, marked as not sent.
    statusHost.dataset.error = "1";
    statusHost.replaceChildren(h("p", { class: "field__error" }, icon("alert"), err.message), unsentNote());
  } finally {
    sendBtn.removeAttribute("aria-busy");
    sendBtn.disabled = isOffline() || !writable;
  }
}

document.addEventListener("ct:offline", (e) => {
  sendBtn.disabled = Boolean(e.detail?.offline) || !writable;
  delete statusHost.dataset.error;
  showUnsent();
});

// ------------------------------------------------------------- recent list
function renderRecent(rows) {
  const byDay = new Map();
  for (const r of rows) {
    if (!byDay.has(r.worked_on)) byDay.set(r.worked_on, []);
    byDay.get(r.worked_on).push(r);
  }
  return h(
    "div",
    { class: "list" },
    [...byDay].map(([day, wards]) =>
      h(
        "div",
        { class: "list__row recent-row" },
        h(
          "div",
          { class: "list__main" },
          h("span", { class: "list__title" }, formatDate(day)),
          h("span", { class: "list__meta" }, wards.map((w, i) => [i ? ", " : "", `${w.ward_no} `, bdi(w.ward_name ?? "")])),
        ),
        h("span", { class: "list__side muted small" }, t("worked.wardCount", { count: wards.length })),
      ),
    ),
  );
}
const recentHost = h("div", null);
const recentTitle = h("h2", { class: "section-title" });
bindText(recentTitle, () => t("worked.recent"));
const recentView = dataView(recentHost, {
  load: () => api.get("/api/worked"),
  render: renderRecent,
  isEmpty: (rows) => (rows.length ? null : emptyState({ title: t("worked.recentNone"), iconName: "inbox" })),
});

// ------------------------------------------------------------------- page
const title = h("h1", null);
bindText(title, () => t("worked.title"));
const readNote = h("p", { class: "muted small" });
bindText(readNote, () => t("worked.readOnly"));
readNote.hidden = writable;
if (!writable) {
  lockForm(form);
  sendBtn.disabled = true;
  bindAttr(form, "aria-label", () => t("worked.title"));
}
document.getElementById("main").append(
  h("div", { class: "page-title" }, title),
  readNote,
  h("div", { class: "worked-split" }, form, h("section", { class: "stack stack--s" }, recentTitle, recentHost)),
);
updateSummary();
if (isOffline()) sendBtn.disabled = true;
