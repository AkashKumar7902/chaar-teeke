// Shared by the three clinic pages (Today, Enrol, Patient). Nothing here talks to the screen on its own.

import { ApiError, api, getMe } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { has, t } from "/js/core/i18n.js";
import { sharedStream } from "/js/core/sse.js";
import { errorFor, icon, skeleton, toast } from "/js/ui/index.js";

/** "Today · Chaar Teeke": the page name from its bundle, then the brand. */
export function pageTitle(key) {
  return `${t(key)} · ${t("frame.brand")}`;
}

/** ?clinic_id= from the address, or undefined (the server then uses the login's own clinic). */
export function clinicParam() {
  return new URLSearchParams(location.search).get("clinic_id") || undefined;
}

/**
 * Where the person was on Today (the tile, the scroll), kept for this tab only, so coming back from a patient
 * puts them where they were. A convenience: a tab that cannot store anything just starts at the top.
 */
const VIEW_KEY = "ct.clinic.view";
export function rememberView(view) {
  try {
    sessionStorage.setItem(VIEW_KEY, JSON.stringify(view));
  } catch {
    // nothing is remembered; the page starts at the top
  }
}
export function recalledView() {
  try {
    return JSON.parse(sessionStorage.getItem(VIEW_KEY) ?? "null");
  } catch {
    return null;
  }
}

/** The address of a clinic page, keeping a chosen demo clinic. */
export function withClinic(path, clinicId) {
  return clinicId ? `${path}?clinic_id=${encodeURIComponent(clinicId)}` : path;
}

/** Attributes for a button that writes. A visitor's button looks disabled but still asks the server, which answers 403. */
export function writeAttrs(me) {
  return me.can_write ? {} : { "aria-disabled": "true", title: t("clinic.readOnlyTitle") };
}

/** "Now" on the server's clock: the demo clock in demo mode, real time otherwise. */
export async function serverNow(me) {
  if (me.mode !== "demo") return new Date().toISOString();
  return (await api.get("/api/demo/state")).now;
}

export function langName(code) {
  return t(`clinic.lang.${code}`);
}

/** A schedule's name in the page language. The server's own name is shown for an id this page has no words for. */
export function regName(id, serverName) {
  return has(`clinic.regimen.${id}`) ? t(`clinic.regimen.${id}`) : serverName;
}

export function daysLine(days) {
  return t("clinic.days", { list: days.join(", ") });
}

/** GET /api/clinic/config (contract request): enabled schedules, defaults, the missed-call number, the clinics a visitor may switch to. */
export function loadConfig(clinicId) {
  return api.get("/api/clinic/config", { clinic_id: clinicId });
}

/**
 * Run a write from a button. The button shows busy and cannot be pressed twice. A read-only refusal is
 * already toasted by the frame; any other refusal goes to `onError` (default: an amber toast).
 * Resolves with the result, or undefined when the server refused.
 */
export async function runWrite(button, fn, { onError } = {}) {
  if (button?.getAttribute("aria-busy") === "true") return undefined;
  button?.setAttribute("aria-busy", "true");
  try {
    return await fn();
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    // With an `onError` (a dialog, where the frame's toast would sit behind it) even a read-only refusal is shown in place.
    if (onError) onError(err);
    else if (!err.isReadOnly) toast(err.message, { tone: "attention" });
    return undefined;
  } finally {
    button?.removeAttribute("aria-busy");
  }
}

/** Keep keyboard focus on the same control (data-key) across a re-render. */
export function keepFocus(root, rebuild) {
  const key = root.contains(document.activeElement) ? document.activeElement.dataset?.key : null;
  rebuild();
  if (key) root.querySelector(`[data-key="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
}

/**
 * A region that loads once with a skeleton (after 300 ms), then updates in place when the server says
 * something changed, so typed text, scroll and focus stay where they are. A failed load shows the error
 * state with one Retry and no old data.
 *   const live = liveRegion(host, { load, build, update }); live.reload({quiet: true})
 * `build(data)` returns the shell node once; `update(data)` fills it; `drop()` forgets it (language change).
 */
export function liveRegion(host, { load, build, update }) {
  let ticket = 0;
  let built = false;
  async function reload({ quiet = false } = {}) {
    const mine = (ticket += 1);
    if (!quiet || !built) host.replaceChildren(skeleton({ lines: 5, block: true }));
    host.setAttribute("aria-busy", "true");
    try {
      const data = await load();
      if (mine !== ticket) return;
      if (!built) {
        host.replaceChildren(build(data));
        built = true;
      }
      update(data);
    } catch (err) {
      if (mine !== ticket) return;
      if (!(err instanceof ApiError)) console.error(err);
      built = false;
      host.replaceChildren(errorFor(err, { onRetry: () => reload() }));
    } finally {
      if (mine === ticket) host.removeAttribute("aria-busy");
    }
  }
  return {
    reload,
    /** Build again from the same data on the next update (after a language change). */
    drop() {
      built = false;
    },
  };
}

/**
 * Call `onChange` (debounced) when the stream says a clinic list may have changed, and after a reconnect.
 * While the server cannot be reached the page is told through `onOffline(true|false)` and shows no old data.
 */
export function watchClinic(onChange, onOffline) {
  const stream = sharedStream();
  let timer = null;
  const nudge = () => {
    clearTimeout(timer);
    timer = setTimeout(() => onChange(), 350);
  };
  for (const type of ["call", "missed_call", "dose", "course", "callback"]) stream.on(type, nudge);
  stream.onResync(() => onChange());
  document.addEventListener("ct:offline", (event) => onOffline?.(event.detail.offline));
  return stream;
}

/** A small amber line in a form or dialog (a server refusal, in the server's words). */
export function inlineError(message) {
  return h("p", { class: "inline-error", role: "alert" }, icon("alert"), h("span", null, message));
}
