// A1 Coverage. One number (notched share of street-dog bites where the patient could tell) with its
// interval, n, could-not-see count and the claimed band; the look-here areas; the areas as a list; the ward
// map as a second view of the same numbers. Live through the shared event stream.

import { api, getMode } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { formatCount, formatPercent } from "/js/core/format.js";
import { t } from "/js/core/i18n.js";
import { demoDataInfo, infoButton, shareDisplay } from "/js/ui/index.js";
import { mapLegend, mapView } from "/js/ui/mapView.js";
import { areaHref, areaList, markArea } from "./team/areas.js";
import { boot, dataView, windowText } from "./team/lib.js";

await boot(["team"], "team.title");

const main = document.getElementById("main");
const site = await getMode();
const titleLine = h("p", { class: "muted page-title__window" });
let demoInfo = demoDataInfo(site);
const body = h("div", { class: "stack stack--l" });
main.append(h("div", { class: "page-title" }, h("h1", null, t("team.title")), titleLine, demoInfo), body);
document.addEventListener("ct:langchange", () => {
  main.querySelector("h1").textContent = t("team.title");
  if (demoInfo) {
    const fresh = demoDataInfo(site);
    demoInfo.replaceWith(fresh);
    demoInfo = fresh;
  }
  if (view.data) titleLine.textContent = windowText(view.data.window_start, view.data.window_end);
});

// ---------------------------------------------------------------- the map (built once, kept across reloads)
let map = null;
let mapNote = null;
let areas = null;
let lastClusters = [];

function aboutShare(c) {
  const lines = [h("p", null, t("team.about.p1")), h("p", null, t("team.about.p2")), h("p", null, t("team.about.p3"))];
  const s = c.city;
  if (typeof s.bound_low === "number" && typeof s.bound_high === "number") {
    lines.push(h("p", null, t("team.about.bounds", { low: formatPercent(s.bound_low), high: formatPercent(s.bound_high) })));
  }
  lines.push(h("p", { class: "muted" }, t("team.about.interval")));
  return h("div", { class: "stack stack--s" }, lines);
}

function hero(c) {
  const el = shareDisplay(c.city, {
    claim: { band: c.claim_band, labelKey: c.claim_label_key },
    lastChecked: null,
  });
  // The component's own (i) explains the arithmetic; this one also says what the number stands in for.
  const own = el.querySelector(".share__head .pop");
  const about = infoButton({ topic: t("team.about.topic"), title: t("team.about.topic"), content: aboutShare(c) });
  if (own) own.replaceWith(about);
  return h("div", { class: "card cov-hero" }, el);
}

function onPick(info) {
  if (!mapNote) return;
  if (!info) {
    mapNote.textContent = "";
    if (areas) markArea(areas, null);
    return;
  }
  const area = lastClusters.find((c) => c.id === info.cluster_id);
  const name = info.ward_name ?? "";
  mapNote.replaceChildren(
    area ? t("team.map.pick", { no: info.ward_no, name, area: area.name }) : t("team.map.pickNone", { no: info.ward_no, name }),
    area ? [" ", h("a", { href: areaHref(area.id) }, t("team.lookHere.open"))] : null,
  );
  if (areas) markArea(areas, info.cluster_id);
}

let root = null;
let unplacedEl = null;
/** Bites with no ward are said once, under the list; nothing is said when there are none. */
function setUnplaced(c) {
  const some = typeof c.could_not_place === "string" || c.could_not_place > 0;
  unplacedEl.textContent = some ? t("team.unplaced", { n: formatCount(c.could_not_place) }) : "";
  unplacedEl.hidden = !some;
}
function build(c) {
  lastClusters = c.clusters;
  titleLine.textContent = windowText(c.window_start, c.window_end);
  areas = areaList(c.clusters);
  const mapBox = h("section", { class: "stack stack--s cov-map", "aria-labelledby": "map-h" });
  mapNote = h("p", { class: "muted small cov-map__note", "aria-live": "polite" });
  map?.destroy();
  map = mapView({ onSelect: onPick });
  map.setData({ clusters: c.clusters });
  // Only the map box: the full legend is behind the (i), so "Look here" is not said a second time beside it.
  mapBox.append(
    h("div", { class: "list-head" }, h("h2", { class: "section-title", id: "map-h" }, t("map.label")), infoButton({ topic: t("team.map.about"), title: t("map.label"), content: mapLegend() })),
    map.el.firstElementChild,
    mapNote,
  );
  unplacedEl = h("p", { class: "faint small" });
  setUnplaced(c);
  // The answer and the areas on one side, the map on the other. An area the team should look at carries
  // one pill and one short line in its row; the full rule is behind the (i) of the areas.
  root = h(
    "div",
    { class: "cov-split" },
    h(
      "div",
      { class: "stack stack--l" },
      hero(c),
      h(
        "section",
        { class: "stack stack--s cov-areas", "aria-labelledby": "areas-h" },
        h(
          "div",
          { class: "list-head" },
          h("h2", { class: "section-title", id: "areas-h" }, t("team.areas.title")),
          infoButton({ topic: t("team.areas.topic"), title: t("team.areas.topic"), content: h("div", { class: "stack stack--s" }, h("p", null, t("team.areas.about")), h("p", null, t("team.lookHere.about"))) }),
        ),
        areas,
        unplacedEl,
      ),
    ),
    mapBox,
  );
  return root;
}

// The map is built once per language and kept while the numbers change, so a live update never resets it.
let builtFor = null;
const view = dataView(body, {
  load: () => api.get("/api/coverage"),
  render: (c) => {
    const key = document.documentElement.lang;
    if (root && builtFor === key) {
      refresh(c);
      return root;
    }
    builtFor = key;
    return build(c);
  },
  events: ["report", "alert"],
  block: true,
});

/** Same layout, new numbers: rebuild the text parts but keep the map and its position. */
function refresh(c) {
  const keepMap = map;
  lastClusters = c.clusters;
  titleLine.textContent = windowText(c.window_start, c.window_end);
  root.querySelector(".cov-hero").replaceWith(hero(c));
  const list = areaList(c.clusters);
  areas.replaceWith(list);
  areas = list;
  setUnplaced(c);
  keepMap.setData({ clusters: c.clusters });
}
