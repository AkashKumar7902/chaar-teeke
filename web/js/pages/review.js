// R1 Review queue. One job: place the recordings the matcher was not sure about, one case at a time.
// The reviewer hears the clip (Space plays), reads the words, and picks one of up to three candidate wards
// (the words "likely" and "possible", never scores), "Outside Jammu city (pick tehsil)" or "Can't tell".
// Keys 1 to 5 choose, J and K move. A choice waits 5 s with an Undo before it is sent. The server deletes the clip.

import { ApiError, api } from "/js/core/api.js";
import { bdi, h, nextFrame } from "/js/core/dom.js";
import { formatDate, formatDateTime, formatTime } from "/js/core/format.js";
import { t } from "/js/core/i18n.js";
import { sharedStream } from "/js/core/sse.js";
import { readPref, writePref } from "/js/core/storage.js";
import { checkbox, emptyState, errorFor, errorState, field, icon, infoButton, pill, selectInput, skeleton, textInput, toast } from "/js/ui/index.js";
import { boot, serverNow } from "./oversight/lib.js";
import { player } from "./review/player.js";
import { loadWardList } from "./review/wards.js";

const me = await boot(["review"], "review.title");

const UNDO_MS = 5000;
const KEYS_PREF = "ct.review.keys";

const S = {
  phase: "loading", // loading | empty | case | taken | error
  item: null,
  k: 0, // which waiting case is open (0 = the oldest)
  remaining: null,
  checkedAt: null,
  error: null,
  panel: null, // null | { kind: "outside" | "ward", value, error }
  pending: null, // { decision, deadline, timer, tick, busy, failure }
  locked: false, // no recording: only Can't tell
  note: null,
  remember: false,
  wards: null, // null | "loading" | "failed" | [{ward_no, ward_name}]
  keys: readPref(KEYS_PREF) !== "off",
  offline: false,
};
let pl = null; // the player of the open case
let ticket = 0;
let focusCase = false;
let caseEl = null;

// ----------------------------------------------------------------------------------------- the frame of the page
const main = document.getElementById("main");
const heading = h("h1", null, t("review.title"));
const badge = h("span", { class: "pill rv-left", "aria-live": "polite" });
const keysSlot = h("span", { class: "rv-keys-slot" });
const stage = h("div", { class: "rv-stage" });
main.append(h("div", { class: "page-title" }, heading, badge, keysSlot), stage);

function buildKeys() {
  const use = checkbox({ name: "keys", label: t("review.keys.use"), checked: S.keys });
  use.querySelector("input").addEventListener("change", (event) => {
    S.keys = event.target.checked;
    writePref(KEYS_PREF, S.keys ? "on" : "off");
  });
  const list = ["space", "ward", "outside", "cant", "other", "move", "undo"].map((k) => h("li", null, t(`review.keys.${k}`)));
  keysSlot.replaceChildren(infoButton({ topic: t("review.keys.topic"), title: t("buttons.about", { topic: t("review.keys.topic") }), content: h("div", { class: "stack stack--s" }, h("ul", { class: "rv-keylist" }, list), use) }));
}

// ------------------------------------------------------------------------------------------------ loading cases
function setItem(item, k) {
  if (!S.item || S.item.report_id !== item.report_id) {
    pl?.destroy();
    pl = item.audio_available ? player({ reportId: item.report_id, onMissing: lockNoRecording }) : null;
  }
  S.item = item;
  S.k = k;
  S.remaining = item.remaining;
  S.locked = !item.audio_available;
  S.panel = null;
  S.pending = null;
  S.remember = false;
  S.note = null;
  S.phase = "case";
  focusCase = true;
}

function lockNoRecording() {
  S.locked = true;
  S.panel = null;
  pl?.destroy();
  pl = null;
  paint();
}

/** show: replace the open case with the one the server sends; otherwise only keep the count and the empty state honest. */
async function loadCase({ k = 0, show = true } = {}) {
  const mine = (ticket += 1);
  if (show) {
    S.phase = "loading";
    paint();
  }
  try {
    const item = await api.get("/api/review/next", k > 0 ? { skip: k } : undefined);
    if (mine !== ticket) return;
    if (item === null) {
      if (S.pending) return;
      // The queue got shorter while the reviewer was on a later case: start again from the oldest.
      if (k > 0) return loadCase({ k: 0, show });
      pl?.destroy();
      pl = null;
      S.item = null;
      S.remaining = 0;
      S.phase = "empty";
      S.checkedAt = await serverNow(me);
    } else if (!show) {
      S.remaining = item.remaining;
    } else {
      setItem(item, k);
    }
  } catch (err) {
    if (mine !== ticket) return;
    if (!(err instanceof ApiError)) console.error(err);
    if (show || S.phase === "empty") {
      S.phase = "error";
      S.error = err;
    }
  }
  paint();
}

