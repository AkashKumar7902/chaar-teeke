// C3 Patient. One job: mark a dose. The timeline, the call log, "Dose given today", "Stop reminders" and a
// logged tap-to-reveal for the full number. Place and dog answers are never here: the API does not send them.

import { api, ApiError } from "/js/core/api.js";
import { bdi, h, replace } from "/js/core/dom.js";
import { formatDate, formatDateTime, formatTime, doseLabel, istDate, maskedPhone } from "/js/core/format.js";
import { mountFrame } from "/js/core/frame.js";
import { initI18n, t } from "/js/core/i18n.js";
import { sharedStream } from "/js/core/sse.js";
import { emptyState, icon, infoButton, pill, statusPill, textInput, toast } from "/js/ui/index.js";
import { inlineError, keepFocus, langName, pageTitle, regName, runWrite, serverNow, withClinic, writeAttrs } from "./clinic/common.js";
import { formDialog } from "./course/dialog.js";

await initI18n({ bundles: ["common", "clinic", "course"], audience: "staff" });
const { me } = await mountFrame({ mode: "staff" });

const courseId = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() ?? "");
const SHOW_NUMBER_MS = 30000;
const REASONS = [
  // The words sent to the server and kept in the audit log; the labels on screen are translated.
  ["asked", "Patient asked to stop"],
  ["finished", "Patient finished the course elsewhere"],
  ["wrong", "Wrong number or wrong patient"],
];

const host = h("div", { class: "stack stack--l" });
document.getElementById("main").append(host);
const ui = { course: null, now: null, revealed: null, timer: null, ticket: 0, built: false, offline: false };
const slots = {};

document.title = pageTitle("course.title");
load();
const stream = sharedStream();
let nudgeTimer = null;
const nudge = () => {
  clearTimeout(nudgeTimer);
  nudgeTimer = setTimeout(() => load({ quiet: true }), 350);
};
for (const type of ["call", "dose", "course"]) {
  stream.on(type, (event) => {
    if (event.data.course_id === courseId) nudge();
  });
}
stream.onResync(() => load({ quiet: true }));
document.addEventListener("ct:offline", (event) => {
  if (event.detail.offline) {
    ui.ticket += 1;
    ui.built = false;
    clearReveal();
    host.replaceChildren(emptyState({ title: t("course.offline.title"), tone: "attention", iconName: "offline" }));
  } else load({ quiet: true });
});
document.addEventListener("ct:langchange", () => {
  document.title = pageTitle("course.title");
  ui.built = false;
  load({ quiet: true });
});

// ------------------------------------------------------------------- load
async function load({ quiet = false } = {}) {
  const mine = (ui.ticket += 1);
  if (!quiet || !ui.built) host.replaceChildren(skeletonView());
  try {
    const [course, now] = await Promise.all([api.get(`/api/courses/${encodeURIComponent(courseId)}`), serverNow(me)]);
    if (mine !== ui.ticket) return;
    ui.course = course;
    ui.now = now;
    if (!ui.built) {
      host.replaceChildren(build());
      ui.built = true;
    }
    paint();
  } catch (err) {
    if (mine !== ui.ticket) return;
    ui.built = false;
    if (!(err instanceof ApiError)) console.error(err);
    host.replaceChildren(failureView(err));
  }
}

function skeletonView() {
  return h("div", { class: "skeleton", "aria-hidden": "true" }, [1, 2, 3, 4, 5].map(() => h("span", { class: "skeleton__line" })));
}

function failureView(err) {
  if (err instanceof ApiError && err.status === 404) {
    return emptyState({ title: err.message, action: h("a", { class: "btn", href: "/clinic" }, t("course.backToday")) });
  }
  if (err instanceof ApiError && err.code === "network") {
    return emptyState({ title: t("states.offline"), tone: "attention", iconName: "offline", action: h("button", { type: "button", class: "btn", onClick: () => load() }, icon("refresh"), t("buttons.retry")) });
  }
  if (err instanceof ApiError && err.isForbidden) {
    return emptyState({ title: err.code === "forbidden" && err.message ? err.message : t("states.forbidden"), iconName: "info", action: h("a", { class: "btn", href: "/clinic" }, t("course.backToday")) });
  }
  return emptyState({
    title: err instanceof ApiError ? err.message : t("errors.generic"),
    tone: "attention",
    iconName: "offline",
    action: h("button", { type: "button", class: "btn", onClick: () => load() }, icon("refresh"), t("buttons.retry")),
  });
}

