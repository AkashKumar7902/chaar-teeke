// C1 Today and C4 vaccine out/in. One job: see who is due and who missed, and act on the few things that
// need a tap (link a missed call, answer a call-back request, say the vaccine is out).
// GET /api/clinic/today is the record; the event stream only says "look again".

import { ApiError, api } from "/js/core/api.js";
import { bdi, h, replace } from "/js/core/dom.js";
import { formatDate, formatDateTime, formatTime, doseLabel, istDate, maskedPhone, sinceText } from "/js/core/format.js";
import { mountFrame } from "/js/core/frame.js";
import { initI18n, t } from "/js/core/i18n.js";
import { confirmDialog, emptyState, icon, infoButton, openDialog, pill, statusPill, textInput, field, toast } from "/js/ui/index.js";
import { clinicParam, inlineError, keepFocus, langName, liveRegion, loadConfig, pageTitle, recalledView, rememberView, runWrite, serverNow, watchClinic, withClinic, writeAttrs } from "./clinic/common.js";
import { formDialog } from "./course/dialog.js";

const PAGE = 30; // rows shown before "Show more"
const LISTS = [
  ["due", "due_today"],
  ["missed", "missed"],
  ["waiting", "awaiting_call"],
  ["next", "next_due"],
];
const VISITOR_DESK = "cln_demo_visitor"; // the demo clinic a browser phone's patient is linked to
const SHOW_NUMBER_MS = 30000;
const SEARCH_WAIT_MS = 250;

await initI18n({ bundles: ["common", "clinic"], audience: "staff" });
const { me } = await mountFrame({ mode: "staff" });
document.title = pageTitle("clinic.title");

const recalled = recalledView();
const state = {
  data: null,
  now: null,
  config: null,
  sel: recalled?.sel ?? null,
  chosen: Boolean(recalled?.sel),
  showDone: Boolean(recalled?.showDone), // doses already given are in the list only when asked for
  scroll: recalled?.scroll ?? 0, // put back once, on the first paint
  revealed: null, // {id, phone}: a call-back's number, shown for a moment
  shown: PAGE,
};
const slots = {};
const main = document.getElementById("main");
const host = h("div", { class: "stack stack--l" });
main.append(host);

/**
 * The clinic whose list is shown. A visitor or the administrator opens the demo visitor desk when the visitor's own
 * (browser-phone) patient is there, so the course they just started is the first thing on the page. A clinic
 * login has one clinic; an address with ?clinic_id= is what was asked for.
 */
async function resolveClinic() {
  const asked = clinicParam();
  if (asked || me.role === "clinic" || me.mode !== "demo") return asked;
  const config = await loadConfig(undefined);
  if (!config.clinics.some((c) => c.id === VISITOR_DESK)) return undefined;
  const desk = await api.get("/api/clinic/today", { clinic_id: VISITOR_DESK });
  const hasSimulatorCourse = LISTS.some(([, key]) => (desk[key] ?? []).some((r) => r.source === "simulator"));
  if (!hasSimulatorCourse) return undefined;
  history.replaceState(null, "", withClinic("/clinic", VISITOR_DESK));
  return VISITOR_DESK;
}

const live = liveRegion(host, {
  load: async () => {
    const clinicId = await resolveClinic();
    const [today, now, config] = await Promise.all([
      api.get("/api/clinic/today", { clinic_id: clinicId }),
      serverNow(me),
      // Only a visitor or the administrator can switch clinics; a clinic login has exactly one.
      me.role === "clinic" ? Promise.resolve(null) : loadConfig(clinicId).catch(() => null),
    ]);
    return { today, now, config };
  },
  build,
  update: ({ today, now, config }) => {
    state.data = today;
    state.now = now;
    state.config = config;
    paint();
  },
});

watchClinic(
  () => live.reload({ quiet: true }),
  (offline) => (offline ? showOffline() : live.reload({ quiet: true })),
);
document.addEventListener("ct:langchange", () => {
  document.title = pageTitle("clinic.title");
  live.drop();
  live.reload({ quiet: true });
});
live.reload();

