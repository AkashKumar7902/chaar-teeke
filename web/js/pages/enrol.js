// C2 Enrol. One job: add a patient with as few taps as possible. It has not been timed with clinic staff yet.
// Three screens: (1) the register number and whether the patient is an adult or a minor with a guardian,
// (2) a check of what the system will use, each default with "Change", (3) done. The server decides what is
// allowed (schedule switched on, first dose not in the future, eligibility); this page only asks and shows its answer.

import { api } from "/js/core/api.js";
import { append, bdi, h, replace } from "/js/core/dom.js";
import { formatDate, istDate } from "/js/core/format.js";
import { mountFrame } from "/js/core/frame.js";
import { AUTONYMS, initI18n, t } from "/js/core/i18n.js";
import { sharedStream } from "/js/core/sse.js";
import { banner, errorSummary, icon, infoButton, pill, statusPill, textInput } from "/js/ui/index.js";
import { clinicParam, daysLine, langName, liveRegion, pageTitle, regName, loadConfig, runWrite, serverNow, withClinic, writeAttrs } from "./clinic/common.js";

await initI18n({ bundles: ["common", "clinic", "enrol"], audience: "staff" });
const { me } = await mountFrame({ mode: "staff" });
document.title = pageTitle("enrol.title");

const STEPS = ["first", "check"]; // the third screen, "done", replaces the form
const SCREENS = STEPS.length + 1;
const host = h("div", { class: "stack stack--l" });
document.getElementById("main").append(host);

// What the person has typed or chosen. It survives a language change and a failed send.
const values = { register_no: "", patient_type: "new", regimen_id: null, lang: null, day0: null, eligibility: null };
const ui = { step: 0, cfg: null, today: null, errors: {}, created: null };

const live = liveRegion(host, {
  load: async () => {
    const clinicId = clinicParam();
    const [cfg, now] = await Promise.all([loadConfig(clinicId), serverNow(me)]);
    return { cfg, today: istDate(now) };
  },
  build: ({ cfg, today }) => {
    ui.cfg = cfg;
    ui.today = today;
    values.regimen_id ??= cfg.default_regimen;
    values.lang ??= cfg.default_lang;
    values.day0 ??= today;
    return ui.created ? doneView() : formView();
  },
  update: () => {},
});
document.addEventListener("ct:langchange", () => {
  document.title = pageTitle("enrol.title");
  live.drop();
  live.reload({ quiet: true });
});
live.reload();

// ------------------------------------------------------------------ form
let el = {};