// ------------------------------------------------------------------ shell
function build() {
  slots.head = h("div", { class: "stack stack--s" });
  slots.dose = h("section", { class: "card stack", "aria-labelledby": "dose-title" });
  slots.timeline = h("section", { class: "stack stack--s", "aria-labelledby": "timeline-title" });
  slots.facts = h("section", { class: "stack stack--s", "aria-labelledby": "facts-title" });
  slots.calls = h("section", { class: "stack stack--s", "aria-labelledby": "calls-title" });
  slots.stop = h("div", { class: "course__stop" });
  return h(
    "div",
    { class: "stack stack--l" },
    slots.head,
    h("div", { class: "split course__grid" }, h("div", { class: "stack stack--l" }, slots.dose, slots.timeline, slots.facts, slots.stop), slots.calls),
  );
}

function paint() {
  keepFocus(host, () => {
    paintHead();
    paintDose();
    paintTimeline();
    paintFacts();
    paintCalls();
    paintStop();
  });
}

const open = (c) => c.status === "active" || c.status === "awaiting_call";

/** True when this page was opened from the Today list: going back then is the browser's own back, which keeps the list's place. */
function cameFromToday() {
  try {
    const from = new URL(document.referrer);
    return from.origin === location.origin && from.pathname === "/clinic";
  } catch {
    return false;
  }
}