// ------------------------------------------------------------------ shell
function build() {
  slots.head = h("div", { class: "stack stack--s" });
  slots.vaccine = h("section", { class: "card vaccine", "aria-labelledby": "vaccine-title" });
  slots.tiles = h("div", { class: "stats tiles", role: "group", "aria-label": t("clinic.tiles.label") });
  slots.callbacks = h("section", { class: "card card--attention", id: "clinic-callbacks", tabindex: "-1", hidden: true });
  slots.unlinked = h("section", { class: "card card--attention", id: "clinic-unlinked", tabindex: "-1", hidden: true });
  slots.listHead = h("div", { class: "list-head" });
  slots.search = searchBox();
  slots.list = h("div", { class: "stack stack--s", "aria-live": "polite" });
  return h(
    "div",
    { class: "stack stack--l" },
    slots.head,
    h(
      "div",
      { class: "split clinic__grid" },
      h("div", { class: "stack" }, slots.search.el, slots.tiles, slots.vaccine, slots.callbacks, slots.unlinked),
      h("section", { class: "stack", "aria-labelledby": "list-title" }, slots.listHead, slots.list),
    ),
  );
}

function showOffline() {
  live.drop();
  host.replaceChildren(emptyState({ title: t("clinic.offline.title"), tone: "attention", iconName: "offline" }));
}

// ------------------------------------------------------------------ paint
function paint() {
  const d = state.data;
  for (const [, listKey] of LISTS) {
    if (d.counts[listKey] !== undefined && d.counts[listKey] !== rowsFor(listKey).length) console.error(`clinic: count ${listKey} ${d.counts[listKey]} does not match its list (${rowsFor(listKey).length})`);
  }
  if (!state.restored) {
    state.restored = true;
    // what was remembered belongs to the clinic it was on
    if (recalled && recalled.clinic !== d.clinic_id) Object.assign(state, { sel: null, chosen: false, showDone: false, scroll: 0 });
  }
  if (d.counts.unlinked !== d.unlinked.length) console.error("clinic: unlinked count does not match its list");
  if (d.counts.callbacks !== d.callbacks.length) console.error("clinic: callbacks count does not match its list");
  if (!state.chosen || !lists().some(([k]) => k === state.sel)) state.sel = (lists().find(([, k]) => rowsFor(k).length > 0) ?? LISTS[0])[0];
  keepFocus(host, () => {
    paintHead();
    paintVaccine();
    paintTiles();
    paintCallbacks();
    paintUnlinked();
    paintListHead();
    paintList();
  });
  if (state.scroll) {
    const top = state.scroll;
    state.scroll = 0;
    requestAnimationFrame(() => scrollTo(0, top));
  }
}

function paintHead() {
  const d = state.data;
  const clinics = state.config?.clinics ?? [];
  const where =
    clinics.length > 1
      ? Object.assign(field({ label: t("clinic.clinicLabel"), control: switcher(clinics, d.clinic_id) }).el, { className: "field switch" })
      : h("p", { class: "muted" }, formatDate(d.local_date), " · ", bdi(d.clinic_name));
  replace(slots.head,
    h(
      "div",
      { class: "page-title row--between" },
      h("h1", null, t("clinic.title")),
      h(
        "div",
        { class: "row" },
        h("a", { class: "btn", href: withClinic("/clinic/sticker", d.clinic_id) }, icon("print"), t("clinic.stickers")),
        h("a", { class: "btn btn--primary", href: withClinic("/clinic/enrol", clinicParam()) }, icon("plus"), t("clinic.enrol")),
      ),
    ),
    where,
    clinics.length > 1 ? h("p", { class: "muted" }, formatDate(d.local_date)) : null,
  );
}

function switcher(clinics, current) {
  const select = h(
    "select",
    { class: "select", name: "clinic", "data-key": "clinic" },
    clinics.map((c) => h("option", { value: c.id, selected: c.id === current ? true : null }, c.name)),
  );
  select.addEventListener("change", () => location.assign(withClinic("/clinic", select.value)));
  return select;
}