// --------------------------------------------------------------------------------------------------- decisions
const wardText = (no, name) => (name ? t("review.placed.ward", { no, name }) : t("review.placed.wardNameless", { no }));

function decisionText(d) {
  if (d.kind === "ward") return wardText(d.ward_no, d.ward_name);
  if (d.kind === "outside") return t("review.placed.outside", { tehsil: d.tehsil });
  return t("review.placed.cant");
}

function choose(decision) {
  if (S.phase !== "case" || S.pending) return;
  if (!me.can_write) {
    toast(t("review.readOnly"), { tone: "attention" });
    return;
  }
  if (S.locked && decision.kind !== "cant") return;
  S.note = null;
  S.panel = null;
  S.pending = { decision, deadline: Date.now() + UNDO_MS, timer: setTimeout(send, UNDO_MS), tick: setInterval(tickCountdown, 250), busy: false, failure: null };
  window.addEventListener("beforeunload", leaveGuard);
  paint();
  nextFrame(() => stage.querySelector(".rv-undo button")?.focus({ preventScroll: true }));
}

function leaveGuard(event) {
  if (S.pending) event.preventDefault();
}

function stopTimers() {
  const p = S.pending;
  if (!p) return;
  clearTimeout(p.timer);
  clearInterval(p.tick);
}

function undo() {
  if (!S.pending || S.pending.busy) return;
  stopTimers();
  S.pending = null;
  window.removeEventListener("beforeunload", leaveGuard);
  focusCase = true;
  paint();
}

function tickCountdown() {
  const el = stage.querySelector(".rv-undo__count");
  if (el && S.pending) el.textContent = t("review.sending", { s: Math.max(0, Math.ceil((S.pending.deadline - Date.now()) / 1000)) });
}

function bodyOf(d) {
  if (d.kind === "ward") return { ward_no: d.ward_no, remember_name: d.remember };
  if (d.kind === "outside") return { outside_tehsil: d.tehsil };
  return { cant_tell: true };
}

async function send() {
  const p = S.pending;
  if (!p || p.busy) return;
  stopTimers();
  p.busy = true;
  p.failure = null;
  paint();
  try {
    const result = await api.post(`/api/review/${encodeURIComponent(S.item.report_id)}/decide`, bodyOf(p.decision));
    S.pending = null;
    window.removeEventListener("beforeunload", leaveGuard);
    toast(t("review.saved"));
    S.remaining = result.remaining;
    S.item = null; // the next case is a different one
    await loadCase({ k: 0 });
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    if (err.code === "already_decided" || err.code === "not_in_queue") {
      S.pending = null;
      window.removeEventListener("beforeunload", leaveGuard);
      pl?.destroy();
      pl = null;
      S.phase = "taken";
    } else if (err.isReadOnly) {
      S.pending = null;
      window.removeEventListener("beforeunload", leaveGuard);
      focusCase = true;
    } else if (err.code === "bad_decision" && p.decision.kind === "outside") {
      S.pending = null;
      window.removeEventListener("beforeunload", leaveGuard);
      S.panel = { kind: "outside", value: p.decision.tehsil, error: err.message };
    } else {
      p.busy = false;
      p.failure = err.isNetwork ? t("review.notSent") : err.message;
    }
    paint();
  }
}

// -------------------------------------------------------------------------------------------------- the pieces
function choiceButton({ key, title, meta = null, onClick, disabled = false }) {
  const readOnly = !me.can_write;
  const button = h(
    "button",
    { type: "button", class: "rv-choice", disabled: disabled || null, "aria-disabled": readOnly && !disabled ? "true" : null, "aria-keyshortcuts": key, dataset: { key }, onClick },
    h("span", { class: "rv-choice__key", "aria-hidden": "true" }, key),
    h("span", { class: "rv-choice__body" }, h("span", { class: "rv-choice__title" }, title), meta),
  );
  return button;
}