function paintHead() {
  const c = ui.course;
  // From the list: back is the browser's back, so the list is where it was. Otherwise the link opens Today, which puts the
  // tile and the scroll back from what it remembered for this tab.
  const back = h(
    "a",
    {
      class: "back",
      href: withClinic("/clinic", me.role === "clinic" ? undefined : c.clinic_id),
      onClick: (event) => {
        if (!cameFromToday() || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
        event.preventDefault();
        history.back();
      },
    },
    icon("chevronDown", { className: "back__icon" }),
    t("course.backToday"),
  );
  replace(
    slots.head,
    back,
    h(
      "div",
      { class: "row" },
      h("h1", null, t("course.heading", { no: c.register_no ?? t("clinic.noRegister") })),
      pill({ tone: c.status === "stopped" ? "attention" : c.status === "completed" ? "primary" : "neutral", iconName: { awaiting_call: "hourglass", active: "phone", completed: "check", stopped: "x" }[c.status], text: t(`course.status.${c.status}`) }),
      c.source === "simulator" ? pill({ tone: "simulated", iconName: "phone", text: t("status.source.simulator") }) : null,
    ),
  );
}

// ------------------------------------------------------- dose given today
function nextPending(c) {
  const today = istDate(ui.now);
  return c.doses.find((d) => ["due", "late_minor", "late_review"].includes(d.status) && d.due_date <= today) ?? null;
}

function paintDose() {
  const c = ui.course;
  const d = nextPending(c);
  const title = h("div", { class: "card__head" }, h("h2", { id: "dose-title" }, d ? t("course.dose.title", { dose: doseLabel(d.dose_no) }) : t("course.dose.none")));
  // A closed course says so in the header pill; there is nothing to mark, so no card.
  slots.dose.hidden = !open(c);
  if (!open(c)) return replace(slots.dose);
  if (c.status === "awaiting_call") {
    return replace(slots.dose, h("div", { class: "card__head" }, h("h2", { id: "dose-title" }, t("course.dose.waitingTitle"))), h("p", { class: "muted" }, t("course.dose.waiting")));
  }
  if (!d) {
    const next = c.doses.find((x) => x.status === "due");
    return replace(slots.dose, title, next ? h("p", { class: "muted" }, t("course.dose.next", { dose: doseLabel(next.dose_no), date: formatDate(next.due_date) })) : null);
  }
  // "Due" already says due today; only a late dose adds its date.
  const lateText = d.due_date < istDate(ui.now) ? t("course.dose.wasDue", { date: formatDate(d.due_date) }) : null;
  const when = h("div", { class: "date-form", hidden: true });
  const toggle = h("button", { type: "button", class: "btn", "data-key": "other-day", "aria-expanded": "false", ...writeAttrs(me) });
  toggle.textContent = t("course.dose.otherDay");
  const input = h("input", { class: "input", type: "date", id: "given-on", name: "on", value: istDate(ui.now), min: c.day0, max: istDate(ui.now), "data-key": "given-on" });
  const send = h("button", { type: "button", class: "btn", "data-key": "given-other", ...writeAttrs(me) }, t("course.dose.markOn"));
  send.addEventListener("click", () => mark(send, d, input.value));
  replace(when, h("label", { class: "field__label", for: "given-on" }, t("course.dose.dateLabel")), h("div", { class: "row" }, h("div", { class: "grow" }, input), send));
  toggle.addEventListener("click", () => {
    const show = when.hidden;
    when.hidden = !show;
    toggle.setAttribute("aria-expanded", String(show));
    if (show) input.focus();
  });
  const primary = h("button", { type: "button", class: "btn btn--primary btn--lg btn--block", "data-key": "given-today", ...writeAttrs(me) }, icon("check"), t("course.dose.markToday", { n: d.dose_no }));
  primary.addEventListener("click", () => mark(primary, d, null));
  replace(slots.dose, title, h("div", { class: "row" }, statusPill("dose", d.status), lateText ? h("span", { class: "muted" }, lateText) : null), primary, toggle, when);
}

async function mark(button, dose, on) {
  await runWrite(button, async () => {
    await api.post(`/api/courses/${encodeURIComponent(courseId)}/doses/${dose.dose_no}/given`, on ? { on } : {});
    toast(t("course.dose.marked", { n: dose.dose_no }));
    await load({ quiet: true });
  });
}

// ---------------------------------------------------------------- timeline
function paintTimeline() {
  const c = ui.course;
  replace(
    slots.timeline,
    h("h2", { id: "timeline-title", class: "section-title" }, t("course.timeline")),
    h(
      "ol",
      { class: "timeline" },
      c.doses.map((d) =>
        h(
          "li",
          { class: ["timeline__step", d.status === "given" ? "is-given" : ""], "aria-label": `${doseLabel(d.dose_no)}, ${t("course.day", { n: d.offset_days })}` },
          h(
            "div",
            { class: "timeline__when" },
            h("span", { class: "timeline__no" }, doseLabel(d.dose_no)),
            h("span", { class: "muted" }, `${t("course.day", { n: d.offset_days })} · ${formatDate(d.due_date)}`),
            d.given_on ? h("span", { class: "small muted" }, t("course.givenOn", { date: formatDate(d.given_on), by: t(`course.by.${d.marked_by ?? "clinic"}`) })) : null,
          ),
          statusPill("dose", d.status),
        ),
      ),
    ),
  );
}

// -------------------------------------------------------------- patient facts
function paintFacts() {
  const c = ui.course;
  const rows = [
    [t("course.f.schedule"), regName(c.regimen_id, c.regimen_name)],
    [t("course.f.lang"), langName(c.lang)],
    [t("course.f.age"), c.eligibility === "guardian_consented" ? t("course.age.minor") : c.eligibility === "adult" ? t("course.age.adult") : t("course.age.unconfirmed")],
  ];
  replace(
    slots.facts,
    h("h2", { id: "facts-title", class: "section-title" }, t("course.facts")),
    h("dl", { class: "kv facts" }, rows.map(([k, v]) => [h("dt", null, k), h("dd", null, v)]), h("dt", null, t("course.f.phone")), h("dd", null, phoneCell(c))),
  );
}

function phoneCell(c) {
  if (ui.revealed) {
    return h("div", { class: "phone" }, h("span", { class: "phone__number num", dir: "ltr" }, ui.revealed), h("button", { type: "button", class: "btn btn--quiet", "data-key": "hide-number", onClick: clearReveal }, t("course.phone.hide")), h("p", { class: "small muted phone__note" }, t("course.phone.shown", { s: SHOW_NUMBER_MS / 1000 })));
  }
  if (c.phone_purged) return h("span", null, maskedPhone(null), " ", infoButton({ topic: t("course.phone.topic"), title: t("course.phone.topic"), content: h("p", null, t("course.phone.purged")) }));
  if (!c.phone_last4) return h("span", { class: "muted" }, t("course.phone.none"));
  return h(
    "div",
    { class: "phone" },
    h("span", { class: "num" }, maskedPhone(c.phone_last4)),
    h("button", { type: "button", class: "btn", "data-key": "reveal", ...writeAttrs(me), onClick: (event) => reveal(event.currentTarget) }, icon("phone"), t("course.phone.show")),
    h("p", { class: "small muted phone__note" }, t("course.phone.logged")),
  );
}

async function reveal(button) {
  await runWrite(button, async () => {
    const { phone } = await api.post(`/api/courses/${encodeURIComponent(courseId)}/reveal-phone`);
    ui.revealed = phone;
    clearTimeout(ui.timer);
    ui.timer = setTimeout(clearReveal, SHOW_NUMBER_MS);
    paintFacts();
    document.querySelector('[data-key="hide-number"]')?.focus();
  });
}

function clearReveal() {
  clearTimeout(ui.timer);
  ui.revealed = null;
  if (ui.built) keepFocus(host, paintFacts);
}

// ----------------------------------------------------------------- call log
function paintCalls() {
  const c = ui.course;
  const calls = [...c.calls].sort((a, b) => (a.scheduled_for < b.scheduled_for ? 1 : a.scheduled_for > b.scheduled_for ? -1 : b.attempt - a.attempt));
  const head = h("div", { class: "list-head" }, h("h2", { id: "calls-title", class: "section-title" }, t("course.calls.title")), infoButton({ topic: t("course.calls.topic"), title: t("course.calls.title"), content: h("p", null, t("course.calls.explain")) }));
  const body = calls.length
    ? h("ul", { class: "list calls" }, calls.map(callRow))
    : emptyState({ title: t("states.noCalls"), body: t("states.lastChecked", { when: formatTime(ui.now) }) });
  replace(slots.calls, head, body);
}

function callRow(k) {
  const kind = k.dose_no ? t("course.calls.kindDose", { kind: t(`course.kind.${k.kind}`), dose: doseLabel(k.dose_no) }) : t(`course.kind.${k.kind}`);
  const heard = k.seconds_listened == null ? null : t("course.calls.listened", { s: Math.round(k.seconds_listened) });
  return h(
    "li",
    { class: "list__row calls__row" },
    h(
      "div",
      { class: "list__main" },
      h("span", { class: "list__title num" }, formatDateTime(k.scheduled_for)),
      h("span", { class: "list__meta" }, kind),
      h("span", { class: "list__meta" }, [t("course.calls.try", { n: k.attempt }), heard].filter(Boolean).join(" · ")),
      k.error ? h("p", { class: "call__error" }, icon("alert"), h("span", null, bdi(k.error))) : null,
    ),
    h("span", { class: "list__side" }, statusPill("call", k.status)),
  );
}

// --------------------------------------------------------------- stop reminders
function paintStop() {
  const c = ui.course;
  if (!open(c)) return replace(slots.stop);
  const button = h("button", { type: "button", class: "btn", "data-key": "stop", ...writeAttrs(me), onClick: () => stopDialog(button) }, icon("x"), t("course.stop.button"));
  replace(slots.stop, h("div", { class: "row" }, button, infoButton({ topic: t("course.stop.topic"), title: t("course.stop.button"), content: h("p", null, t("course.stop.explain")) })));
}

async function stopDialog(trigger) {
  let choice = null;
  const other = textInput({ name: "other", maxlength: 150, autocomplete: "off" });
  other.id = "stop-other";
  const otherWrap = h("div", { class: "field", hidden: true }, h("label", { class: "field__label", for: "stop-other" }, t("course.stop.otherLabel")), other);
  const errors = h("div", { role: "status" });
  const radios = [...REASONS.map(([key, text]) => [key, text, t(`course.stop.reason.${key}`)]), ["other", null, t("course.stop.reason.other")]].map(([key, text, label]) => {
    const input = h("input", { type: "radio", name: "reason", value: key });
    input.addEventListener("change", () => {
      choice = key;
      otherWrap.hidden = key !== "other";
      if (key === "other") other.focus();
      replace(errors);
    });
    return h("label", { class: "choice" }, input, h("span", { class: "choice__text" }, h("span", { class: "choice__label" }, label)));
  });
  const body = h("div", { class: "stack" }, h("p", null, t("course.stop.ask")), h("div", { class: "choices", role: "radiogroup", "aria-label": t("course.stop.reasonLabel") }, radios), otherWrap, errors);
  await formDialog({
    title: t("course.stop.title"),
    body,
    confirmLabel: t("course.stop.confirm"),
    onConfirm: async (button) => {
      if (!choice) return replace(errors, inlineError(t("course.stop.needReason"))), false;
      let reason = REASONS.find(([k]) => k === choice)?.[1];
      if (choice === "other") {
        const said = other.value.trim();
        if (!said) return replace(errors, inlineError(t("course.stop.needOther"))), false;
        reason = `Other: ${said}`;
      }
      const done = await runWrite(
        button,
        async () => {
          await api.post(`/api/courses/${encodeURIComponent(courseId)}/stop`, { reason }, { quiet: true });
          return true;
        },
        { onError: (err) => replace(errors, inlineError(err.message)) },
      );
      if (done) {
        toast(t("course.stop.done"));
        await load({ quiet: true });
      }
      return Boolean(done);
    },
  });
  if (trigger.isConnected) trigger.focus();
}