// -------------------------------------------------------------- vaccine (C4)
function paintVaccine() {
  const d = state.data;
  const out = d.vaccine_out;
  const pick = (wantOut, label) =>
    h(
      "button",
      {
        type: "button",
        class: "seg__btn",
        "aria-pressed": String(out === wantOut),
        "data-key": wantOut ? "vaccine-out" : "vaccine-in",
        ...writeAttrs(me),
        onClick: (event) => setVaccine(event.currentTarget, wantOut),
      },
      label,
    );
  const line = !out
    ? t("clinic.vaccine.inLine")
    : d.vaccine_out_since
      ? t("clinic.vaccine.outLine", { since: sinceText(d.vaccine_out_since, state.now) })
      : t("clinic.vaccine.outNoTime");
  slots.vaccine.dataset.out = String(out);
  slots.vaccine.replaceChildren(
    h(
      "div",
      { class: "vaccine__top" },
      h(
        "div",
        { class: "card__head" },
        h("h2", { id: "vaccine-title" }, t("clinic.vaccine.title")),
        infoButton({ topic: t("clinic.vaccine.topic"), title: t("clinic.vaccine.title"), content: h("p", null, t("clinic.vaccine.explain")) }),
      ),
      h("div", { class: "seg", role: "group", "aria-label": t("clinic.vaccine.title") }, pick(false, t("clinic.vaccine.in")), pick(true, t("clinic.vaccine.out"))),
    ),
    h("p", { class: out ? "vaccine__line vaccine__line--out" : "vaccine__line" }, out ? icon("alert") : null, h("span", null, line)),
  );
}

async function setVaccine(button, out) {
  const d = state.data;
  if (out === d.vaccine_out) return;
  // A visitor cannot change anything, so there is nothing to confirm: the server's refusal is the answer.
  if (out && me.can_write) {
    const ok = await confirmDialog({ title: t("clinic.vaccine.confirmTitle"), body: t("clinic.vaccine.confirmBody"), confirmLabel: t("clinic.vaccine.confirmOut") });
    if (!ok) return;
  }
  await runWrite(button, async () => {
    await api.post(`/api/clinics/${encodeURIComponent(d.clinic_id)}/vaccine`, { out });
    await live.reload({ quiet: true });
  });
}

// ------------------------------------------------------------------ counts
/** The rows of one server list. "Next due" is a list only when the server sends it. */
const rowsFor = (listKey) => state.data[listKey] ?? [];
const lists = () => LISTS.filter(([, listKey]) => Array.isArray(state.data[listKey]));
const isGiven = (r) => r.state === "marked_given";

// People still to act on come first; those who say they took it, then those already marked given, go to the
// end. The order inside each group is the server's.
const DONE_RANK = { patient_says_taken: 1, marked_given: 2 };
function listRows(key) {
  return [...rowsFor(LISTS.find(([k]) => k === key)[1])].sort((a, b) => (DONE_RANK[a.state] ?? 0) - (DONE_RANK[b.state] ?? 0));
}

function paintTiles() {
  const d = state.data;
  const given = d.due_today.filter(isGiven).length;
  const left = d.due_today.length - given;
  const tile = (key, { n, unit = null, label, aria = null, attention = false }) => {
    const cls = ["stat", "tile", attention && n > 0 ? "stat--attention" : ""];
    const body = [h("span", { class: "stat__n" }, String(n), unit ? [" ", h("span", { class: "stat__unit" }, unit)] : null), h("span", { class: "stat__label" }, label)];
    return h("button", { type: "button", class: cls, "aria-pressed": String(state.sel === key), "aria-label": aria, "data-key": `tile-${key}`, onClick: () => choose(key) }, body);
  };
  // "Due today" counts doses already given, so the tile says how many are left and how many are done.
  const due = tile("due", { n: left, unit: t("clinic.tiles.left"), label: given > 0 ? t("clinic.tiles.dueGiven", { given }) : t("clinic.tiles.due"), aria: t("clinic.tiles.dueAria", { left, given }) });
  slots.tiles.replaceChildren(
    due,
    tile("missed", { n: d.missed.length, label: t("clinic.tiles.missed"), attention: true }),
    tile("waiting", { n: d.awaiting_call.length, label: t("clinic.tiles.waiting") }),
    ...(Array.isArray(d.next_due) ? [tile("next", { n: d.next_due.length, label: t("clinic.tiles.next") })] : []),
  );
}

