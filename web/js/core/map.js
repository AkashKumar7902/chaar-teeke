// Ward map on MapLibre GL JS 4.7.1 (pinned). Ward polygons come from /data/wards.geojson, fills and marks
// from data the page passes in. No external basemap by default (an optional OpenFreeMap one is behind
// basemap: "openfreemap", set from the server's CT_BASEMAP). Counts and shares only: never points.
//
// Recovery: resizes are debounced 120 ms; any running camera move is stopped before the
// resize; a canvas that died is rebuilt once where it was; an easing-frame crash is handled the same way.
//
// One resize path. MapLibre's own resize tracking (a window "resize" listener and a second ResizeObserver) is
// off (`trackResize: false`), because it resized at once, without the debounce or the stop-move rule, and
// even when nothing had changed: a window "resize" event (a full-page screenshot, a phone's address bar, a
// window moved between screens) made it reassign the canvas size, which clears the drawing buffer, and a
// frame taken before the repaint showed the map in the upper part of its box only. Now the container's
// ResizeObserver and the window event both go through the guard, and the guard resizes only when the canvas
// no longer matches its container (`canvasMatches`).

export const MAPLIBRE_VERSION = "4.7.1";
// MapLibre is vendored (web/vendor/maplibre-gl-4.7.1/, with its licence and checksums), so the page needs no
// outside script host and the content security policy allows 'self' only.
export const MAPLIBRE_JS = `/vendor/maplibre-gl-${MAPLIBRE_VERSION}/maplibre-gl.js`;
export const MAPLIBRE_CSS = `/vendor/maplibre-gl-${MAPLIBRE_VERSION}/maplibre-gl.css`;
export const OPENFREEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";
export const WARDS_URL = "/data/wards.geojson";
export const RESIZE_DEBOUNCE_MS = 120;
/** A rebuilt canvas that then draws for this long is healthy: the next death may be rebuilt again. */
export const HEALTHY_MS = 30000;
/** MapLibre 4.7 can die inside an easing frame; these messages mean "rebuild", not "show an error". */
export const MAP_CRASH = /_onEaseFrame|already running/;

/** Notched-share bins for the colour ramp: share < 0.40, < 0.55, < 0.70, < 0.80, else. */
export const RAMP_EDGES = [0.4, 0.55, 0.7, 0.8];
export const BITE_BOUNDS = [74.744, 32.647, 74.931, 32.806]; // wards.geojson extent, west south east north

/** Which colour step a share falls in: "r1" (lowest) to "r5" (highest). Pure. */
export function rampBin(share) {
  if (typeof share !== "number" || share < 0 || share > 1) throw new RangeError(`rampBin: expected 0 to 1, got ${share}`);
  const i = RAMP_EDGES.findIndex((edge) => share < edge);
  return `r${(i === -1 ? RAMP_EDGES.length : i) + 1}`;
}

/** The bin for one cluster: ramp step, "hatch" (too few answers) or "nodata". Pure. */
export function clusterBin(share) {
  switch (share?.status) {
    case "ok":
    case "bracketed":
      return rampBin(share.share);
    case "too_few":
      return "hatch";
    case "no_data":
    case undefined:
      return "nodata";
    default:
      throw new Error(`clusterBin: unknown share status "${share.status}"`);
  }
}

/**
 * Merge page data into ward properties. Pure.
 * clusters: ClusterCoverage[] (id, name, ward_nos, share, look_here, simulated_bites)
 * Wards in no cluster get "nodata". Simulated: wards of clusters with simulated bites plus `simulatedWards`.
 */
export function wardProperties(wardNos, clusters, { simulatedWards = [], selected = null } = {}) {
  const byWard = new Map();
  for (const c of clusters) {
    for (const no of c.ward_nos) {
      byWard.set(no, {
        cluster_id: c.id,
        cluster_name: c.name,
        bin: clusterBin(c.share),
        look: Boolean(c.look_here),
        sim: (c.simulated_bites ?? 0) > 0,
      });
    }
  }
  const sim = new Set(simulatedWards);
  return wardNos.map((no) => {
    const p = byWard.get(no) ?? { cluster_id: null, cluster_name: null, bin: "nodata", look: false, sim: false };
    return { ward_no: no, ...p, sim: p.sim || sim.has(no), selected: no === selected };
  });
}

