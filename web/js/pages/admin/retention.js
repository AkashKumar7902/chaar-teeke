// M4 Retention. One job: show that deletion ran. When the job last ran, what is held, what is due, what
// has been deleted, and the rule for each kind. GET /api/admin/retention.

import { api } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { agoText, formatCount, formatDateTime } from "/js/core/format.js";
import { t } from "/js/core/i18n.js";
import { icon, pill } from "/js/ui/index.js";
import { region, serverNow } from "./common.js";

// Each kind: its key in `pending`, in `deleted`, and the rule line (strings come from `rules`).
const KINDS = [
  { id: "phones", pending: "phones", due: "phones_due", deleted: "phone", rule: (r) => ({ days: r.phone_purge_days_after_last_dose }) },
  { id: "clips", pending: "clips", due: "clips_due", deleted: "clip", rule: (r) => ({ days: r.clip_max_age_days }) },
  { id: "missed", pending: "missed_calls", due: "missed_calls_due", deleted: "missed_call", rule: (r) => ({ hours: r.unlinked_missed_call_hours }) },
];

export function retentionTab({ me, signal }) {
  const host = h("div", { class: "stack" });
  host.append(h("h2", { class: "visually-hidden" }, t("admin.retention.title")));
  const body = h("div", { class: "stack" });
  host.append(body);
  region(body, {
    load: async () => {
      const [report, now] = await Promise.all([api.get("/api/admin/retention"), serverNow(me)]);
      return { report, now };
    },
    render: ({ report, now }) => {
      const last = report.last_run
        ? h("p", { class: "adm-lastrun" }, icon("clock"), h("span", null, t("admin.retention.lastRun", { when: formatDateTime(report.last_run) }), " ", Date.parse(now) - Date.parse(report.last_run) >= 60000 ? h("span", { class: "muted" }, `(${agoText(report.last_run, now)})`) : null))
        : h("p", { class: "adm-lastrun adm-lastrun--none" }, pill({ tone: "attention", iconName: "alert", text: t("admin.retention.neverRun") }));
      return h(
        "div",
        { class: "stack" },
        last,
        h(
          "div",
          { class: "adm-kinds" },
          KINDS.map((k) => card(k, report)),
        ),
      );
    },
    events: ["system", "clock"],
    signal,
  });
  return host;
}

function card(kind, report) {
  const due = report.pending[kind.due];
  return h(
    "section",
    { class: "card adm-kind", "aria-labelledby": `ret-${kind.id}` },
    h("h3", { id: `ret-${kind.id}` }, t(`admin.retention.kinds.${kind.id}`)),
    h("p", { class: "muted" }, t(`admin.retention.rules.${kind.id}`, kind.rule(report.rules))),
    h(
      "dl",
      { class: "adm-counts" },
      count(t("admin.retention.held"), report.pending[kind.pending]),
      count(t("admin.retention.due"), due, due > 0),
      count(t("admin.retention.deleted"), report.deleted[kind.deleted]),
    ),
  );
}

function count(label, n, attention = false) {
  return h("div", { class: ["adm-count", attention ? "adm-count--attention" : ""] }, h("dt", null, label), h("dd", { class: "num" }, formatCount(n)));
}
