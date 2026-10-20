// M5 System. The only home of internals, and the only place red is normal: a check that is a real fault is
// red, a part that is switched off on purpose is said plainly and is not. GET /api/system/status,
// POST /api/system/telephony/reconnect, POST /api/demo/reset (demo mode only, behind a confirmation).

import { api, resetMe, getMe } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { currentLang, t, has } from "/js/core/i18n.js";
import { banner, confirmDialog, icon, pill, toast } from "/js/ui/index.js";
import { guarded, region, runWrite, writeAttrs } from "./common.js";
import { showMissing } from "./prompts.js";

const VOICE = /^voice_(hi|doi|ur)$/;
const checkLabel = (name) => (has(`admin.system.checks.${name}`) ? t(`admin.system.checks.${name}`) : name);

export function systemTab({ me, signal, openPrompts, onReset }) {
  const host = h("div", { class: "stack" });
  host.append(h("h2", { class: "visually-hidden" }, t("admin.system.title")));
  const body = h("div", { class: "stack stack--l" });
  host.append(body);
  let view = null;

  view = region(body, {
    load: () => api.get("/api/system/status"),
    render: (status) => {
      const faults = status.checks.filter((c) => c.alarm).length;
      const working = status.checks.filter((c) => c.ok).length;
      const parts = [];
      if (faults > 0) parts.push(banner({ tone: "alarm", text: t("admin.system.faults", { count: faults }) }));
      parts.push(
        h(
          "div",
          { class: "stack" },
          h("p", { class: "adm-summary" }, t("admin.system.summary", { ok: working, total: status.checks.length })),
          facts(status),
          h("ul", { class: "adm-checks" }, status.checks.map((c) => checkRow(c, status))),
        ),
      );
      if (me.mode === "demo") parts.push(demoCard());
      return h("div", { class: "stack stack--l" }, parts);
    },
    events: ["system", "clock"],
    signal,
    lines: 8,
  });

  function facts(status) {
    const item = (label, value) => h("div", { class: "stat" }, h("span", { class: "stat__label" }, label), h("span", { class: "adm-fact" }, value));
    return h(
      "div",
      { class: "stats adm-facts" },
      item(t("admin.system.mode"), t(`admin.system.modes.${status.mode}`)),
      item(t("admin.system.link"), has(`admin.system.links.${status.telephony}`) ? t(`admin.system.links.${status.telephony}`) : status.telephony),
      item(t("admin.system.policy"), status.policy_version),
    );
  }

  function checkRow(c, status) {
    const lang = VOICE.exec(c.name)?.[1];
    const missing = lang ? status.prompts_missing[lang]?.length ?? 0 : 0;
    const state = c.ok ? "ok" : c.alarm ? "fault" : c.name === "purge" ? "late" : lang ? "notready" : "off";
    const pills = {
      ok: pill({ tone: "primary", iconName: "check", text: t("status.system.ok") }),
      fault: pill({ tone: "alarm", iconName: "alert", text: t("status.system.fault") }),
      late: pill({ tone: "attention", iconName: "clock", text: t("admin.system.late") }),
      notready: pill({ tone: "attention", iconName: "alert", text: t("admin.system.notReady") }),
      off: pill({ tone: "neutral", iconName: "x", text: t("admin.system.off") }),
    };
    const actions = [];
    if (c.name === "telephony" && !c.ok) {
      actions.push(
        h("button", { type: "button", class: "btn", "data-key": "reconnect", ...writeAttrs(me), onClick: guarded(me, (e) => reconnect(e.currentTarget)) }, icon("refresh"), t("admin.system.reconnect")),
      );
    }
    if (lang && missing > 0) {
      actions.push(
        h("button", { type: "button", class: "btn", "data-key": `open-${lang}`, onClick: () => { showMissing(lang); openPrompts(); } }, t("admin.system.openPrompts")),
      );
    }
    return h(
      "li",
      { class: ["adm-check", state === "fault" ? "adm-check--fault" : ""] },
      h("div", { class: "adm-check__main" }, h("strong", null, checkLabel(c.name)), h("span", { class: "adm-check__detail", lang: currentLang() === "en" ? null : "en" }, c.detail)),
      h("div", { class: "adm-check__side" }, pills[state], actions),
    );
  }

  async function reconnect(button) {
    const check = await runWrite(button, () => api.post("/api/system/telephony/reconnect"));
    if (check) {
      toast(check.detail, { tone: check.ok ? "neutral" : "attention" });
      await view.reload({ quiet: true });
    }
    body.querySelector('[data-key="reconnect"]')?.focus();
  }

  function demoCard() {
    return h(
      "section",
      { class: "card adm-demo", "aria-labelledby": "demo-h" },
      h("h3", { id: "demo-h" }, t("admin.system.demo.title")),
      h("p", { class: "muted" }, t("admin.system.demo.body")),
      h("div", null, h("button", { type: "button", class: "btn", "data-key": "reset-demo", ...writeAttrs(me), onClick: guarded(me, (e) => resetDemo(e.currentTarget)) }, t("admin.system.demo.button"))),
    );
  }

  async function resetDemo(button) {
    const ok = await confirmDialog({
      title: t("admin.system.demo.confirmTitle"),
      body: t("admin.system.demo.confirmBody"),
      confirmLabel: t("admin.system.demo.button"),
    });
    if (!ok) return button.isConnected && button.focus();
    const done = await runWrite(button, async () => {
      await api.post("/api/demo/reset");
      // The reset signs this administrator in again with a new CSRF token and cookie: ask /api/me once more.
      resetMe();
      await getMe();
      return true;
    });
    if (done) {
      toast(t("admin.system.demo.done"));
      onReset();
    }
    return undefined;
  }

  return host;
}
