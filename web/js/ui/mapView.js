// The ward map as a component: the .map box, a loading and failure overlay with one Retry, and the legend.
// The page keeps a list beside it (the map is secondary, and not keyboard-reachable).

import { WardMap } from "../core/map.js";
import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { errorState, skeleton } from "./state.js";

export function mapLegend() {
  const item = (cls, text) => h("li", { class: "map-legend__item" }, h("span", { class: `sw ${cls}`, "aria-hidden": "true" }), text);
  return h(
    "div",
    null,
    h(
      "ul",
      { class: "map-legend" },
      item("sw--r1", t("map.bin1")),
      item("sw--r2", t("map.bin2")),
      item("sw--r3", t("map.bin3")),
      item("sw--r4", t("map.bin4")),
      item("sw--r5", t("map.bin5")),
      item("sw--hatch", t("map.tooFew")),
      item("sw--nodata", t("map.noData")),
      item("sw--sim", t("map.simulated")),
      item("sw--look", t("map.lookHere")),
    ),
    h("p", { class: "faint small" }, t("map.legendNote")),
  );
}

/**
 * mapView({basemap, onSelect}) -> {el, setData({clusters, simulatedWards}), select(wardNo), destroy()}
 * Append `el` to the page. Data set before the map is ready is applied when it is.
 */
export function mapView({ basemap = "none", onSelect } = {}) {
  const box = h("div", { class: "map", role: "region", "aria-label": t("map.label"), dir: "ltr" });
  const overlay = h("div", { class: "map__status" });
  box.append(overlay);
  const el = h("div", null, box, mapLegend());
  let wardMap = null;
  let pending = { clusters: [], simulatedWards: [] };
  let selected = null;

  function show(node) {
    overlay.replaceChildren(node);
    overlay.hidden = false;
  }

  async function start() {
    show(skeleton({ lines: 2, block: true }));
    wardMap?.destroy();
    wardMap = new WardMap(box, {
      labels: { zoomIn: t("map.zoomIn"), zoomOut: t("map.zoomOut"), attribution: t("map.attribution"), tipWard: t("map.ward") },
      basemap,
      onSelect: (info) => {
        selected = info?.ward_no ?? null;
        onSelect?.(info);
      },
      onStatus: (status, message, kind) => {
        if (status === "ready") {
          overlay.hidden = true;
          wardMap.setData(pending);
          if (selected !== null) wardMap.select(selected);
        } else if (status === "failed") {
          show(errorState({ message: t(kind === "stopped" ? "map.stopped" : "map.failed"), onRetry: start }));
          if (message) console.warn(`map: ${message}`);
        }
      },
      onNote: (note) => {
        if (note === "restarted") console.warn("map: restarted after a canvas problem");
      },
    });
    try {
      await wardMap.init();
    } catch {
      // onStatus("failed") already showed the message and Retry
    }
  }
  start();

  return {
    el,
    setData(data) {
      pending = data;
      wardMap?.setData(data);
    },
    select(no) {
      selected = no;
      wardMap?.select(no);
    },
    destroy() {
      wardMap?.destroy();
    },
  };
}
