// Cross-cutting states, defined once (UX.md section 6): empty, loading, error, offline, forbidden.
// Empty says what is missing and when it was last checked. It never shows 0 for an unseen count.

import { ApiError } from "../core/api.js";
import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { icon } from "./icons.js";

/** emptyState({title, body, action}) - action is a Node (a button or link), optional. */
export function emptyState({ title, body = null, action = null, tone = "neutral", iconName = "inbox" }) {
  return h(
    "div",
    { class: ["empty", tone === "neutral" ? "" : `empty--${tone}`], role: tone === "neutral" ? null : "status" },
    h("span", { class: "empty__icon" }, icon(iconName, { className: "icon--lg" })),
    h("p", { class: "empty__title" }, title),
    body ? h("p", { class: "empty__body" }, body) : null,
    action ? h("div", { class: "empty__actions" }, action) : null,
  );
}

/** Grey placeholder lines; invisible for the first 300 ms (pure CSS), so a fast answer never flashes. */
export function skeleton({ lines = 3, block = false } = {}) {
  const nodes = [];
  if (block) nodes.push(h("span", { class: "skeleton__block" }));
  for (let i = 0; i < lines; i += 1) nodes.push(h("span", { class: "skeleton__line" }));
  return h("div", { class: "skeleton", "aria-hidden": "true" }, nodes);
}

/** Amber error in plain words, where it failed, one Retry. `why` is an optional link to the system page. */
export function errorState({ message, onRetry, why = null, tone = "attention" }) {
  const actions = [];
  if (onRetry) {
    actions.push(
      h("button", { type: "button", class: "btn", onClick: onRetry }, icon("refresh"), t("buttons.retry")),
    );
  }
  if (why) actions.push(h("a", { class: "btn btn--quiet", href: why }, t("states.why")));
  return emptyState({
    title: message,
    tone,
    iconName: tone === "alarm" ? "alert" : "offline",
    action: actions.length ? actions : null,
  });
}

/** The right state for an ApiError: offline, forbidden, or the server's own words. */
export function errorFor(error, { onRetry, why = null } = {}) {
  if (error instanceof ApiError && error.code === "network") {
    return errorState({ message: t("states.offline"), onRetry });
  }
  if (error instanceof ApiError && error.isForbidden) {
    return emptyState({ title: t("states.forbidden"), iconName: "info" });
  }
  const message = error instanceof ApiError ? error.message : t("errors.generic");
  return errorState({ message, onRetry, why });
}

/** A strip under the header: offline, a system fault, or a plain note. Tone alarm is for faults only. */
export function banner({ text, tone = "attention", action = null }) {
  return h(
    "div",
    { class: ["banner", tone === "attention" ? "" : `banner--${tone}`], role: tone === "alarm" ? "alert" : "status" },
    icon(tone === "neutral" ? "info" : "alert"),
    h("span", { class: "grow" }, text),
    action,
  );
}

/**
 * Load, then render. Shows a skeleton (after 300 ms), then `render(data)` or an error with Retry.
 * `isEmpty(data)` -> an emptyState node (or null to render normally).
 *   const view = loadInto(el, () => api.get("/api/clinic/today"), (d) => node, { isEmpty })
 * Returns {reload}. Call reload() after SSE resync. A newer load always wins over an older one.
 */
export function loadInto(el, loader, render, { isEmpty = null, why = null, lines = 4, block = false } = {}) {
  let ticket = 0;
  async function reload({ quiet = false } = {}) {
    const mine = (ticket += 1);
    if (!quiet) el.replaceChildren(skeleton({ lines, block }));
    el.setAttribute("aria-busy", "true");
    try {
      const data = await loader();
      if (mine !== ticket) return;
      const empty = isEmpty ? isEmpty(data) : null;
      const view = empty ?? render(data);
      if (!view) throw new TypeError("loadInto: render() must return a node");
      el.replaceChildren(view);
    } catch (err) {
      if (mine !== ticket) return;
      if (!(err instanceof ApiError)) console.error(err);
      el.replaceChildren(errorFor(err, { onRetry: () => reload(), why }));
    } finally {
      if (mine === ticket) el.removeAttribute("aria-busy");
    }
  }
  reload();
  return { reload };
}