function choose(key) {
  state.sel = key;
  state.chosen = true;
  state.shown = PAGE;
  keepFocus(host, () => {
    paintTiles();
    paintListHead();
    paintList();
  });
  // On a phone the list sits below the tasks; show it so the tap has a visible effect.
  if (globalThis.matchMedia("(max-width: 63.99rem)").matches) {
    slots.listHead.scrollIntoView({ block: "start", behavior: globalThis.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }
}

/** Keep the tile and the scroll for this tab, so a patient opened from the list and left returns here. */
function remember() {
  if (state.data) rememberView({ clinic: state.data.clinic_id, sel: state.sel, showDone: state.showDone, scroll: Math.round(scrollY) });
}
addEventListener("pagehide", remember);

// ------------------------------------------------------------ find a patient
/** The register-number search above the tiles: any active or waiting patient of this clinic, due today or not. */
function searchBox() {
  const input = textInput({ name: "find", type: "search", inputmode: "search", autocomplete: "off", placeholder: t("clinic.find") });
  const found = h("div", { class: "stack stack--s", "aria-live": "polite" });
  const box = field({ label: t("clinic.find"), control: input });
  box.el.classList.add("find");
  box.el.querySelector("label")?.classList.add("visually-hidden");
  const form = h("form", { class: "stack stack--s", role: "search", onSubmit: (event) => submit(event) }, box.el, found);
  let timer = null;
  let ticket = 0;

  async function run() {
    const mine = (ticket += 1);
    const q = input.value.trim();
    if (!q) {
      replace(found);
      return [];
    }
    try {
      const rows = await api.get("/api/clinic/courses", { q, clinic_id: state.data?.clinic_id });
      if (mine !== ticket) return [];
      replace(found, rows.length ? h("div", { class: "list find__results" }, rows.map((r) => patientRow(r, "search"))) : emptyState({ title: t("clinic.noMatch") }));
      return rows;
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      if (mine === ticket) replace(found, inlineError(err.message));
      return [];
    }
  }
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(run, SEARCH_WAIT_MS);
  });
  async function submit(event) {
    event.preventDefault();
    clearTimeout(timer);
    const rows = await run();
    if (rows.length === 1) location.assign(`/clinic/p/${encodeURIComponent(rows[0].course_id)}`);
  }
  return { el: form };
}

// -------------------------------------------------- call-back requests (C1)
function paintCallbacks() {
  const rows = state.data.callbacks;
  slots.callbacks.hidden = rows.length === 0;
  if (rows.length === 0) return slots.callbacks.replaceChildren();
  slots.callbacks.replaceChildren(
    h(
      "div",
      { class: "card__head" },
      h("h2", null, t("clinic.callbacks.title", { count: rows.length })),
      infoButton({ topic: t("clinic.callbacks.topic"), title: t("clinic.callbacks.title", { count: rows.length }), content: h("p", null, t("clinic.callbacks.explain")) }),
    ),
    h("p", { class: "muted" }, t("clinic.callbacks.ask")),
    h(
      "ul",
      { class: "tasks" },
      rows.map((r) => callbackItem(r)),
    ),
  );
}

