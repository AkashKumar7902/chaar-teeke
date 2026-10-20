// A3 Alerts. Newest first. One filled button, "I'm on it", for a vet; Acted or Not acted with a note; the
// vet may ask the clinic to call the patient (the vet never sees a number). The red banner shows only when the
// server says an alert waited unseen by every vet. Live through the shared event stream.

import { api } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { sinceText } from "/js/core/format.js";
import { t } from "/js/core/i18n.js";
import { alertCard, banner, emptyState, toast } from "/js/ui/index.js";
import { noteDialog } from "./team/noteDialog.js";
import { boot, dataView, isVet, personName, todayTracker } from "./team/lib.js";

const me = await boot(["alerts"], "alerts.title");
const today = todayTracker(me);
await today.ready;
const vet = isVet(me);
const PAGE = 12;

const main = document.getElementById("main");
let filter = "open";
let shown = { open: PAGE, closed: PAGE };
let view = null;

const faultHost = h("div", { class: "alerts-fault" });
const listHost = h("div", { class: "stack" });
const openBtn = h("button", { type: "button", class: "seg__btn", onClick: () => pick("open") });
const closedBtn = h("button", { type: "button", class: "seg__btn", onClick: () => pick("closed") });
const seg = h("div", { class: "seg", role: "group" }, openBtn, closedBtn);
const title = h("h1", null);
const readNote = h("p", { class: "muted small" });
const root = h("div", { class: "stack stack--l" }, h("div", { class: "page-title" }, title, seg), faultHost, readNote, listHost);

function pick(next) {
  filter = next;
  view.paint();
}

async function act(fn, done) {
  try {
    const result = await fn();
    if (done) toast(typeof done === "function" ? done(result) : done);
  } catch (err) {
    toast(err.message, { tone: "attention" });
  } finally {
    view.reload({ quiet: true });
  }
}

const claim = (a) => act(() => api.post(`/api/alerts/${a.id}/claim`), t("alerts.claimed"));
// The reply names the clinic that was asked, so the vet knows who will phone the patient.
const callback = (a) => act(() => api.post(`/api/alerts/${a.id}/callback`), (sent) => (sent.clinic_name ? t("alerts.callbackAskedAt", { clinic: sent.clinic_name }) : t("alerts.callbackAsked")));
async function close(a, acted) {
  const note = await noteDialog({
    title: t(acted ? "alerts.dialog.actedTitle" : "alerts.dialog.notActedTitle"),
    label: t(acted ? "alerts.dialog.actedLabel" : "alerts.dialog.notActedLabel"),
    required: !acted,
    confirmLabel: t("alerts.dialog.save"),
  });
  if (note === null) return;
  await act(() => api.post(`/api/alerts/${a.id}/${acted ? "acted" : "not-acted"}`, { note }), t("alerts.closedDone"));
}

function card(a, names) {
  const area = a.cluster_id ? names.get(a.cluster_id) : null;
  // Without a ward the card names the area; with a ward the area is added beside it.
  const shownAlert = { ...a, claimed_by: personName(a.claimed_by), cluster_id: a.ward_name ? a.cluster_id : (area ?? a.cluster_id) };
  const handlers = vet
    ? {
        onClaim: claim,
        onActed: (x) => close(x, true),
        onNotActed: (x) => close(x, false),
        onCallback: a.status === "claimed" ? callback : undefined,
      }
    : {};
  const el = alertCard(shownAlert, handlers);
  if (a.ward_name && area) el.querySelector(".alert-card__facts dd")?.append(" · ", area);
  return el;
}

function paintList(data) {
  const all = data.alerts;
  const open = all.filter((a) => a.status === "new" || a.status === "claimed");
  const closed = all.filter((a) => a.status === "acted" || a.status === "not_acted");
  openBtn.textContent = t("alerts.open", { count: open.length });
  closedBtn.textContent = t("alerts.closed", { count: closed.length });
  seg.setAttribute("aria-label", t("alerts.filter"));
  openBtn.setAttribute("aria-pressed", String(filter === "open"));
  closedBtn.setAttribute("aria-pressed", String(filter === "closed"));
  const rows = filter === "open" ? open : closed;
  if (rows.length === 0) {
    const latest = all[0];
    const none =
      filter === "closed"
        ? t("alerts.noneClosed")
        : latest
          ? t("alerts.none", { since: sinceText(latest.created_at, today.nowDate()) })
          : t("alerts.noneEver");
    listHost.replaceChildren(emptyState({ title: none, iconName: "inbox" }));
    return;
  }
  const limit = shown[filter];
  const nodes = [h("div", { class: "grid alerts-grid" }, rows.slice(0, limit).map((a) => card(a, data.names)))];
  if (rows.length > limit) {
    nodes.push(
      h("div", { class: "row" }, h("button", { type: "button", class: "btn", onClick: () => { shown[filter] += PAGE; view.paint(); } }, t("alerts.more"))),
    );
  }
  listHost.replaceChildren(...nodes);
}

function render(data) {
  title.textContent = t("alerts.title");
  readNote.textContent = vet ? "" : t("alerts.vetsOnly");
  readNote.hidden = vet;
  faultHost.replaceChildren(data.fault ? banner({ text: t("alerts.fault"), tone: "alarm" }) : "");
  paintList(data);
  return root;
}

async function load() {
  // The list first: a vet opening it is what marks alerts as seen, and the system check reads that.
  const [alerts, coverage] = await Promise.all([api.get("/api/alerts"), api.get("/api/coverage")]);
  const status = await api.get("/api/system/status");
  const names = new Map(coverage.clusters.map((c) => [c.id, c.name]));
  const fault = status.checks.some((c) => c.name === "alerts" && c.alarm && !c.ok);
  return { alerts, names, fault };
}

view = dataView(main, { load, render, events: ["alert", "system"], lines: 5 });
// "Waited unseen" is a matter of time, so the check is repeated while the page stays open.
setInterval(() => view.quietReload(), 60_000);