function candidateButtons(item) {
  if (item.candidates.length === 0) return [h("p", { class: "muted rv-none" }, icon("info"), t("review.candidates.none"))];
  return item.candidates.slice(0, 3).map((c, i) =>
    choiceButton({
      key: String(i + 1),
      title: wardText(c.ward_no, c.ward_name),
      meta: h("span", { class: "rv-choice__meta" }, pill({ tone: "estimate", iconName: "map", text: t(`status.confidence.${c.label}`) }), h("span", { class: "small muted" }, t("review.candidates.matched"), " ", bdi(c.evidence))),
      disabled: S.locked,
      onClick: () => choose({ kind: "ward", ward_no: c.ward_no, ward_name: c.ward_name, remember: S.remember }),
    }),
  );
}

function openPanel(kind) {
  if (S.phase !== "case" || S.pending || S.locked) return;
  if (!me.can_write) {
    toast(t("review.readOnly"), { tone: "attention" });
    return;
  }
  S.panel = { kind, value: "", error: null };
  if (kind === "ward" && !Array.isArray(S.wards)) {
    S.wards = "loading";
    loadWardList().then(
      (list) => {
        S.wards = list;
        paint();
      },
      (err) => {
        console.error(err);
        S.wards = "failed";
        paint();
      },
    );
  }
  paint();
  nextFrame(() => stage.querySelector(".rv-panel input, .rv-panel select")?.focus({ preventScroll: true }));
}

function closePanel() {
  S.panel = null;
  focusCase = true;
  paint();
}

function panelView() {
  const p = S.panel;
  const submit = () => {
    if (p.kind === "outside") {
      const tehsil = p.value.trim();
      if (!tehsil) {
        p.error = t("review.tehsil.required");
        paint();
        nextFrame(() => stage.querySelector(".rv-panel input")?.focus({ preventScroll: true }));
        return;
      }
      choose({ kind: "outside", tehsil });
    } else {
      const no = Number(p.value);
      const ward = Array.isArray(S.wards) ? S.wards.find((w) => w.ward_no === no) : null;
      if (!ward) {
        p.error = t("review.pick.required");
        paint();
        return;
      }
      choose({ kind: "ward", ward_no: ward.ward_no, ward_name: ward.ward_name, remember: S.remember });
    }
  };
  let control;
  let label;
  let hint = null;
  if (p.kind === "outside") {
    label = t("review.tehsil.label");
    hint = t("review.tehsil.hint");
    control = textInput({ name: "tehsil", maxlength: 80, value: p.value, autocomplete: "off" });
    control.addEventListener("input", () => (p.value = control.value));
  } else {
    label = t("review.pick.label");
    const options = [{ value: "", label: Array.isArray(S.wards) ? t("review.pick.choose") : S.wards === "failed" ? t("review.pick.failed") : t("review.pick.loading") }];
    if (Array.isArray(S.wards)) for (const w of S.wards) options.push({ value: String(w.ward_no), label: `${w.ward_no} ${w.ward_name ?? ""}`.trim() });
    control = selectInput({ name: "ward", options, value: p.value });
    control.addEventListener("change", () => (p.value = control.value));
  }
  const f = field({ label, hint, control, error: p.error });
  const form = h(
    "form",
    { class: "rv-panel stack stack--s", novalidate: true, onSubmit: (event) => (event.preventDefault(), submit()) },
    f.el,
    h("div", { class: "row" }, h("button", { type: "submit", class: "btn btn--primary" }, t("review.use")), h("button", { type: "button", class: "btn", onClick: closePanel }, t("review.dismiss"))),
  );
  form.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      closePanel();
    }
  });
  return form;
}

function rememberTick() {
  // nothing to remember without words, so there is no tick to offer
  if (!(S.item.transcript && S.item.transcript.trim())) return null;
  const off = S.locked || !me.can_write;
  const tick = checkbox({ name: "remember", label: t("review.remember"), checked: S.remember && !off });
  const input = tick.querySelector("input");
  input.disabled = off;
  input.addEventListener("change", () => (S.remember = input.checked));
  return h("div", { class: "rv-remember" }, tick);
}

