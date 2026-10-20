// M2 People. One job: who can sign in and as what. Add a person, reset a password, switch someone off.
// GET|POST|PUT /api/admin/people. A password is made here, sent once and shown once; it is never kept.

import { api } from "/js/core/api.js";
import { bdi, h } from "/js/core/dom.js";
import { t } from "/js/core/i18n.js";
import { confirmDialog, dataTable, emptyState, field, icon, infoButton, pill, selectInput, textInput, toast } from "/js/ui/index.js";
import { ApiError, formDialog, formError, guarded, makePassword, passwordDialog, region, runWrite, writeAttrs } from "./common.js";

const ROLES = ["clinic", "abc", "vet", "oversight", "reviewer", "admin"];
const FIELD_OF = { username_taken: "username", unknown_clinic: "clinic" };

export function peopleTab({ me, signal }) {
  const host = h("div", { class: "stack" });
  const add = h(
    "button",
    { type: "button", class: "btn btn--primary", "data-key": "add-person", ...writeAttrs(me), onClick: guarded(me, () => openAdd()) },
    icon("plus"),
    t("admin.people.add"),
  );
  host.append(
    h("div", { class: "list-head" }, h("h2", { class: "visually-hidden" }, t("admin.people.title")), infoButton({ topic: t("admin.people.topic"), title: t("admin.people.topic"), content: h("p", null, t("admin.people.about")) }), add),
  );
  const body = h("div", { class: "stack" });
  host.append(body);
  // The clinics are read too, so a clinic login can be given its clinic in the add form.
  const view = region(body, {
    load: async () => {
      const [people, clinics] = await Promise.all([api.get("/api/admin/people"), api.get("/api/admin/clinics")]);
      return { people, clinics };
    },
    render: ({ people }) => {
      if (people.length === 0) return emptyState({ title: t("admin.people.emptyTitle"), body: t("admin.people.emptyBody") });
      return dataTable({
        columns: [
          { key: "name", label: t("admin.people.cols.name"), render: (p) => h("div", { class: "adm-name" }, h("strong", null, bdi(p.display_name)), h("span", { class: "muted small" }, bdi(p.username))) },
          { key: "role", label: t("admin.people.cols.role"), render: (p) => t(`roles.${p.role}`) },
          { key: "clinic", label: t("admin.people.cols.clinic"), render: (p) => (p.clinic_name ? bdi(p.clinic_name) : h("span", { class: "muted" }, t("admin.people.noClinic"))) },
          { key: "status", label: t("admin.people.cols.status"), render: (p) => (p.active ? pill({ tone: "primary", iconName: "check", text: t("admin.people.active") }) : pill({ tone: "neutral", iconName: "x", text: t("admin.people.off") })) },
          { key: "act", label: t("admin.people.cols.actions"), render: (p) => actions(p) },
        ],
        rows: people,
      });
    },
    signal,
  });

  function actions(p) {
    const named = { name: p.display_name };
    return h(
      "div",
      { class: "adm-actions" },
      h("button", { type: "button", class: "btn", "data-key": `reset-${p.id}`, "aria-label": t("admin.people.resetNamed", named), ...writeAttrs(me), onClick: guarded(me, (e) => resetPassword(p, e.currentTarget)) }, t("admin.people.reset")),
      h(
        "button",
        { type: "button", class: "btn", "data-key": `toggle-${p.id}`, "aria-label": t(p.active ? "admin.people.switchOffNamed" : "admin.people.switchOnNamed", named), ...writeAttrs(me), onClick: guarded(me, (e) => toggle(p, e.currentTarget)) },
        t(p.active ? "admin.people.switchOff" : "admin.people.switchOn"),
      ),
    );
  }

  async function resetPassword(p, button) {
    const ok = await confirmDialog({ title: t("admin.people.resetTitle", { name: p.display_name }), body: t("admin.people.resetBody"), confirmLabel: t("admin.people.reset") });
    if (!ok) return button.isConnected && button.focus();
    const password = makePassword();
    const done = await runWrite(button, async () => {
      await api.put("/api/admin/people", { id: p.id, password });
      return true;
    });
    if (!done) return undefined;
    await passwordDialog({ title: t("admin.people.newPasswordTitle", { name: p.display_name }), lead: t("admin.people.newPasswordLead"), password });
    await view.reload({ quiet: true });
    host.querySelector(`[data-key="reset-${p.id}"]`)?.focus();
    return undefined;
  }

  async function toggle(p, button) {
    if (p.active) {
      const ok = await confirmDialog({ title: t("admin.people.offTitle", { name: p.display_name }), body: t("admin.people.offBody"), confirmLabel: t("admin.people.switchOff") });
      if (!ok) return button.isConnected && button.focus();
    }
    const done = await runWrite(button, async () => {
      await api.put("/api/admin/people", { id: p.id, active: !p.active });
      return true;
    });
    if (done) {
      toast(t(p.active ? "admin.people.switchedOff" : "admin.people.switchedOn", { name: p.display_name }));
      await view.reload({ quiet: true });
    }
    host.querySelector(`[data-key="toggle-${p.id}"]`)?.focus();
    return undefined;
  }

  async function openAdd() {
    let clinics;
    try {
      clinics = await api.get("/api/admin/clinics");
    } catch (err) {
      if (err instanceof ApiError) return toast(err.message, { tone: "attention" });
      throw err;
    }
    const password = makePassword();
    const errors = h("div", { class: "stack stack--s", "aria-live": "polite" });
    const f = {
      username: field({ label: t("admin.people.fields.username"), hint: t("admin.people.usernameHint"), control: textInput({ name: "username", autocomplete: "off", maxlength: 32 }) }),
      name: field({ label: t("admin.people.fields.name"), control: textInput({ name: "display_name", autocomplete: "off", maxlength: 80 }) }),
      role: field({ label: t("admin.people.fields.role"), control: selectInput({ name: "role", value: "clinic", options: ROLES.map((r) => ({ value: r, label: t(`roles.${r}`) })) }) }),
      clinic: field({
        label: t("admin.people.fields.clinic"),
        control: selectInput({ name: "clinic_id", value: "", options: [{ value: "", label: t("admin.clinics.choose") }, ...clinics.map((c) => ({ value: c.id, label: c.name }))] }),
      }),
    };
    const syncClinic = () => {
      f.clinic.el.hidden = f.role.control.value !== "clinic";
    };
    f.role.control.addEventListener("change", syncClinic);
    syncClinic();
    const made = { name: "" };
    const created = await formDialog({
      title: t("admin.people.addTitle"),
      body: h("div", { class: "stack" }, errors, h("div", { class: "adm-form" }, f.username.el, f.name.el, f.role.el, f.clinic.el), h("p", { class: "muted small" }, t("admin.people.passwordNote"))),
      confirmLabel: t("admin.people.add"),
      wide: true,
      onConfirm: async () => {
        errors.replaceChildren();
        for (const x of Object.values(f)) x.setError(null);
        const values = { username: f.username.control.value.trim(), display_name: f.name.control.value.trim(), role: f.role.control.value, clinic_id: f.role.control.value === "clinic" ? f.clinic.control.value || null : null };
        let bad = false;
        for (const [k, v] of [["username", values.username], ["name", values.display_name]]) {
          if (!v) {
            f[k].setError(t("admin.required"));
            bad = true;
          }
        }
        if (values.role === "clinic" && !values.clinic_id) {
          f.clinic.setError(t("admin.required"));
          bad = true;
        }
        if (bad) return false;
        try {
          await api.post("/api/admin/people", { ...values, password }, { quiet: true });
          made.name = values.display_name;
          return true;
        } catch (err) {
          if (!(err instanceof ApiError)) throw err;
          const key = FIELD_OF[err.code];
          if (key) f[key].setError(err.message);
          else errors.replaceChildren(formError(err.message));
          return false;
        }
      },
    });
    if (created) {
      view.reload({ quiet: true });
      await passwordDialog({ title: t("admin.people.newPasswordTitle", { name: made.name }), lead: t("admin.people.newPasswordLead"), password });
    }
    host.querySelector('[data-key="add-person"]')?.focus();
    return undefined;
  }
  return host;
}
