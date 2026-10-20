// The list of analysis areas, and the look-here cards. Both work without the map.

import { bdi, h } from "/js/core/dom.js";
import { clusterBin } from "/js/core/map.js";
import { formatCount } from "/js/core/format.js";
import { t } from "/js/core/i18n.js";
import { pill } from "/js/ui/index.js";
import { shareLine, unsureLine } from "./lib.js";

export const areaHref = (id) => `/team/cluster/${encodeURIComponent(id)}`;

/** The one short line under a flagged area (its pill says "Look here"); the full rule is behind the (i). */
function lookLine(c) {
  const data = c.look_here_reason_data;
  if (!c.look_here || !data) return null;
  return t("team.lookHere.strong", { n: formatCount(data.n) });
}

/** One area: colour chip (the map's own colour), name, its share with interval and n, flags. */
function areaRow(c) {
  const s = c.share;
  const facts = [unsureLine(s)];
  if (s.status === "bracketed") facts.push(t("team.fewer50"));
  const why = lookLine(c);
  const flags = [];
  if (c.look_here) flags.push(pill({ tone: "attention", iconName: "alert", text: t("map.lookHere") }));
  if (c.simulated_bites > 0) flags.push(pill({ tone: "simulated", iconName: "phone", text: t("map.simulated") }));
  return h(
    "a",
    { class: "list__row list__row--link area-row", href: areaHref(c.id), dataset: { cluster: c.id } },
    h("span", { class: `sw sw--${clusterBin(s)} area-row__chip`, "aria-hidden": "true" }),
    h(
      "div",
      { class: "list__main" },
      h("span", { class: "list__title" }, bdi(c.name)),
      h("span", { class: "list__meta" }, shareLine(s)),
      facts.some(Boolean) ? h("span", { class: "list__meta" }, facts.filter(Boolean).join(" · ")) : null,
      why ? h("span", { class: "list__meta area-row__why" }, why) : null,
    ),
    flags.length ? h("div", { class: "list__side" }, flags) : null,
  );
}

export function areaList(clusters) {
  return h("div", { class: "list area-list" }, clusters.map(areaRow));
}

/** Highlight the row of the area a clicked ward belongs to. */
export function markArea(listEl, clusterId) {
  for (const row of listEl.querySelectorAll(".area-row")) {
    if (row.dataset.cluster === clusterId) row.setAttribute("aria-current", "true");
    else row.removeAttribute("aria-current");
  }
}