function choicesView() {
  const item = S.item;
  if (S.panel) {
    // a sub-step replaces the list, so nothing jumps below the fold; Cancel goes back
    return h(
      "div",
      { class: "stack rv-decide" },
      h("h3", { class: "rv-sub" }, t(S.panel.kind === "outside" ? "review.outside" : "review.other")),
      panelView(),
    );
  }
  const cant = choiceButton({ key: "5", title: t("review.cant"), onClick: () => choose({ kind: "cant" }) });
  return h(
    "div",
    { class: "stack rv-decide" },
    h("h3", { class: "rv-sub" }, t("review.candidates.title")),
    h("div", { class: "rv-choices" }, candidateButtons(item), choiceButton({ key: "4", title: t("review.outside"), onClick: () => openPanel("outside"), disabled: S.locked }), cant),
    h("button", { type: "button", class: "btn btn--quiet rv-other", "aria-keyshortcuts": "6", disabled: S.locked || null, "aria-disabled": me.can_write || S.locked ? null : "true", onClick: () => openPanel("ward") }, icon("map"), t("review.other"), h("span", { class: "rv-choice__key rv-choice__key--quiet", "aria-hidden": "true" }, "6")),
  );
}

function undoView() {
  const p = S.pending;
  const secs = Math.max(0, Math.ceil((p.deadline - Date.now()) / 1000));
  return h(
    "div",
    { class: ["rv-undo", p.failure ? "rv-undo--fail" : ""], role: "status" },
    h("p", { class: "rv-undo__what" }, icon(p.failure ? "alert" : "check"), decisionText(p.decision)),
    p.failure
      ? h("p", { class: "rv-undo__time" }, p.failure)
      : [h("span", { class: "visually-hidden" }, t("review.sendingSr")), h("p", { class: "rv-undo__time rv-undo__count", "aria-hidden": "true" }, p.busy ? "" : t("review.sending", { s: secs }))],
    h(
      "div",
      { class: "row" },
      h("button", { type: "button", class: "btn", disabled: p.busy || null, onClick: undo }, t("review.undo")),
      p.failure ? h("button", { type: "button", class: "btn btn--primary", onClick: send }, icon("refresh"), t("review.retrySend")) : h("button", { type: "button", class: "btn", disabled: p.busy || null, onClick: send }, t("review.sendNow")),
    ),
  );
}

function transcriptView(item) {
  const words = item.transcript && item.transcript.trim();
  let body;
  if (words) body = h("blockquote", { class: "rv-words" }, bdi(item.transcript));
  else if (item.asr_status === "off") body = h("p", { class: "rv-words rv-words--none" }, icon("info"), t("review.transcript.off"));
  else if (item.asr_status === "pending") body = h("p", { class: "rv-words rv-words--none" }, icon("hourglass"), t("review.transcript.pending"));
  else if (item.asr_status === "done") body = h("p", { class: "rv-words rv-words--none" }, icon("info"), t("review.transcript.empty"));
  else
    body = h(
      "div",
      { class: "stack stack--s" },
      h("p", { class: "rv-words rv-words--none" }, icon("alert"), t("review.transcript.failed")),
      item.asr_error ? h("p", { class: "small muted" }, t("review.transcript.reason", { why: item.asr_error })) : null,
    );
  return h("section", { class: "stack stack--s", "aria-labelledby": "rv-tr-h" }, h("h3", { class: "rv-sub", id: "rv-tr-h" }, t("review.transcript.title")), body);
}

function listenView(item) {
  return h(
    "div",
    { class: "stack rv-listen" },
    S.locked
      ? h("div", { class: "rv-nolisten", role: "status" }, icon("info"), h("div", null, h("p", { class: "rv-nolisten__t" }, t("review.noAudio")), h("p", { class: "small muted" }, t("review.noAudioHint"))))
      : pl.el,
    transcriptView(item),
    // the tick sits with the words it is about, before any ward is chosen
    S.pending ? null : rememberTick(),
    h("p", { class: "small muted" }, t("review.deleted", { date: formatDate(item.expires_at) })),
  );
}

function caseView() {
  const item = S.item;
  caseEl = h(
    "section",
    { class: "card rv-case", tabindex: "-1", "aria-labelledby": "rv-case-h" },
    h("div", { class: "rv-case__head" }, h("h2", { class: "section-title", id: "rv-case-h" }, t("review.case")), h("span", { class: "small muted" }, t("review.heard", { when: formatDateTime(item.heard_at) }))),
    S.note ? h("p", { class: "rv-note", role: "status" }, icon("info"), S.note) : null,
    h("div", { class: "rv-grid" }, listenView(item), S.pending ? undoView() : choicesView()),
  );
  return caseEl;
}

