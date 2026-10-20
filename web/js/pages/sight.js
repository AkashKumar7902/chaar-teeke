// A5 Sight count. Big steppers for notched, not notched and could not see, per ward and day, from the team's
// rounds. Tap, then send. A failed send leaves every number on screen, marked "Not sent"; nothing is queued.

import { api } from "/js/core/api.js";
import { bdi, h } from "/js/core/dom.js";
import { formatCount, formatDate } from "/js/core/format.js";
import { t } from "/js/core/i18n.js";
import { emptyState, field, icon, selectInput, stepper, toast, unsentNote } from "/js/ui/index.js";
import { bindText, boot, factLine, canRecord, dataView, isOffline, loadWards, lockForm, onLang, todayTracker, wardLabel } from "./team/lib.js";

const me = await boot(["sight"], "sight.title");
const today = todayTracker(me);
await today.ready;
const writable = canRecord(me);
const state = { ward: "" };

// ---------------------------------------------------------------- the form
const dateInput = h("input", { class: "input", type: "date", name: "counted_on", max: today.get(), value: today.get(), required: true });
const dateField = field({ label: t("sight.date"), control: dateInput });
bindText(dateField.el.querySelector(".field__label"), () => t("sight.date"));

const wardHost = h("div", { class: "sight-ward" });
let wardField = null;
function renderWard(wards) {
  const control = selectInput({
    name: "ward_no",
    value: state.ward,
    options: [{ value: "", label: t("sight.chooseWard") }, ...wards.map((w) => ({ value: String(w.ward_no), label: wardLabel(w) }))],
  });
  control.addEventListener("change", () => {
    state.ward = control.value;
    wardField?.setError(null);
  });
  wardField = field({ label: t("sight.ward"), control });
  if (!writable) lockForm(wardField.el);
  return wardField.el;
}
dataView(wardHost, { load: loadWards, render: renderWard, lines: 2, keepOnOffline: true });

const defs = [
  ["notched", "sight.notched"],
  ["unnotched", "sight.unnotched"],
  ["unsure", "sight.unsure"],
];
const steppers = defs.map(([key, label]) => {
  const s = stepper({ label: t(label), value: 0, min: 0, max: 9999, onChange: () => touched() });
  const relabel = () => {
    s.el.querySelector(".stepper__label").textContent = t(label);
    const [less, more] = s.el.querySelectorAll(".stepper__btn");
    less.setAttribute("aria-label", t("buttons.fewer", { label: t(label) }));
    more.setAttribute("aria-label", t("buttons.more", { label: t(label) }));
  };
  onLang(relabel);
  s.el.classList.add("sight-step");
  return { key, s };
});
const total = () => steppers.reduce((a, { s }) => a + s.get(), 0);

const statusHost = h("div", { class: "stack stack--s", role: "status" });
function touched() {
  if (!statusHost.dataset.error) showUnsent();
}
function showUnsent() {
  statusHost.replaceChildren(isOffline() && total() > 0 ? unsentNote() : "");
}

const sendLabel = h("span", null);
bindText(sendLabel, () => t("sight.send"));
const sendBtn = h("button", { type: "submit", class: "btn btn--primary btn--lg" }, sendLabel);

const form = h(
  "form",
  { class: "card stack sight-form", novalidate: true, onSubmit: (e) => { e.preventDefault(); send(); } },
  h("div", { class: "sight-form__where" }, wardHost, dateField.el),
  h("div", { class: "sight-steps" }, steppers.map(({ s }) => s.el)),
  statusHost,
  h("div", { class: "row" }, sendBtn),
);

async function send() {
  delete statusHost.dataset.error;
  dateField.setError(null);
  wardField?.setError(null);
  const day = dateInput.value;
  let bad = false;
  if (!state.ward) {
    wardField?.setError(t("sight.needWard"));
    bad = true;
  }
  if (!day) {
    dateField.setError(t("sight.needDate"));
    bad = true;
  } else if (day > today.get()) {
    dateField.setError(t("sight.futureDate"));
    bad = true;
  }
  if (total() === 0) {
    statusHost.dataset.error = "1";
    statusHost.replaceChildren(h("p", { class: "field__error" }, icon("alert"), t("sight.needCount")));
    bad = true;
  }
  if (bad) return;
  sendBtn.disabled = true;
  sendBtn.setAttribute("aria-busy", "true");
  try {
    const body = { ward_no: Number(state.ward), counted_on: day };
    for (const { key, s } of steppers) body[key] = s.get();
    await api.post("/api/sight-counts", body);
    for (const { s } of steppers) s.set(0);
    statusHost.replaceChildren();
    toast(t("sight.sent", { ward: state.ward }));
    recentView.reload({ quiet: true });
  } catch (err) {
    // Every number stays where it is, marked as not sent.
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
let recentLimit = 10;
function renderRecent(rows) {
  const list = h(
    "div",
    { class: "list" },
    rows.slice(0, recentLimit).map((r) =>
      h(
        "div",
        { class: "list__row recent-row" },
        h(
          "div",
          { class: "list__main" },
          h("span", { class: "list__title" }, `${r.ward_no} `, bdi(r.ward_name ?? "")),
          h("span", { class: "list__meta" }, factLine([`${t("sight.notched")} ${formatCount(r.notched)}`, `${t("sight.unnotched")} ${formatCount(r.unnotched)}`, `${t("sight.unsure")} ${formatCount(r.unsure)}`])),
        ),
        h("span", { class: "list__side muted small" }, formatDate(r.counted_on)),
      ),
    ),
  );
  if (rows.length <= recentLimit) return list;
  return h("div", { class: "stack stack--s" }, list, h("div", { class: "row" }, h("button", { type: "button", class: "btn", onClick: () => { recentLimit += 10; recentView.paint(); } }, t("sight.more"))));
}
const recentHost = h("div", null);
const recentView = dataView(recentHost, {
  load: () => api.get("/api/sight-counts"),
  render: renderRecent,
  isEmpty: (rows) => (rows.length ? null : emptyState({ title: t("sight.recentNone"), iconName: "inbox" })),
});

// -------------------------------------------------------------------- page
const title = bindText(h("h1", null), () => t("sight.title"));
const recentTitle = bindText(h("h2", { class: "section-title" }), () => t("sight.recent"));
const readNote = bindText(h("p", { class: "muted small" }), () => t("sight.readOnly"));
readNote.hidden = writable;
if (!writable) {
  lockForm(form);
  sendBtn.disabled = true;
}
if (isOffline()) sendBtn.disabled = true;
document.getElementById("main").append(
  h("div", { class: "page-title" }, title),
  readNote,
  h("div", { class: "sight-split" }, form, h("section", { class: "stack stack--s" }, recentTitle, recentHost)),
);
