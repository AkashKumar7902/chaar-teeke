// M1 Clinics. One job: set a clinic's schedule, call language, visit time and phone, and add a clinic.
// GET|POST|PUT /api/admin/clinics. The schedule list is GET /api/admin/regimens (every schedule; the form offers the enabled ones),
// so a first clinic can be added when there is none to ask through.

import { api } from "/js/core/api.js";
import { bdi, h } from "/js/core/dom.js";
import { has, t } from "/js/core/i18n.js";
import { maskedPhone } from "/js/core/format.js";
import { dataTable, emptyState, field, icon, infoButton, pill, selectInput, textInput, toast, unsentNote } from "/js/ui/index.js";
import { ApiError, formDialog, formError, guarded, region, writeAttrs } from "./common.js";

const LANGS = ["hi", "doi", "ur"];
const ARV = ["confirmed", "unverified"];
/** Which field a server refusal belongs to; any other refusal goes in the form's error slot. */
const FIELD_OF = { language_not_ready: "lang", regimen_disabled: "regimen", unknown_regimen: "regimen", unknown_ward: "ward" };

/** A schedule's name in the page language; the server's own id when this page has no words for it. */
export const regName = (id) => (has(`admin.regimen.${id}`) ? t(`admin.regimen.${id}`) : id);

const digits = (phone) => String(phone ?? "").replace(/\D/g, "");

export function clinicsTab({ me, signal }) {
  const host = h("div", { class: "stack" });
  let view = null;

  const add = h(
    "button",
    { type: "button", class: "btn btn--primary", "data-key": "add-clinic", ...writeAttrs(me), onClick: guarded(me, () => openForm(null)) },
    icon("plus"),
    t("admin.clinics.add"),
  );
  const head = h(
    "div",
    { class: "list-head" },
    h("h2", { class: "visually-hidden" }, t("admin.clinics.title")),
    infoButton({ topic: t("admin.clinics.topic"), title: t("admin.clinics.topic"), content: aboutClinics() }),
    add,
  );
  const body = h("div", { class: "stack" });
  host.append(head, body);
  view = region(body, { load: () => api.get("/api/admin/clinics"), render, signal });

  function render(clinics) {
    if (clinics.length === 0) {
      return emptyState({ title: t("admin.clinics.emptyTitle"), body: t("admin.clinics.emptyBody") });
    }
    return dataTable({
      caption: null,
      columns: [
        { key: "name", label: t("admin.clinics.cols.name"), render: (c) => h("div", { class: "adm-name" }, h("strong", null, bdi(c.name)), h("span", { class: "muted small" }, bdi(c.short_name))) },
        { key: "ward", label: t("admin.clinics.cols.ward"), render: (c) => h("span", { class: ["adm-nowrap", c.ward_no === null ? "muted" : ""] }, c.ward_no === null ? t("admin.clinics.noWard") : t("admin.clinics.wardNo", { no: c.ward_no })) },
        { key: "regimen", label: t("admin.clinics.cols.regimen"), render: (c) => regName(c.default_regimen) },
        { key: "lang", label: t("admin.clinics.cols.lang"), render: (c) => t(`admin.lang.${c.default_lang}`) },
        { key: "visit", label: t("admin.clinics.cols.visit"), render: (c) => c.visit_time ?? notSet() },
        { key: "phone", label: t("admin.clinics.cols.phone"), render: (c) => (c.phone ? maskedPhone(digits(c.phone).slice(-4)) : notSet()) },
        { key: "arv", label: t("admin.clinics.cols.arv"), render: (c) => arvPill(c.arv_status) },
        {
          key: "edit",
          label: t("admin.clinics.cols.actions"),
          render: (c) =>
            h("button", { type: "button", class: "btn", "data-key": `edit-${c.id}`, "aria-label": t("admin.clinics.editNamed", { name: c.name }), ...writeAttrs(me), onClick: guarded(me, () => openForm(c)) }, t("admin.clinics.edit")),
        },
      ],
      rows: clinics,
    });
  }

  async function openForm(clinic) {
    // The lists the form offers come from the server. One that fails to load disables its own field and
    // says so; the other fields still work and a save sends only what changed.
    const [wards, regimens] = await Promise.allSettled([api.get("/api/wards"), api.get("/api/admin/regimens")]);
    const form = buildForm({ clinic, wards, regimens });
    const saved = await formDialog({
      title: clinic ? t("admin.clinics.editTitle", { name: clinic.name }) : t("admin.clinics.addTitle"),
      body: form.el,
      confirmLabel: clinic ? t("buttons.save") : t("admin.clinics.add"),
      wide: true,
      onConfirm: () => form.submit(),
    });
    if (saved) {
      toast(clinic ? t("admin.clinics.saved", { name: form.savedName() }) : t("admin.clinics.added", { name: form.savedName() }));
      view.reload({ quiet: true });
    }
    const back = host.querySelector(`[data-key="${clinic ? `edit-${clinic.id}` : "add-clinic"}"]`);
    back?.focus();
  }

  return host;
}