function formView() {
  const cfg = ui.cfg;
  el = {};
  const regimens = cfg.regimens.filter((r) => r.enabled);

  // Screen 1: the two things only the person at the desk knows.
  // The register number is digits at this clinic's desk, so the phone offers the number pad.
  const register = textInput({ name: "register_no", value: values.register_no, maxlength: 20, autocomplete: "off", inputmode: "numeric", required: true });
  register.autofocus = true;
  register.addEventListener("input", () => (values.register_no = register.value));
  el.register = question("register", t("enrol.q.register"), register);
  el.age = question(
    "age",
    t("enrol.q.age"),
    choices("eligibility", [
      { value: "adult", label: t("enrol.age.adult") },
      { value: "guardian_consented", label: t("enrol.age.minor") },
    ]),
    { info: t("enrol.info.age") },
  );
  el.first = h("div", { class: "stack stack--l", "data-step": "first" }, el.register.el, el.age.el);

  // Screen 2: what the system will use. Each default can be changed here, in place.
  const date = h("input", { id: "day0", class: "input", type: "date", name: "day0", value: values.day0, max: ui.today, required: true, "aria-label": t("enrol.q.date") });
  date.addEventListener("input", () => {
    values.day0 = date.value;
    clearError("day0");
  });
  const done = (key) => () => toggleEdit(key, false);
  el.edit = {
    type: choices("patient_type", [
      { value: "new", label: t("enrol.type.new") },
      { value: "old", label: t("enrol.type.old") },
    ], done("type")).el,
    regimen: choices(
      "regimen_id",
      regimens.map((r) => ({
        value: r.id,
        label: regName(r.id, r.name),
        hint: [daysLine(r.days), r.id === cfg.default_regimen ? t("enrol.clinicDefault") : null].filter(Boolean).join(" · "),
      })),
      done("regimen"),
    ).el,
    lang: choices(
      "lang",
      cfg.langs.map((code) => ({ value: code, label: langName(code), aux: AUTONYMS[code], auxLang: code, hint: code === cfg.default_lang ? t("enrol.clinicDefault") : null })),
      done("lang"),
    ).el,
    date: h("div", { class: "stack stack--s" }, h("p", { class: "field__hint" }, t("enrol.hint.date")), date, h("div", { id: "q-date-err" })),
  };
  date.addEventListener("change", done("date"));
  date.setAttribute("aria-describedby", "q-date-err");
  el.rows = {};
  const rows = [
    ["register", t("enrol.q.register"), () => bdi(values.register_no.trim() || "-"), () => backToFirst("register")],
    ["age", t("enrol.q.age"), () => (values.eligibility ? t(values.eligibility === "adult" ? "enrol.age.adult" : "enrol.age.minor") : "-"), () => backToFirst("age")],
    ["type", t("enrol.q.type"), () => t(`enrol.type.${values.patient_type}`)],
    ["regimen", t("enrol.q.regimen"), () => regimenText()],
    ["lang", t("enrol.q.lang"), () => (values.lang ? langName(values.lang) : "-")],
    ["date", t("enrol.q.date"), () => dateText()],
  ];
  el.list = h("ul", { class: "checks" }, rows.map(([key, label, text, change]) => checkRow(key, label, text, change)));
  el.problems = h("div", { class: "enrol__problems stack stack--s" });
  el.progress = h("div", { class: "progress" }, (el.progressText = h("p", { class: "progress__text" })), h("div", { class: "progress__track", "aria-hidden": "true" }, (el.progressBar = h("div", { class: "progress__bar" }))));
  el.check = h("section", { class: "step step--check stack", "data-step": "check", "aria-labelledby": "check-title" }, h("h2", { id: "check-title", tabindex: "-1" }, t("enrol.q.check")), el.list);
  el.back = h("a", { class: "btn", href: withClinic("/clinic", clinicParam()), onClick: (event) => goBack(event) }, t("buttons.back"));
  el.next = h("button", { type: "submit", class: "btn btn--primary", ...writeAttrs(me) });
  el.nav = h("div", { class: "enrol__nav" }, el.back, el.next);

  const form = h("form", { class: "stack stack--l", novalidate: true, onSubmit: (event) => (event.preventDefault(), advance()) }, el.problems, el.progress, el.first, el.check, el.nav);
  const head = h("div", { class: "stack stack--s" }, h("h1", { id: "enrol-title", tabindex: "-1" }, t("enrol.title")), h("p", { class: "muted" }, bdi(cfg.clinic_name)));
  ui.step = Math.min(ui.step, STEPS.length - 1);
  queueMicrotask(() => {
    showStep();
    if (ui.step === 0 && !values.register_no) register.focus();
  });
  return h("div", { class: "stack stack--l" }, head, form);
}

const regimenText = () => {
  const reg = ui.cfg.regimens.find((r) => r.id === values.regimen_id);
  return reg ? `${regName(reg.id, reg.name)}, ${daysLine(reg.days).toLowerCase()}` : "-";
};
const dateText = () => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(values.day0 ?? "")) return "-";
  return values.day0 === ui.today ? t("enrol.dateToday", { date: formatDate(values.day0) }) : formatDate(values.day0);
};

/** One line of the check: what will be used, and "Change" to open its control right there. */
function checkRow(key, label, text, change) {
  const value = h("span", { class: "check__v" });
  const editor = el.edit[key] ? h("div", { class: "check__edit", hidden: true }, el.edit[key]) : null;
  const button = h("button", { type: "button", class: "btn btn--quiet", "data-key": `change-${key}`, "aria-expanded": editor ? "false" : null, "aria-label": `${t("enrol.change")}: ${label}`, onClick: () => (editor ? toggleEdit(key) : change()) }, t("enrol.change"));
  const li = h("li", { class: "check__row", "data-row": key }, h("div", { class: "check__line" }, h("span", { class: "check__k" }, label), value, button), editor);
  el.rows[key] = { li, value, text, editor, button };
  return li;
}

function toggleEdit(key, open) {
  const row = el.rows[key];
  if (!row?.editor) return;
  const show = open ?? row.editor.hidden;
  row.editor.hidden = !show;
  row.button.setAttribute("aria-expanded", String(show));
  if (show) (row.editor.querySelector("input:checked") ?? row.editor.querySelector("input"))?.focus();
  else row.button.focus();
}