/**
 * Is the map's canvas dead? Pure over plain values so it can be tested under node.
 *  canvas: the map canvas (or null); container: the element holding it; contextLost: gl.isContextLost()
 * A zero-size canvas is only dead when the container itself has a size.
 */
export function isCanvasDead({ canvas, container, contextLost = false }) {
  if (!canvas) return true;
  if (canvas.isConnected === false) return true;
  if (contextLost) return true;
  const containerHasSize = (container?.clientWidth ?? 0) > 0 && (container?.clientHeight ?? 0) > 0;
  if (containerHasSize && (canvas.width === 0 || canvas.height === 0)) return true;
  return false;
}

/**
 * Does the canvas already have the size its container asks for? Pure over plain values.
 *  canvas: the map canvas (its pixel size); container: the element MapLibre fills; pixelRatio: devicePixelRatio
 * MapLibre sizes the canvas to the container's client size times the pixel ratio (rounded down), so a pixel of
 * rounding is a match. A hidden container (no size) has nothing to match: whether its canvas is dead is
 * `isCanvasDead`'s question.
 */
export function canvasMatches({ canvas, container, pixelRatio = 1 }) {
  if (!canvas) return false;
  const w = container?.clientWidth ?? 0;
  const h = container?.clientHeight ?? 0;
  if (w === 0 || h === 0) return true;
  return Math.abs(canvas.width - w * pixelRatio) <= 1 && Math.abs(canvas.height - h * pixelRatio) <= 1;
}

/**
 * The resize guard, as a pure factory (timers and callbacks injected).
 * notify() is called on every container resize (and window resize); the work runs once the container has been
 * still for `debounceMs`: if `changed()` says the canvas no longer matches its container, stop a running move and
 * resize (a notify that changes nothing touches nothing: a resize clears the drawing buffer); if the canvas died
 * rebuild once (then give up and onFailed).
 * After a rebuild the canvas has `healthyMs` to prove itself: a second death inside that time gives up, one
 * after it is a new problem and is rebuilt again (otherwise a long-lived page could rebuild only once, ever).
 * crash(message) handles an easing-frame crash the same way.
 */
export function createResizeGuard({
  debounceMs = RESIZE_DEBOUNCE_MS,
  isMoving,
  stop,
  resize,
  changed = () => true,
  isDead,
  rebuild,
  onFailed,
  healthyMs = HEALTHY_MS,
  timers = { setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (id) => clearTimeout(id) },
}) {
  let timer = null;
  let healthyTimer = null;
  let rebuilt = false;

  function clearHealthy() {
    if (healthyTimer !== null) timers.clearTimeout(healthyTimer);
    healthyTimer = null;
  }
  function recover() {
    clearHealthy();
    if (rebuilt) {
      onFailed();
      return;
    }
    rebuilt = true;
    rebuild();
    healthyTimer = timers.setTimeout(() => {
      healthyTimer = null;
      if (!isDead()) rebuilt = false;
    }, healthyMs);
  }
  function run() {
    timer = null;
    if (changed()) {
      if (isMoving()) stop();
      resize();
    }
    if (isDead()) recover();
  }
  return {
    notify() {
      if (timer !== null) timers.clearTimeout(timer);
      timer = timers.setTimeout(run, debounceMs);
    },
    crash(message) {
      if (!MAP_CRASH.test(String(message))) return false;
      if (isMoving()) stop();
      recover();
      return true;
    },
    dispose() {
      if (timer !== null) timers.clearTimeout(timer);
      timer = null;
      clearHealthy();
    },
    get rebuilt() {
      return rebuilt;
    },
  };
}

// ------------------------------------------------------------------ loading
let libPromise = null;

/** The vendored MapLibre at the pinned version, from this server. Loaded once. */
export function loadMapLibre() {
  if (globalThis.maplibregl) return Promise.resolve(globalThis.maplibregl);
  if (libPromise) return libPromise;
  libPromise = new Promise((resolve, reject) => {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = MAPLIBRE_CSS;
    document.head.append(css);
    const script = document.createElement("script");
    script.src = MAPLIBRE_JS;
    script.async = true;
    script.onload = () => (globalThis.maplibregl ? resolve(globalThis.maplibregl) : reject(new Error("MapLibre loaded but is missing")));
    script.onerror = () => {
      libPromise = null;
      reject(new Error("MapLibre could not be loaded"));
    };
    document.head.append(script);
  });
  return libPromise;
}