// ----------------------------------------------------------------------------------------------------- paint
function paint() {
  const left = S.remaining;
  const showBadge = (S.phase === "case" || S.phase === "taken") && typeof left === "number";
  badge.hidden = !showBadge;
  badge.textContent = showBadge ? t("review.left", { count: left }) : "";
  caseEl = null;
  let view;
  if (S.phase === "loading") view = skeleton({ lines: 5, block: true });
  else if (S.phase === "error") view = S.offline ? errorState({ message: t("states.offline"), onRetry: () => loadCase() }) : errorFor(S.error, { onRetry: () => loadCase() });
  else if (S.phase === "empty")
    view = emptyState({
      title: t("review.empty.title"),
      body: S.checkedAt ? t("states.lastChecked", { when: formatTime(S.checkedAt) }) : null,
      action: h("button", { type: "button", class: "btn", onClick: () => loadCase() }, icon("refresh"), t("review.empty.again")),
    });
  else if (S.phase === "taken")
    view = emptyState({
      title: t("review.taken.title"),
      body: t("review.taken.body"),
      tone: "attention",
      iconName: "alert",
      action: h("button", { type: "button", class: "btn btn--primary", onClick: () => ((S.item = null), loadCase()) }, t("review.taken.next")),
    });
  else view = caseView();
  stage.replaceChildren(view);
  if (focusCase && caseEl) {
    focusCase = false;
    nextFrame(() => caseEl?.focus({ preventScroll: true }));
  }
}

// ---------------------------------------------------------------------------------------------------- the keys
function move(delta) {
  if (S.phase !== "case" || S.pending) return;
  if (!(S.remaining > 1)) {
    S.note = t("review.only");
    paint();
    return;
  }
  const k = Math.min(Math.max(S.k + delta, 0), S.remaining - 1);
  if (k === S.k) return;
  loadCase({ k });
}

function isTyping(target) {
  return target instanceof Element && (target.closest("input, textarea, select, [contenteditable='true']") !== null);
}

document.addEventListener("keydown", (event) => {
  if (!S.keys || event.defaultPrevented || event.altKey || event.metaKey || event.ctrlKey || event.isComposing) return;
  if (isTyping(event.target)) return;
  if (S.phase !== "case") return;
  const active = document.activeElement;
  const idle = active === document.body || active === caseEl || active === document.documentElement;
  const key = event.key;
  if (S.pending) {
    if (key === "Escape" || key === "Backspace") {
      event.preventDefault();
      undo();
    } else if (key === "Enter" && idle) {
      event.preventDefault();
      send();
    }
    return;
  }
  if (key === " ") {
    if (active?.classList.contains("rv-play")) return; // the button's own Space
    if (idle && pl) {
      event.preventDefault();
      pl.toggle();
    }
    return;
  }
  const lower = key.toLowerCase();
  if (lower === "j") move(1);
  else if (lower === "k") move(-1);
  else if (/^[1-3]$/.test(key)) stage.querySelector(`.rv-choice[data-key="${key}"]`)?.click();
  else if (key === "4") stage.querySelector('.rv-choice[data-key="4"]')?.click();
  else if (key === "5") stage.querySelector('.rv-choice[data-key="5"]')?.click();
  else if (key === "6") stage.querySelector(".rv-other")?.click();
  else return;
  if (/^[1-6jk]$/.test(lower)) event.preventDefault();
});

// ------------------------------------------------------------------------------------------- live and offline
const stream = sharedStream();
stream.on("review", (event) => {
  if (typeof event.data?.remaining !== "number" || S.pending) return;
  if (S.phase === "empty" && event.data.remaining > 0) loadCase();
  else if (S.phase === "case" || S.phase === "taken") {
    S.remaining = event.data.remaining;
    badge.textContent = t("review.left", { count: S.remaining });
  }
});
stream.onResync(() => {
  if (S.phase !== "loading" && !S.pending) loadCase({ show: S.phase === "empty" || S.phase === "error", k: S.phase === "case" ? S.k : 0 });
});
document.addEventListener("ct:offline", (event) => {
  S.offline = Boolean(event.detail?.offline);
  if (S.pending) return;
  if (S.offline) {
    ticket += 1;
    pl?.destroy();
    pl = null;
    S.item = null;
    S.phase = "error";
    S.error = null;
    paint();
  } else loadCase();
});
document.addEventListener("ct:langchange", () => {
  heading.textContent = t("review.title");
  buildKeys();
  pl?.refresh();
  if (S.note) S.note = null;
  paint();
});

buildKeys();
loadCase();