function callbackItem(r) {
  const who = r.register_no ? bdi(r.register_no) : t("clinic.noRegister");
  const number = state.revealed?.id === r.id ? state.revealed.phone : null;
  return h(
    "li",
    { class: "task" },
    h(
      "div",
      { class: "task__main" },
      r.course_id ? h("a", { class: "task__title", href: `/clinic/p/${encodeURIComponent(r.course_id)}` }, h("span", null, t("clinic.registerNo", { no: "" }).trim(), " ", who)) : h("span", { class: "task__title" }, who),
      h("span", { class: "list__meta" }, t("clinic.callbacks.asked", { when: whenText(r.requested_at) })),
      number ? h("span", { class: "list__meta" }, t("clinic.callbacks.shown", { s: SHOW_NUMBER_MS / 1000 })) : null,
    ),
    h(
      "div",
      { class: "task__actions" },
      r.course_id
        ? number
          ? h("a", { class: "btn btn--primary", "data-key": `cb-call-${r.id}`, href: `tel:${number.replace(/[^\d+]/g, "")}` }, icon("phone"), h("span", { class: "num", dir: "ltr" }, number))
          : h("button", { type: "button", class: "btn", "data-key": `cb-show-${r.id}`, ...writeAttrs(me), onClick: (event) => revealNumber(r, event.currentTarget) }, icon("phone"), t("clinic.callbacks.show"))
        : null,
      h("button", { type: "button", class: "btn", "data-key": `cb-done-${r.id}`, ...writeAttrs(me), onClick: (event) => closeCallback(r, "done", event.currentTarget) }, icon("check"), t("clinic.callbacks.done")),
      h("button", { type: "button", class: "btn", "data-key": `cb-no-${r.id}`, ...writeAttrs(me), onClick: (event) => closeCallback(r, "closed_no_contact", event.currentTarget) }, t("clinic.callbacks.noContact")),
    ),
  );
}

let hideNumberTimer = null;
/** The number is asked for, and the ask is logged by the server; it is shown for a short time, then forgotten. */
async function revealNumber(r, button) {
  await runWrite(button, async () => {
    const { phone } = await api.post(`/api/courses/${encodeURIComponent(r.course_id)}/reveal-phone`);
    state.revealed = { id: r.id, phone };
    clearTimeout(hideNumberTimer);
    hideNumberTimer = setTimeout(() => {
      state.revealed = null;
      keepFocus(host, paintCallbacks);
    }, SHOW_NUMBER_MS);
    keepFocus(host, paintCallbacks);
    host.querySelector(`[data-key="cb-call-${r.id}"]`)?.focus();
  });
}

/** Done or could not reach, with an optional one-line note that the vet's team can read on the alert. */
async function closeCallback(r, outcome, trigger) {
  const note = textInput({ name: "note", maxlength: 300, autocomplete: "off" });
  note.id = "cb-note";
  const errors = h("div", { role: "status" });
  const body = h("div", { class: "stack" }, h("div", { class: "field" }, h("label", { class: "field__label", for: "cb-note" }, t("clinic.callbacks.noteLabel")), note), errors);
  await formDialog({
    title: t(outcome === "done" ? "clinic.callbacks.doneTitle" : "clinic.callbacks.noContactTitle"),
    body,
    confirmLabel: t("clinic.callbacks.save"),
    onConfirm: async (button) => {
      const text = note.value.trim();
      const saved = await runWrite(
        button,
        async () => {
          await api.post(`/api/callbacks/${encodeURIComponent(r.id)}/done`, text ? { outcome, note: text } : { outcome }, { quiet: true });
          return true;
        },
        { onError: (err) => replace(errors, inlineError(err.message)) },
      );
      if (saved) {
        if (state.revealed?.id === r.id) state.revealed = null;
        await live.reload({ quiet: true });
      }
      return Boolean(saved);
    },
  });
  if (trigger.isConnected) trigger.focus();
}

function whenText(iso) {
  return istDate(iso) === istDate(state.now) ? formatTime(iso) : formatDateTime(iso);
}

