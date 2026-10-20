// Shared by the sterilisation-team pages (coverage, area, alerts, worked wards, sight count).
// Boot, one live data view with the cross-cutting states, and a few formatters.

import { api, getMe } from "/js/core/api.js";
import { bdi, h } from "/js/core/dom.js";
import { formatCount, formatDate, formatPercent, formatRange, formatShare, istDate } from "/js/core/format.js";
import { initI18n, t } from "/js/core/i18n.js";
import { mountFrame } from "/js/core/frame.js";
import { sharedStream } from "/js/core/sse.js";
import { errorFor, errorState, skeleton } from "/js/ui/index.js";

let repaintTitle = () => {};
/** Call after the page name changes (the area page names its area). */
export const refreshTitle = () => repaintTitle();

/** Load strings, mount the frame, return who is here. Every team page starts with this. */
export async function boot(bundles, titleKey) {
  // titleKey is a string key, or a function giving the page name (the area page names the area).
  await initI18n({ bundles: ["common", ...bundles], audience: "staff" });
  const { me } = await mountFrame({ mode: "staff" });
  const setTitle = () => {
    document.title = `${typeof titleKey === "function" ? titleKey() : t(titleKey)} · ${t("frame.brand")}`;
  };
  setTitle();
  document.addEventListener("ct:langchange", setTitle);
  repaintTitle = setTitle;
  return me;
}

export function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, ms);
  };
}

/** The role that sees the view (a visitor sees the view they chose). */
export function viewOf(me) {
  return me.role === "visitor" ? (me.acting_as ?? "abc") : me.role;
}

/** Team field records (worked wards, sight counts) are written by the ABC team and the vets. */
export function canRecord(me) {
  return me.can_write && (me.role === "abc" || me.role === "vet");
}

export function isVet(me) {
  return me.can_write && me.role === "vet";
}

/** "3 Aug to 19 Oct" (the coverage window). */
export function windowText(start, end) {
  return t("team.window", { from: formatDate(start), to: formatDate(end) });
}

/** A share on one line: percentage with interval and n, or n of minimum needed, or "No calls yet". Never a bare percentage. */
export function shareLine(share) {
  if (share.status === "ok" || share.status === "bracketed") {
    return t("team.shareLine", {
      pct: formatPercent(share.share),
      range: formatRange(share.ci_low, share.ci_high),
      n: formatCount(share.n),
    });
  }
  return formatShare(share);
}

/** "Could not see: 18" (not shown when there are no answers at all). */
export function unsureLine(share) {
  return share.status === "no_data" ? null : t("team.couldNotSee", { n: formatCount(share.unsure) });
}

/** Strip the audit prefix from "staff:vet". */
export function personName(actor) {
  return actor ? String(actor).replace(/^(staff|visitor):/, "") : actor;
}

/** Today's date on the server's clock: the demo clock in demo mode, else the browser's. */
export function todayTracker(me) {
  let now = new Date();
  let ready = Promise.resolve();
  if (me.mode === "demo") {
    ready = api
      .get("/api/demo/state")
      .then((d) => {
        now = new Date(d.now);
      })
      .catch((err) => console.warn(`today: ${err.message}`));
    sharedStream().on("clock", (ev) => {
      now = new Date(ev.data.now);
    });
  }
  return { ready, get: () => istDate(now), nowDate: () => now };
}

/**
 * One data region with every cross-cutting state: skeleton after 300 ms (CSS), error with Retry, offline
 * (no stale data: the region is replaced), 403, and reload on the shared stream's resync.
 *   const view = dataView(host, { load, render, isEmpty, events: ["alert"] })
 * `keepOnOffline` keeps an already drawn region (a picker holding what the person chose) when the connection drops.
 * `render(data)` returns the node to show. Return the same node every time to keep it (a map) across reloads.
 * Language changes repaint from the data already held, without a request.
 */
export function dataView(host, { load, render, isEmpty = null, events = [], why = null, lines = 4, block = false, keepOnOffline = false }) {
  let ticket = 0;
  let data = null;
  let offline = false;

  function paint() {
    const node = (isEmpty ? isEmpty(data) : null) ?? render(data);
    if (host.childNodes.length !== 1 || host.firstChild !== node) host.replaceChildren(node);
  }

  function showOffline() {
    // A form's own choices are not live data: when the region was already drawn it stays, and the page marks it "Not sent".
    if (keepOnOffline && data !== null) return;
    ticket += 1;
    data = null;
    host.replaceChildren(errorState({ message: t("states.offline"), onRetry: () => reload() }));
  }

  async function reload({ quiet = false } = {}) {
    if (offline) {
      showOffline();
      return;
    }
    const mine = (ticket += 1);
    if (!quiet || data === null) host.replaceChildren(skeleton({ lines, block }));
    host.setAttribute("aria-busy", "true");
    try {
      const next = await load();
      if (mine !== ticket) return;
      data = next;
      paint();
    } catch (err) {
      if (mine !== ticket) return;
      data = null;
      if (err?.name !== "ApiError") console.error(err);
      host.replaceChildren(errorFor(err, { onRetry: () => reload(), why }));
    } finally {
      if (mine === ticket) host.removeAttribute("aria-busy");
    }
  }

  const quietReload = debounce(() => reload({ quiet: true }), 600);
  const stream = sharedStream();
  for (const type of events) stream.on(type, quietReload);
  stream.onResync(() => reload({ quiet: true }));
  document.addEventListener("ct:offline", (event) => {
    offline = Boolean(event.detail?.offline);
    if (offline) showOffline();
    else reload({ quiet: true });
  });
  document.addEventListener("ct:langchange", () => {
    if (data !== null) paint();
  });
  reload();
  return {
    reload,
    quietReload,
    get data() {
      return data;
    },
    paint,
  };
}

/** Is the page offline right now (set by the frame)? */
export function isOffline() {
  return document.documentElement.hasAttribute("data-offline");
}

export { bdi, h };

/** Facts on one line, separated by a dot. Each fact stays whole when the line wraps ("Could not see 6" never splits). */
export function factLine(parts) {
  const out = [];
  for (const part of parts.filter(Boolean)) {
    if (out.length) out.push(" · ");
    out.push(h("span", { class: "nw" }, part));
  }
  return out;
}

/** Read-only roles see a form but cannot use it: every control inside is switched off. */
export function lockForm(root) {
  for (const el of root.querySelectorAll("input, select, textarea, button")) el.disabled = true;
}

/** Wards for pickers: GET /api/wards sorted by number. */
export async function loadWards() {
  const wards = await api.get("/api/wards");
  return wards.slice().sort((a, b) => a.ward_no - b.ward_no);
}

export const wardLabel = (w) => `${w.ward_no} ${w.ward_name ?? ""}`.trim();

// Text that a page builds once and keeps (a form with typed values): bound to a function so a language
// change rewrites it in place, and nothing typed is lost.
const binds = [];
document.addEventListener("ct:langchange", () => binds.forEach((run) => run()));

export function bindText(el, fn) {
  const run = () => {
    el.textContent = fn();
  };
  run();
  binds.push(run);
  return el;
}

export function bindAttr(el, name, fn) {
  const run = () => el.setAttribute(name, fn());
  run();
  binds.push(run);
  return el;
}

export function onLang(fn) {
  binds.push(fn);
}