/** "Change" on the register number or the age: back to the first screen, on that question. */
function backToFirst(key) {
  ui.step = 0;
  showStep();
  el[key].focus();
}

/** A question: a fieldset with its legend, an optional (i), a hint and an inline error. `control` is an input or choices(). */
function question(key, legend, control, { info = null, hint = null } = {}) {
  const wrap = h("fieldset", { class: "step stack stack--s", "data-step": key });
  const input = control instanceof HTMLElement ? control : control.el.querySelector("input");
  input.id = `q-${key}`;
  const single = control instanceof HTMLElement;
  // A single input is named by a real <label>; a group of radios by the fieldset's <legend>.
  const title = single ? h("label", { class: "step__legend", for: input.id }, legend) : h("legend", { class: "step__legend", tabindex: "-1" }, legend);
  const err = h("div", { id: `q-${key}-err` });
  input.setAttribute("aria-describedby", `q-${key}-err`);
  append(
    wrap,
    h("div", { class: "step__head" }, title, info ? infoButton({ topic: legend, title: legend, content: h("p", null, info) }) : null),
    hint ? h("p", { class: "field__hint" }, hint) : null,
    single ? control : control.el,
    err,
  );
  return {
    el: wrap,
    focus: () => (single ? input : title).focus(),
    setError(message) {
      replace(err, message ? h("p", { class: "field__error" }, icon("alert"), message) : null);
      wrap.classList.toggle("field--error", Boolean(message));
      if (single) message ? input.setAttribute("aria-invalid", "true") : input.removeAttribute("aria-invalid");
    },
  };
}

/** Big radio rows. The chosen value is kept in `values[name]`; `onPick` runs after a choice. */
function choices(name, options, onPick = null) {
  const inputs = options.map((o) => {
    const input = h("input", { type: "radio", name, value: o.value, checked: values[name] === o.value ? true : null });
    input.addEventListener("change", () => {
      values[name] = o.value;
      clearError(name);
      paintSummary();
      onPick?.();
    });
    return h(
      "label",
      { class: "choice" },
      input,
      h("span", { class: "choice__text" }, h("span", { class: "choice__label" }, o.label), o.hint ? h("span", { class: "choice__hint" }, o.hint) : null),
      o.aux ? h("span", { class: "choice__aux", lang: o.auxLang }, o.aux) : null,
    );
  });
  return { el: h("div", { class: "choices", role: "radiogroup" }, inputs) };
}

const QUESTION_OF = { register_no: "register", eligibility: "age" };
function clearError(name) {
  if (name === "day0") {
    delete ui.errors.date;
    replace(document.getElementById("q-date-err"));
    return;
  }
  const key = QUESTION_OF[name];
  if (key && ui.errors[key]) {
    delete ui.errors[key];
    el[key]?.setError(null);
  }
}

// ------------------------------------------------------------- validation
function check(key) {
  switch (key) {
    case "register":
      if (!values.register_no.trim()) return t("enrol.err.register");
      if (values.register_no.trim().length > 20) return t("enrol.err.registerLong");
      return null;
    case "date":
      if (!/^\d{4}-\d{2}-\d{2}$/.test(values.day0 ?? "") || values.day0 > ui.today) return t("enrol.err.date");
      return null;
    case "age":
      return values.eligibility ? null : t("enrol.err.age");
    default:
      return null;
  }
}

function validate(keys) {
  const found = [];
  for (const key of keys) {
    const message = check(key);
    if (key === "date") replace(document.getElementById("q-date-err"), message ? h("p", { class: "field__error" }, icon("alert"), message) : null);
    else el[key]?.setError(message);
    if (message) {
      ui.errors[key] = message;
      found.push({ id: key === "date" ? "day0" : `q-${key}`, key, message });
    } else delete ui.errors[key];
  }
  return found;
}

// ---------------------------------------------------------------- stepping
function showStep() {
  if (!el.first) return;
  const check = STEPS[ui.step] === "check";
  el.first.hidden = check;
  el.check.hidden = !check;
  el.progressText.textContent = t("enrol.progress", { n: ui.step + 1, total: SCREENS });
  el.progressBar.style.inlineSize = `${Math.round(((ui.step + 1) / SCREENS) * 100)}%`;
  el.next.textContent = check ? t("enrol.submit") : t("buttons.next");
  paintSummary();
}