// ------------------------------------------------------ unlinked missed calls
function paintUnlinked() {
  const rows = state.data.unlinked;
  slots.unlinked.hidden = rows.length === 0;
  if (rows.length === 0) return slots.unlinked.replaceChildren();
  slots.unlinked.replaceChildren(
    h(
      "div",
      { class: "card__head" },
      h("h2", null, t("clinic.unlinked.title", { count: rows.length })),
      infoButton({ topic: t("clinic.unlinked.topic"), title: t("clinic.unlinked.title", { count: rows.length }), content: h("p", null, t("clinic.unlinked.explain")) }),
    ),
    h(
      "ul",
      { class: "tasks" },
      rows.map((m) =>
        h(
          "li",
          { class: "task task--one" },
          h(
            "div",
            { class: "task__main" },
            h("span", { class: "task__title num" }, whenText(m.at)),
            h("span", { class: "list__meta" }, maskedPhone(m.phone_last4), m.transport === "sim" ? [" ", pill({ tone: "simulated", iconName: "phone", text: t("status.source.simulator") })] : null),
          ),
          h("div", { class: "task__actions" }, h("button", { type: "button", class: "btn", "data-key": `link-${m.id}`, ...writeAttrs(me), onClick: (event) => linkDialog(m, event.currentTarget) }, t("clinic.unlinked.link"))),
        ),
      ),
    ),
  );
}

async function linkDialog(call, trigger) {
  const waiting = state.data.awaiting_call;
  const errors = h("div");
  let body;
  if (waiting.length === 0) {
    body = h("div", { class: "stack" }, h("p", null, t("clinic.link.none")), h("a", { class: "btn", href: withClinic("/clinic/enrol", clinicParam()) }, icon("plus"), t("clinic.enrol")));
  } else {
    body = h(
      "div",
      { class: "stack stack--s" },
      h("p", null, t("clinic.link.pick", { when: whenText(call.at), phone: maskedPhone(call.phone_last4) })),
      errors,
      h(
        "ul",
        { class: "list pick" },
        waiting.map((r) =>
          h(
            "li",
            null,
            h(
              "button",
              {
                type: "button",
                class: "list__row pick__row",
                ...writeAttrs(me),
                onClick: (event) => {
                  const button = event.currentTarget;
                  runWrite(
                    button,
                    async () => {
                      await api.post(`/api/missed-calls/${encodeURIComponent(call.id)}/link`, { course_id: r.course_id }, { quiet: true });
                      button.closest("dialog")?.close();
                      toast(t("clinic.link.done", { no: r.register_no ?? t("clinic.noRegister") }));
                      await live.reload({ quiet: true });
                    },
                    { onError: (err) => errors.replaceChildren(inlineError(err.message)) },
                  );
                },
              },
              h("span", { class: "prow__no num" }, r.register_no ? bdi(r.register_no) : t("clinic.noRegister")),
              h("span", { class: "list__meta" }, langName(r.lang)),
            ),
          ),
        ),
      ),
    );
  }
  await openDialog({ title: t("clinic.link.title"), body, actions: [{ label: t("buttons.cancel"), value: false }] });
  trigger.isConnected ? trigger.focus() : null;
}

// ------------------------------------------------------------ patient lists
function paintListHead() {
  const given = state.sel === "due" ? state.data.due_today.filter(isGiven).length : 0;
  replace(
    slots.listHead,
    h("h2", { id: "list-title" }, t(`clinic.lists.${state.sel}`)),
    given > 0
      ? h(
          "button",
          {
            type: "button",
            class: "btn",
            "aria-pressed": String(state.showDone),
            "data-key": "show-done",
            onClick: () => {
              state.showDone = !state.showDone;
              state.shown = PAGE;
              keepFocus(host, () => {
                paintListHead();
                paintList();
              });
            },
          },
          state.showDone ? t("clinic.hideDone") : t("clinic.showDone", { count: given }),
        )
      : null,
  );
}

