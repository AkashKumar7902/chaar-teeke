// Toasts: short, polite, in a live region. One at a time: a new toast replaces the one on screen, so they never
// stack. Errors and "Not sent" stay until closed (or until the next toast); plain notes fade after 3 s.

import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { icon } from "./icons.js";

export const TOAST_MS = 3000;

let region = null;
let current = null; // {node, timer} of the toast on screen

function ensureRegion() {
  if (region && document.body.contains(region)) return region;
  region = h("div", { class: "toast-region", role: "region", "aria-label": t("states.notifications") });
  const live = h("div", { "aria-live": "polite", "aria-atomic": "false", class: "toast-live" });
  region.append(live);
  document.body.append(region);
  region.live = live;
  return region;
}

function clearCurrent() {
  if (!current) return;
  if (current.timer) clearTimeout(current.timer);
  current.node.remove();
  current = null;
}

/** toast("Saved") or toast(message, {tone: "attention", sticky: true}). Replaces any toast on screen. Returns {dismiss}. */
export function toast(message, { tone = "neutral", sticky = tone !== "neutral", ms = TOAST_MS } = {}) {
  const live = ensureRegion().live;
  const node = h(
    "div",
    { class: ["toast", tone === "neutral" ? "" : `toast--${tone}`], role: tone === "alarm" ? "alert" : "status" },
    tone !== "neutral" ? icon("alert") : null,
    h("span", { class: "toast__text" }, message),
    h(
      "button",
      { type: "button", class: "btn btn--quiet btn--icon", "aria-label": t("buttons.close"), onClick: () => dismiss() },
      icon("x"),
    ),
  );
  clearCurrent();
  live.append(node);
  const mine = { node, timer: null };
  current = mine;
  function dismiss() {
    if (mine.timer) clearTimeout(mine.timer);
    node.remove();
    if (current === mine) current = null;
  }
  if (!sticky) mine.timer = setTimeout(dismiss, ms);
  return { dismiss };
}