function goBack(event) {
  if (ui.step === 0) return; // the link goes to Today
  event.preventDefault();
  ui.step -= 1;
  showStep();
  el.register.focus();
}

async function advance() {
  if (STEPS[ui.step] === "first") {
    const found = validate(["register", "age"]);
    if (found.length) {
      replace(el.problems, errorSummary(found.map((f) => ({ id: f.id, message: f.message }))));
      return el[found[0].key].focus();
    }
    replace(el.problems);
    ui.step += 1;
    showStep();
    return el.check.querySelector("h2").focus();
  }
  // The check screen: look at everything once more, then send.
  const found = validate(["register", "age", "date"]);
  if (found.length) {
    replace(el.problems, errorSummary(found.map((f) => ({ id: f.id, message: f.message }))));
    if (found[0].key === "date") toggleEdit("date", true);
    else backToFirst(found[0].key);
    return;
  }
  replace(el.problems);
  await runWrite(
    el.next,
    async () => {
      ui.created = await api.post("/api/courses", {
        clinic_id: ui.cfg.clinic_id,
        register_no: values.register_no.trim(),
        patient_type: values.patient_type,
        regimen_id: values.regimen_id,
        lang: values.lang,
        day0: values.day0,
        eligibility: values.eligibility,
      });
      host.replaceChildren(doneView());
    },
    {
      onError: (err) => {
        // The server's own words, and what you typed stays.
        // The frame already toasts a read-only refusal in the server's words, so only a real refusal gets the summary.
        replace(el.problems, err.isReadOnly ? null : errorSummary([{ id: "enrol-title", message: err.message }]), h("p", { class: "unsent", role: "status" }, icon("alert"), h("span", null, t("states.notSent"))));
        el.problems.firstElementChild?.focus();
      },
    },
  );
}

function paintSummary() {
  if (!el.rows) return;
  for (const row of Object.values(el.rows)) replace(row.value, row.text());
}

// ------------------------------------------------------------- after enrol
function doneView() {
  const course = ui.created;
  const cfg = ui.cfg;
  const status = h("div", { class: "row", role: "status" });
  const showStatus = (c) => {
    if (c.status === "awaiting_call") replace(status, statusPill("clinicRow", "awaiting_call"));
    else replace(status, pill({ tone: "primary", iconName: "check", text: t("enrol.done.linked") }), h("a", { href: `/clinic/p/${encodeURIComponent(c.id)}` }, t("enrol.done.open")));
  };
  showStatus(course);
  const stream = sharedStream();
  stream.on("course", async (event) => {
    if (event.data.course_id !== course.id) return;
    try {
      showStatus(await api.get(`/api/courses/${encodeURIComponent(course.id)}`));
    } catch (err) {
      console.error(err);
    }
  });
  const how = [];
  if (cfg.ct_number) {
    how.push(h("p", null, t("enrol.done.ask")), h("p", { class: "number num", dir: "ltr" }, cfg.ct_number));
  }
  if (cfg.sim_phone) {
    how.push(h("p", null, cfg.ct_number ? t("enrol.done.orSim") : t("enrol.done.askSim")), h("div", null, h("a", { class: "btn", href: "/phone", target: "_blank", rel: "noopener" }, icon("phone"), t("enrol.done.phone"))));
  }
  if (!cfg.ct_number && !cfg.sim_phone) how.push(banner({ text: t("enrol.done.noNumber") }));
  return h(
    "div",
    { class: "stack stack--l" },
    h("div", { class: "page-title" }, h("h1", null, t("enrol.done.title", { no: course.register_no ?? "" }))),
    h("section", { class: "card stack" }, ...how, status),
    h(
      "div",
      { class: "enrol__nav enrol__nav--done" },
      h("button", { type: "button", class: "btn", onClick: again }, icon("plus"), t("enrol.done.another")),
      h("a", { class: "btn btn--primary", href: withClinic("/clinic", clinicParam()) }, t("enrol.done.today")),
    ),
  );
}

function again() {
  ui.created = null;
  ui.step = 0;
  ui.errors = {};
  Object.assign(values, { register_no: "", patient_type: "new", regimen_id: ui.cfg.default_regimen, lang: ui.cfg.default_lang, day0: ui.today, eligibility: null });
  host.replaceChildren(formView());
}
