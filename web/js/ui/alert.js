// Sick-dog alert card. Amber, with an icon; the title is where it happened (the page above already says these
// are alerts, so the word is not repeated on every card, only spoken to screen readers). It carries ward or
// area, day, place type and the answer only: never a phone number. The red variant is for a real fault: the
// alert was not delivered.

import { h } from "../core/dom.js";
import { formatDate, formatDayTime } from "../core/format.js";
import { t } from "../core/i18n.js";
import { icon } from "./icons.js";
import { pill, statusPill } from "./pill.js";

/**
 * Where a report or alert happened, and whether that is a machine's estimate. The ward comes from matching
 * what the patient said to a place name, so it is an estimate until a reviewer decides it: bare text only for
 * location_status "decided", the "estimate" pill for anything else that names a place. No place at all is
 * plain "Place not known". `areaName` is the analysis area's name when the caller has it; otherwise the area id.
 * @param {{wardNo?: number|null, wardName?: string|null, clusterId?: string|null, areaName?: string|null, locationStatus?: string|null}} place
 * @returns {{text: string, estimate: boolean}}
 */
export function placeLabel({ wardNo = null, wardName = null, clusterId = null, areaName = null, locationStatus = null } = {}) {
  const ward = wardName && wardNo != null ? t("alert.ward", { name: wardName, no: wardNo }) : wardNo != null ? t("alert.wardNo", { no: wardNo }) : null;
  const area = clusterId ? (areaName ?? t("alert.area", { id: clusterId })) : null;
  const text = ward && wardName ? ward : [ward, area].filter(Boolean).join(", ");
  if (!text) return { text: t("alert.noPlace"), estimate: false };
  return { text, estimate: locationStatus !== "decided" };
}

/** The place as a node: plain text when decided, otherwise an "estimate" pill (dashed, with its word). */
export function placeChip(place, { iconName = "map" } = {}) {
  const { text, estimate } = placeLabel(place);
  return estimate ? pill({ tone: "estimate", iconName, text: `${text} · ${t("alert.estimate")}`, wrap: true }) : h("span", null, text);
}

/** Who has it, from the point of view of the person looking: "You are on it" or "Taken by <name>". */
function takenPill(alert) {
  const text = alert.claimed_by_me ? t("alert.takenByMe") : alert.claimed_by ? t("alert.takenBy", { name: alert.claimed_by }) : null;
  return text ? pill({ tone: "primary", iconName: "user", text }) : statusPill("alert", "claimed");
}

/**
 * alertCard(alert, {onClaim, onActed, onNotActed, onCallback, answer})
 *  alert: AlertOut (its `location_status` says whether the ward is still a machine's estimate; its `claimed_by_me` says whether the viewer took it; `claimed_by` is shown as the name,
 *  so a page may pass a display name there). Actions render only when the matching handler is given (the
 *  server still enforces roles).
 *  One filled button: "I'm on it" while the alert is new; later states show their status and no filled button.
 */
export function alertCard(alert, { onClaim, onActed, onNotActed, onCallback, answer = t("alert.answerSigns") } = {}) {
  const place = { wardNo: alert.ward_no, wardName: alert.ward_name, clusterId: alert.cluster_id, locationStatus: alert.location_status };
  const label = placeLabel(place);
  const where = label.estimate ? `${label.text} · ${t("alert.estimate")}` : label.text;
  const actions = [];
  if (alert.status === "new" && onClaim) {
    actions.push(h("button", { type: "button", class: "btn btn--primary", onClick: () => onClaim(alert) }, t("alert.claim")));
  }
  if (alert.status === "claimed") {
    if (onActed) actions.push(h("button", { type: "button", class: "btn btn--primary", onClick: () => onActed(alert) }, t("alert.acted")));
    if (onNotActed) actions.push(h("button", { type: "button", class: "btn", onClick: () => onNotActed(alert) }, t("alert.notActed")));
  }
  if (onCallback && (alert.status === "new" || alert.status === "claimed") && !alert.callback_status) {
    actions.push(h("button", { type: "button", class: "btn", onClick: () => onCallback(alert) }, t("alert.callback")));
  }
  const facts = [
    [t("alert.factPlace"), t(`placeType.${alert.place_type}`)],
    [t("alert.factAnswer"), answer],
  ];
  // While it is claimed the status pill already says who; once it is closed the name moves to a fact.
  if (alert.status !== "claimed" && alert.claimed_by) facts.push([t("alert.factClaimedBy"), alert.claimed_by_me ? t("alert.you") : alert.claimed_by]);
  if (alert.outcome_note) facts.push([t("alert.factNote"), alert.outcome_note]);
  if (alert.callback_status) facts.push([t("alert.factCallback"), t(`alert.callback_${alert.callback_status}`)]);

  return h(
    "article",
    { class: ["card", "card--attention", "alert-card", alert.simulated ? "is-simulated" : ""], "data-status": alert.status, "aria-label": `${t("alert.title")}: ${where}` },
    h(
      "div",
      { class: "alert-card__title" },
      icon("alert", { className: "icon--lg" }),
      placeChip(place),
      // the day it happened; the instant too for the visitor's own (simulated) alert, which is served with it. A real
      // alert is served with its day only, and a day is never shown as a time of day.
      h("span", { class: "alert-card__time" }, alert.made_at ? formatDayTime(alert.made_at) : formatDate(alert.local_day)),
    ),
    h(
      "div",
      { class: "row" },
      alert.status === "claimed" ? takenPill(alert) : statusPill("alert", alert.status),
      alert.simulated ? pill({ tone: "simulated", iconName: "phone", text: t("status.source.simulator") }) : null,
    ),
    h("dl", { class: "alert-card__facts" }, facts.map(([k, v]) => [h("dt", null, k), h("dd", null, v)])),
    actions.length ? h("div", { class: "alert-card__actions" }, actions) : null,
  );
}

/** The fault card: red, because the system failed to deliver. Used on /team/alerts when delivery fails. */
export function alertFaultCard({ message, onRetry }) {
  return h(
    "article",
    { class: "card card--alarm alert-card alert-card--alarm", role: "alert" },
    h("div", { class: "alert-card__title" }, icon("alert", { className: "icon--lg" }), message),
    onRetry ? h("div", { class: "alert-card__actions" }, h("button", { type: "button", class: "btn", onClick: onRetry }, icon("refresh"), t("buttons.retry"))) : null,
  );
}