function paintList() {
  const d = state.data;
  const all = listRows(state.sel);
  // Doses already given are not work: they are in the list only when asked for.
  const rows = state.showDone ? all : all.filter((r) => !isGiven(r));
  if (rows.length === 0) return replace(slots.list, emptyFor(state.sel, d, all.length));
  const shown = rows.slice(0, state.shown);
  const more = rows.length - shown.length;
  replace(slots.list,
    h("div", { class: "list" }, shown.map((r) => patientRow(r, state.sel))),
    more > 0
      ? h("button", { type: "button", class: "btn", "data-key": "more", onClick: () => { state.shown += PAGE; paintList(); } }, t("clinic.showMore", { count: Math.min(more, PAGE) }))
      : null,
  );
}

function emptyFor(key, d, given) {
  const checked = t("states.lastChecked", { when: formatTime(state.now) });
  if (key === "due" && given > 0) return emptyState({ title: t("clinic.empty.dueDone"), body: checked });
  if (key === "due") {
    const next = d.next_due_date ? t("clinic.empty.dueNext", { date: formatDate(d.next_due_date) }) : null;
    return emptyState({ title: t("clinic.empty.due"), body: [next, next ? " " : null, checked].filter(Boolean).join("") });
  }
  return emptyState({ title: t(`clinic.empty.${key}`), body: checked });
}

/** "Reminder called 18:30": the last call to a patient who is due soon, and whether it got through. */
function lastCallText(r) {
  if (!r.last_call_at) return t("clinic.lastCall.none");
  const through = r.last_call_status === "answered" || r.last_call_status === "completed";
  return t(through ? "clinic.lastCall.called" : "clinic.lastCall.tried", { kind: t(`clinic.callKind.${r.last_call_kind}`), time: whenText(r.last_call_at) });
}

/** A due or late patient with a dose to mark can be marked from the list, without opening the patient. */
const canMark = (r, kind) => (kind === "due" || kind === "missed") && r.next_dose_no && r.state !== "awaiting_call" && !isGiven(r);

async function markGiven(r, button) {
  await runWrite(button, async () => {
    await api.post(`/api/courses/${encodeURIComponent(r.course_id)}/doses/${r.next_dose_no}/given`, {});
    toast(t("clinic.markedToast", { n: r.next_dose_no, no: r.register_no ?? t("clinic.noRegister") }));
    await live.reload({ quiet: true });
  });
}

function patientRow(r, kind) {
  const meta = [];
  if (r.next_dose_no) meta.push(doseLabel(r.next_dose_no));
  if (kind === "missed" && r.next_due_date) meta.push(t("clinic.wasDue", { date: formatDate(r.next_due_date) }));
  if (kind === "next" && r.next_due_date) meta.push(t("clinic.dueOn", { date: formatDate(r.next_due_date) }));
  if (kind === "missed" && r.attempts > 0) meta.push(t("status.clinicRow.tried", { count: r.attempts }));
  if (kind === "next") meta.push(lastCallText(r));
  if (r.state !== "awaiting_call") meta.push(maskedPhone(r.phone_last4));
  meta.push(langName(r.lang));
  const no = r.register_no ? bdi(r.register_no) : t("clinic.noRegister");
  return h(
    "div",
    { class: "list__row prow" },
    h(
      "a",
      { class: "prow__link", href: `/clinic/p/${encodeURIComponent(r.course_id)}` },
      h("span", { class: "prow__no num" }, no),
      h("span", { class: "prow__meta list__meta" }, meta.join(" · ")),
    ),
    h(
      "span",
      { class: "prow__state list__side" },
      r.source === "simulator" ? pill({ tone: "simulated", iconName: "phone", text: t("status.source.simulator") }) : null,
      kind === "next" ? null : statusPill("clinicRow", r.state, { count: r.attempts }),
      canMark(r, kind)
        ? h(
            "button",
            { type: "button", class: "btn prow__given", "data-key": `given-${r.course_id}`, "aria-label": t("clinic.givenFor", { dose: r.next_dose_no, no: r.register_no ?? t("clinic.noRegister") }), ...writeAttrs(me), onClick: (event) => markGiven(r, event.currentTarget) },
            icon("check"),
            t("clinic.given"),
          )
        : null,
    ),
  );
}