function notSet() {
  return pill({ tone: "attention", iconName: "alert", text: t("admin.clinics.notSet") });
}

function arvPill(status) {
  return status === "confirmed"
    ? pill({ tone: "primary", iconName: "check", text: t("admin.clinics.arv.confirmed") })
    : pill({ tone: "attention", iconName: "info", text: t("admin.clinics.arv.unverified") });
}

function aboutClinics() {
  return h(
    "div",
    { class: "stack stack--s" },
    h("p", null, t("admin.clinics.about.arv")),
    h("p", null, t("admin.clinics.about.gaps")),
    h("p", null, t("admin.clinics.about.phone")),
  );
}

function buildForm({ clinic, wards, regimens }) {
  const fields = {};
  const errors = h("div", { class: "stack stack--s", "aria-live": "polite" });
  let savedName = clinic?.name ?? "";

  fields.name = field({ label: t("admin.clinics.fields.name"), control: textInput({ name: "name", value: clinic?.name ?? "", maxlength: 120, autocomplete: "off" }) });
  fields.short = field({ label: t("admin.clinics.fields.short"), control: textInput({ name: "short_name", value: clinic?.short_name ?? "", maxlength: 30, autocomplete: "off" }) });

  // Ward
  const wardOk = wards.status === "fulfilled";
  const wardOptions = [{ value: "", label: t("admin.clinics.noWard") }];
  if (wardOk) {
    for (const w of wards.value.slice().sort((a, b) => a.ward_no - b.ward_no)) wardOptions.push({ value: String(w.ward_no), label: `${w.ward_no} ${w.ward_name ?? ""}`.trim() });
  } else if (clinic?.ward_no != null) {
    wardOptions.push({ value: String(clinic.ward_no), label: t("admin.clinics.wardNo", { no: clinic.ward_no }) });
  }
  fields.ward = field({
    label: t("admin.clinics.fields.ward"),
    control: selectInput({ name: "ward_no", options: wardOptions, value: clinic?.ward_no == null ? "" : String(clinic.ward_no), disabled: !wardOk }),
    error: wardOk ? null : t("admin.clinics.listFailed", { what: t("admin.clinics.fields.ward") }),
  });

  // Schedule: enabled ones only
  const regOk = regimens.status === "fulfilled";
  const regOptions = [];
  if (regOk) {
    if (!clinic) regOptions.push({ value: "", label: t("admin.clinics.choose") });
    const enabled = regimens.value.filter((r) => r.enabled);
    for (const r of enabled) regOptions.push({ value: r.id, label: regName(r.id) });
    if (clinic && !enabled.some((r) => r.id === clinic.default_regimen)) {
      regOptions.unshift({ value: clinic.default_regimen, label: t("admin.clinics.regimenOff", { name: regName(clinic.default_regimen) }) });
    }
  } else if (clinic) {
    regOptions.push({ value: clinic.default_regimen, label: regName(clinic.default_regimen) });
  } else {
    regOptions.push({ value: "", label: t("admin.clinics.choose") });
  }
  fields.regimen = field({
    label: t("admin.clinics.fields.regimen"),
    hint: regOk ? t("admin.clinics.regimenHint") : null,
    control: selectInput({ name: "default_regimen", options: regOptions, value: clinic?.default_regimen ?? "", disabled: !regOk }),
    error: regOk ? null : t("admin.clinics.listFailed", { what: t("admin.clinics.fields.regimen") }),
  });
  if (regOk && clinic) {
    const sel = fields.regimen.control;
    if (!regimens.value.some((r) => r.enabled && r.id === clinic.default_regimen)) sel.options[0].disabled = true;
  }

  // the days of the chosen schedule, under the select
  if (regOk) {
    const daysNote = h("p", { class: "field__hint", "aria-live": "polite" });
    const showDays = () => {
      const r = regimens.value.find((x) => x.id === fields.regimen.control.value);
      daysNote.textContent = r ? t("admin.clinics.days", { list: r.days.join(", ") }) : "";
    };
    fields.regimen.control.addEventListener("change", showDays);
    fields.regimen.el.append(daysNote);
    showDays();
  }

  fields.lang = field({
    label: t("admin.clinics.fields.lang"),
    control: selectInput({ name: "default_lang", options: LANGS.map((l) => ({ value: l, label: t(`admin.lang.${l}`) })), value: clinic?.default_lang ?? "hi" }),
  });
  fields.visit = field({
    label: t("admin.clinics.fields.visit"),
    hint: t("admin.clinics.visitHint"),
    control: textInput({ name: "visit_time", inputmode: "numeric", autocomplete: "off", placeholder: "09:30", maxlength: 5, value: clinic?.visit_time ?? "" }),
  });
  fields.phone = field({
    label: t("admin.clinics.fields.phone"),
    hint: t("admin.clinics.phoneHint"),
    control: textInput({ name: "phone", type: "tel", inputmode: "tel", autocomplete: "off", value: clinic?.phone ?? "", maxlength: 30 }),
  });
  fields.arv = field({
    label: t("admin.clinics.fields.arv"),
    control: selectInput({ name: "arv_status", options: ARV.map((a) => ({ value: a, label: t(`admin.clinics.arv.${a}`) })), value: clinic?.arv_status ?? "unverified" }),
  });

  const el = h(
    "div",
    { class: "stack" },
    errors,
    h("div", { class: "adm-form" }, fields.name.el, fields.short.el, fields.ward.el, fields.regimen.el, fields.lang.el, fields.visit.el, fields.phone.el, fields.arv.el),
  );

  function read() {
    const v = (f) => f.control.value.trim();
    return {
      name: v(fields.name),
      short_name: v(fields.short),
      ward_no: fields.ward.control.value === "" ? null : Number(fields.ward.control.value),
      default_regimen: fields.regimen.control.value,
      default_lang: fields.lang.control.value,
      visit_time: v(fields.visit) === "" ? null : v(fields.visit),
      phone: v(fields.phone) === "" ? null : v(fields.phone),
      arv_status: fields.arv.control.value,
    };
  }

  async function submit() {
    errors.replaceChildren();
    for (const f of Object.values(fields)) if (f.control.getAttribute("aria-invalid") && !f.control.disabled) f.setError(null);
    const values = read();
    let bad = false;
    for (const [key, name] of [["name", "name"], ["short", "short_name"]]) {
      if (!values[name]) {
        fields[key].setError(t("admin.required"));
        bad = true;
      }
    }
    const time = /^(\d{1,2}):(\d{2})$/.exec(values.visit_time ?? "");
    if (values.visit_time !== null && !(time && Number(time[1]) < 24 && Number(time[2]) < 60)) {
      fields.visit.setError(t("admin.clinics.timeBad"));
      bad = true;
    } else if (time) {
      values.visit_time = `${time[1].padStart(2, "0")}:${time[2]}`;
    }
    if (!values.default_regimen) {
      fields.regimen.setError(t("admin.required"));
      bad = true;
    }
    if (bad) return false;
    try {
      if (clinic) {
        const body = { id: clinic.id };
        for (const [k, v] of Object.entries(values)) if (v !== (clinic[k] ?? null)) body[k] = v;
        if (Object.keys(body).length === 1) return true;
        const out = await api.put("/api/admin/clinics", body, { quiet: true });
        savedName = out.name;
      } else {
        const out = await api.post("/api/admin/clinics", values, { quiet: true });
        savedName = out.name;
      }
      return true;
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      // The server names a field by code for some refusals; for a bad value it only says so in words.
      const key = FIELD_OF[err.code] ?? (err.code === "bad_request" ? (/phone/i.test(err.message) ? "phone" : /visit time/i.test(err.message) ? "visit" : null) : null);
      if (key) fields[key].setError(err.message);
      else errors.replaceChildren(formError(err.message));
      if (err.isNetwork) errors.append(unsentNote());
      return false;
    }
  }

  return { el, submit, savedName: () => savedName };
}