let wardsPromise = null;
/** Ward polygons with only the properties the map needs (the file carries long source notes). */
export function loadWards(url = WARDS_URL) {
  if (!wardsPromise) {
    wardsPromise = fetch(url, { credentials: "same-origin" })
      .then((r) => {
        if (!r.ok) throw new Error(`Ward boundaries could not be loaded (${r.status})`);
        return r.json();
      })
      .then((g) => ({
        type: "FeatureCollection",
        features: g.features.map((f) => ({
          type: "Feature",
          geometry: f.geometry,
          properties: { ward_no: f.properties.ward_no, ward_name: f.properties.ward_name },
        })),
      }))
      .catch((err) => {
        wardsPromise = null;
        throw err;
      });
  }
  return wardsPromise;
}

function cssVar(el, name) {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

function hatchImage(color) {
  const size = 16;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  for (const o of [-size, 0, size]) {
    ctx.moveTo(o, size);
    ctx.lineTo(o + size, 0);
  }
  ctx.stroke();
  return ctx.getImageData(0, 0, size, size);
}

// ----------------------------------------------------------------- the map
export class WardMap {
  /**
   * @param {HTMLElement} container the .map element
   * @param {object} options
   *   labels: {zoomIn, zoomOut, attribution, tipWard} translated strings from the page
   *   basemap: "none" | "openfreemap"
   *   onSelect(info | null), onStatus("loading"|"ready"|"failed", message?, kind?), onNote(message)
   */
  constructor(container, { labels, basemap = "none", onSelect, onStatus, onNote } = {}) {
    this.container = container;
    this.labels = labels;
    this.basemap = basemap;
    this.onSelect = onSelect;
    this.onStatus = onStatus ?? (() => {});
    this.onNote = onNote ?? (() => {});
    this.map = null;
    this.lib = null;
    this.wards = null;
    this.data = { clusters: [], simulatedWards: [] };
    this.selected = null;
    this.loaded = false;
    this.canvasEl = document.createElement("div");
    this.canvasEl.className = "map__canvas";
    this.tip = document.createElement("div");
    this.tip.className = "map__tip";
    this.tip.hidden = true;
    container.append(this.canvasEl, this.tip);

    this.guard = createResizeGuard({
      isMoving: () => Boolean(this.map?.isMoving?.()),
      stop: () => this.map.stop(),
      resize: () => this.map?.resize(),
      changed: () => this.canvasChanged(),
      isDead: () => this.dead(),
      rebuild: () => this.rebuild(),
      onFailed: () => this.fail("The map stopped drawing.", "stopped"),
    });
    this.observer = new ResizeObserver(() => this.guard.notify());
    this.observer.observe(container);
    // MapLibre's own window listener is off (trackResize: false); the guard decides whether anything changed
    this.onWindowResize = () => this.guard.notify();
    window.addEventListener("resize", this.onWindowResize);
    this.onWindowError = (event) => {
      if (this.map && this.guard.crash(event.message ?? "")) event.preventDefault();
    };
    window.addEventListener("error", this.onWindowError);
    this.themeObserver = new MutationObserver(() => this.applyTheme());
    this.themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    this.scheme = window.matchMedia("(prefers-color-scheme: dark)");
    this.onScheme = () => this.applyTheme();
    this.scheme.addEventListener("change", this.onScheme);
  }

  async init() {
    this.onStatus("loading");
    try {
      [this.lib, this.wards] = await Promise.all([loadMapLibre(), loadWards()]);
    } catch (err) {
      this.fail(err.message);
      throw err;
    }
    try {
      this.build({ center: null, zoom: null });
    } catch (err) {
      this.fail(err.message || "The map could not start.");
      throw err;
    }
    return this;
  }

  /** True when the canvas is not the size its container asks for (or there is no canvas yet to measure). */
  canvasChanged() {
    if (!this.map) return false; // not built yet (or being rebuilt): the build reads the container's size itself
    return !canvasMatches({ canvas: this.map.getCanvas?.() ?? null, container: this.canvasEl, pixelRatio: window.devicePixelRatio || 1 });
  }

  dead() {
    if (!this.map) return false; // not built yet (or being rebuilt): nothing to measure
    const canvas = this.map.getCanvas?.() ?? null;
    let contextLost = false;
    if (canvas) {
      const gl = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
      contextLost = Boolean(gl?.isContextLost?.());
    }
    return isCanvasDead({ canvas, container: this.container, contextLost });
  }

  /** `kind` is "stopped" when a map that was drawing quit (the page says "stopped drawing"), else it never loaded. */
  fail(message, kind = "load") {
    this.onStatus("failed", message, kind);
  }

  build({ center, zoom }) {
    const lib = this.lib;
    const style =
      this.basemap === "openfreemap"
        ? OPENFREEMAP_STYLE
        : { version: 8, sources: {}, layers: [{ id: "land", type: "background", paint: { "background-color": cssVar(this.container, "--map-bg") } }] };
    const map = new lib.Map({
      container: this.canvasEl,
      style,
      bounds: BITE_BOUNDS,
      fitBoundsOptions: { padding: 16, animate: false },
      minZoom: 8,
      maxBounds: [
        [BITE_BOUNDS[0] - 0.25, BITE_BOUNDS[1] - 0.2],
        [BITE_BOUNDS[2] + 0.25, BITE_BOUNDS[3] + 0.2],
      ],
      dragRotate: false,
      pitchWithRotate: false,
      touchPitch: false,
      attributionControl: false,
      trackResize: false, // the guard resizes, once and debounced; see the note at the top
      locale: { "NavigationControl.ZoomIn": this.labels.zoomIn, "NavigationControl.ZoomOut": this.labels.zoomOut },
    });
    if (center) {
      map.jumpTo({ center, zoom });
    }
    map.touchZoomRotate.disableRotation();
    map.addControl(new lib.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new lib.AttributionControl({ compact: false, customAttribution: this.labels.attribution }), "bottom-left");
    map.on("error", (event) => {
      const message = event?.error?.message ?? String(event?.message ?? "");
      if (this.guard.crash(message)) return;
      if (!this.loaded) this.fail(message || "The map could not start.");
      else this.onNote(message);
    });
    map.on("load", () => this.addLayers(map));
    map.on("click", "wards-fill", (event) => this.pick(event.features?.[0]?.properties));
    map.on("click", (event) => {
      if (!map.queryRenderedFeatures(event.point, { layers: ["wards-fill"] }).length) this.pick(null);
    });
    map.on("mousemove", "wards-fill", (event) => this.showTip(event));
    map.on("mouseleave", "wards-fill", () => {
      this.tip.hidden = true;
      map.getCanvas().style.cursor = "";
    });
    this.map = map;
  }

  addLayers(map) {
    const c = this.colors();
    map.addSource("wards", { type: "geojson", data: this.featureCollection(), promoteId: "ward_no" });
    if (!map.hasImage("hatch")) map.addImage("hatch", hatchImage(c.hatch), { pixelRatio: 2 });
    const fillColor = ["match", ["get", "bin"], "r1", c.r1, "r2", c.r2, "r3", c.r3, "r4", c.r4, "r5", c.r5, c.nodata];
    map.addLayer({ id: "wards-fill", type: "fill", source: "wards", paint: { "fill-color": fillColor, "fill-opacity": 0.95 } });
    map.addLayer({ id: "wards-hatch", type: "fill", source: "wards", filter: ["==", ["get", "bin"], "hatch"], paint: { "fill-pattern": "hatch" } });
    map.addLayer({ id: "wards-line", type: "line", source: "wards", paint: { "line-color": c.line, "line-width": 0.8 } });
    map.addLayer({ id: "wards-look", type: "line", source: "wards", filter: ["==", ["get", "look"], true], paint: { "line-color": c.look, "line-width": 3 } });
    map.addLayer({ id: "wards-sim", type: "line", source: "wards", filter: ["==", ["get", "sim"], true], paint: { "line-color": c.ink, "line-width": 2.5, "line-dasharray": [2, 2] } });
    map.addLayer({ id: "wards-selected", type: "line", source: "wards", filter: ["==", ["get", "selected"], true], paint: { "line-color": c.ink, "line-width": 4 } });
    this.loaded = true;
    this.onStatus("ready");
  }

  colors() {
    const v = (name) => cssVar(this.container, name);
    return {
      bg: v("--map-bg"),
      nodata: v("--map-nodata"),
      line: v("--map-ward-line"),
      hatch: v("--map-hatch"),
      r1: v("--ramp-1"),
      r2: v("--ramp-2"),
      r3: v("--ramp-3"),
      r4: v("--ramp-4"),
      r5: v("--ramp-5"),
      look: v("--attention-line"),
      ink: v("--ink"),
    };
  }

  /** Re-read the theme colours (light, dark, or the data-theme override). */
  applyTheme() {
    const map = this.map;
    if (!map || !this.loaded) return;
    const c = this.colors();
    if (map.getLayer("land")) map.setPaintProperty("land", "background-color", c.bg);
    map.setPaintProperty("wards-fill", "fill-color", ["match", ["get", "bin"], "r1", c.r1, "r2", c.r2, "r3", c.r3, "r4", c.r4, "r5", c.r5, c.nodata]);
    map.setPaintProperty("wards-line", "line-color", c.line);
    map.setPaintProperty("wards-look", "line-color", c.look);
    map.setPaintProperty("wards-sim", "line-color", c.ink);
    map.setPaintProperty("wards-selected", "line-color", c.ink);
    if (map.hasImage("hatch")) map.removeImage("hatch");
    map.addImage("hatch", hatchImage(c.hatch), { pixelRatio: 2 });
  }

  featureCollection() {
    const props = wardProperties(
      this.wards.features.map((f) => f.properties.ward_no),
      this.data.clusters,
      { simulatedWards: this.data.simulatedWards, selected: this.selected },
    );
    const byNo = new Map(props.map((p) => [p.ward_no, p]));
    return {
      type: "FeatureCollection",
      features: this.wards.features.map((f) => ({
        type: "Feature",
        geometry: f.geometry,
        properties: { ...f.properties, ...byNo.get(f.properties.ward_no) },
      })),
    };
  }

  /** Pass cluster coverage (ClusterCoverage[]) and optional extra simulated wards. Safe before the map loads. */
  setData({ clusters, simulatedWards = [] }) {
    this.data = { clusters, simulatedWards };
    this.refresh();
  }

  refresh() {
    const source = this.map?.getSource("wards");
    if (source && this.loaded) source.setData(this.featureCollection());
  }

  select(wardNo) {
    this.selected = wardNo;
    this.refresh();
  }

  pick(properties) {
    this.selected = properties ? properties.ward_no : null;
    this.refresh();
    this.onSelect?.(properties ? { ward_no: properties.ward_no, ward_name: properties.ward_name, cluster_id: properties.cluster_id ?? null } : null);
  }

  showTip(event) {
    const p = event.features?.[0]?.properties;
    if (!p) return;
    this.map.getCanvas().style.cursor = "pointer";
    this.tip.textContent = `${this.labels.tipWard} ${p.ward_no} ${p.ward_name ?? ""}`.trim();
    this.tip.hidden = false;
    this.tip.style.setProperty("--tip-x", `${event.point.x}px`);
    this.tip.style.setProperty("--tip-y", `${event.point.y}px`);
  }

  /** Rebuild a dead canvas once, in the same place, with the same data. */
  rebuild() {
    const old = this.map;
    const center = old?.getCenter?.();
    const zoom = old?.getZoom?.();
    this.loaded = false;
    try {
      old?.remove();
    } catch (err) {
      this.onNote(err.message);
    }
    this.canvasEl.replaceChildren();
    this.map = null;
    try {
      this.build({ center: center ? [center.lng, center.lat] : null, zoom });
    } catch (err) {
      this.fail(err.message || "The map stopped drawing.", "stopped");
      return;
    }
    this.onNote("restarted");
  }

  destroy() {
    this.guard.dispose();
    this.observer.disconnect();
    this.themeObserver.disconnect();
    this.scheme.removeEventListener("change", this.onScheme);
    window.removeEventListener("error", this.onWindowError);
    window.removeEventListener("resize", this.onWindowResize);
    this.map?.remove();
    this.map = null;
    this.canvasEl.remove();
    this.tip.remove();
  }
}

/** createWardMap(el, {labels, basemap, onSelect, onStatus}) -> Promise<WardMap> (ready to setData). */
export async function createWardMap(container, options) {
  const wardMap = new WardMap(container, options);
  await wardMap.init();
  return wardMap;
}
