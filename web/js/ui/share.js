// The notched share, with everything that must sit beside it (SPEC s10): the interval, n and the
// could-not-see count. Too few answers shows no percentage. No answers shows "No calls yet", never 0.

import { append, h } from "../core/dom.js";
import { formatCount, formatPercent, formatRange, formatShare } from "../core/format.js";
import { t } from "../core/i18n.js";
import { icon } from "./icons.js";
import { pill } from "./pill.js";
import { infoButton } from "./popover.js";

function pctNumber(fraction) {
  if (typeof fraction !== "number" || fraction < 0 || fraction > 1) {
    throw new RangeError(`shareDisplay: expected a fraction between 0 and 1, got ${fraction}`);
  }
  return `${Math.round(fraction * 1000) / 10}%`;
}

function claimText(claim) {
  if (claim.labelKey) {
    return t(claim.labelKey, { lo: Math.round(claim.band[0] * 100), hi: Math.round(claim.band[1] * 100) });
  }
  if (typeof claim.label === "string") return claim.label;
  throw new Error("shareDisplay: claim needs labelKey (Coverage.claim_label_key) or a label");
}

function explain(share) {
  const lines = [h("p", null, t("share.explain"))];
  if (share.bound_low !== null && share.bound_low !== undefined && share.bound_high !== null && share.bound_high !== undefined) {
    lines.push(h("p", null, t("share.bounds", { range: formatRange(share.bound_low, share.bound_high) })));
  }
  lines.push(
    h(
      "p",
      { class: "muted" },
      t("share.counts", {
        notched: formatCount(share.notched),
        unnotched: formatCount(share.unnotched),
        unsure: formatCount(share.unsure),
      }),
    ),
  );
  return h("div", { class: "stack stack--s" }, lines);
}

/**
 * shareDisplay(share, {label, claim, compact, simulated, lastChecked})
 *  share: the Share model (n, notched, unnotched, unsure, status, share, ci_low, ci_high, bound_*, needed)
 *  claim: {band: [0.78, 0.8], labelKey} draws the reference band on the bar (city-wide share only). `labelKey` is
 *    Coverage.claim_label_key; the text is that key from the common bundle with the band filled in. `label`
 *    (a ready sentence) is still accepted for a caller that has built it already.
 *  lastChecked: text for the "No calls yet" state, e.g. "Last checked 10:12"
 */
export function shareDisplay(share, { label = t("share.label"), claim = null, compact = false, simulated = false, lastChecked = null } = {}) {
  const el = h("section", {
    class: ["share", compact ? "share--compact" : "", simulated ? "is-simulated" : ""],
    "data-status": share.status,
    "aria-label": label,
  });
  const head = h(
    "div",
    { class: "share__head" },
    h("h3", { class: "share__label" }, label),
    infoButton({ topic: label, title: label, content: explain(share) }),
    simulated ? pill({ tone: "simulated", iconName: "phone", text: t("status.source.simulator") }) : null,
  );
  const facts = h(
    "dl",
    { class: "share__facts" },
    h("div", null, h("dt", null, "n"), h("dd", null, formatCount(share.n))),
    h("div", null, h("dt", null, t("share.couldNotSee")), h("dd", null, formatCount(share.unsure))),
  );

  if (share.status === "no_data") {
    append(el, head, h("p", { class: "share__needed" }, t("states.noCalls")), lastChecked ? h("p", { class: "muted small" }, lastChecked) : null);
    return el;
  }
  if (share.status === "too_few") {
    append(el, 
      head,
      h("p", { class: "share__needed" }, formatShare(share)),
      h("p", { class: "share__note" }, icon("info"), t("share.tooFewNote")),
      facts,
    );
    return el;
  }
  if (share.status !== "ok" && share.status !== "bracketed") throw new Error(`shareDisplay: unknown status "${share.status}"`);
  if (share.share === null || share.ci_low === null || share.ci_high === null) {
    throw new Error("shareDisplay: an ok share needs share, ci_low and ci_high");
  }

  const bar = h(
    "div",
    {
      class: "bar",
      role: "img",
      "aria-label": t("share.barLabel", { share: formatPercent(share.share), range: formatRange(share.ci_low, share.ci_high) }),
      style: { "--lo": pctNumber(share.ci_low), "--hi": pctNumber(share.ci_high), "--pt": pctNumber(share.share) },
    },
    h("span", { class: "bar__track" }),
    claim ? h("span", { class: "bar__claim", style: { "--claim-lo": pctNumber(claim.band[0]), "--claim-hi": pctNumber(claim.band[1]) } }) : null,
    h("span", { class: "bar__ci" }),
    h("span", { class: "bar__point" }),
  );
  append(el, 
    head,
    h(
      "p",
      { class: "share__value" },
      h("span", { class: "share__pct" }, formatPercent(share.share)),
      h("span", { class: "share__ci" }, t("share.interval", { range: formatRange(share.ci_low, share.ci_high) })),
    ),
    bar,
    h("div", { class: "bar__scale", "aria-hidden": "true" }, h("span", null, "0"), h("span", null, "50"), h("span", null, "100")),
    facts,
    share.status === "bracketed" ? h("p", { class: "share__note" }, icon("alert"), t("share.bracketedNote")) : null,
    claim ? h("p", { class: "share__claim" }, claimText(claim)) : null,
  );
  return el;
}
