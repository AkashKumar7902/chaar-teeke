// Status pills: an icon, a word and a tone. Colour alone never carries a state.
// Tones: neutral, primary (done or active), attention (amber), alarm (red: system faults only),
// estimate and simulated (dashed).

import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { icon } from "./icons.js";

export const TONES = ["neutral", "primary", "attention", "alarm", "estimate", "simulated"];

export function pill({ tone = "neutral", iconName = null, text, title = null, wrap = false }) {
  if (!TONES.includes(tone)) throw new Error(`pill: unknown tone "${tone}"`);
  return h(
    "span",
    { class: ["pill", tone === "neutral" ? "" : `pill--${tone}`, wrap ? "pill--wrap" : ""], title },
    iconName ? icon(iconName) : null,
    text,
  );
}

// domain -> value -> [tone, icon]. The words come from status.<domain>.<value> in common.*.json.
// Late and unanswered are amber, never red. Red appears only in the "system" domain.
const MAP = {
  dose: {
    due: ["neutral", "clock"],
    given: ["primary", "check"],
    late_minor: ["attention", "alert"],
    late_review: ["attention", "alert"],
    not_needed: ["neutral", "x"],
  },
  clinicRow: {
    due_today: ["neutral", "clock"],
    reminded: ["primary", "phone"],
    tried: ["neutral", "refresh"],
    patient_says_taken: ["primary", "check"],
    marked_given: ["primary", "check"],
    late_minor: ["attention", "alert"],
    late_review: ["attention", "alert"],
    awaiting_call: ["neutral", "hourglass"],
    no_answer: ["attention", "phone"],
  },
  call: {
    scheduled: ["neutral", "clock"],
    dialing: ["neutral", "phone"],
    ringing: ["neutral", "phone"],
    answered: ["primary", "phone"],
    completed: ["primary", "check"],
    no_answer: ["attention", "phone"],
    failed: ["attention", "alert"],
    cancelled: ["neutral", "x"],
  },
  alert: {
    new: ["attention", "alert"],
    claimed: ["primary", "user"],
    acted: ["primary", "check"],
    not_acted: ["neutral", "x"],
  },
  location: {
    pending: ["neutral", "hourglass"],
    matched: ["estimate", "map"],
    needs_review: ["attention", "list"],
    decided: ["primary", "check"],
    outside_jmc: ["neutral", "map"],
    cant_tell: ["neutral", "x"],
    no_place_heard: ["neutral", "x"],
    skipped: ["neutral", "x"],
  },
  confidence: { likely: ["estimate", "map"], possible: ["estimate", "map"], none: ["neutral", "x"] },
  system: { ok: ["primary", "check"], fault: ["alarm", "alert"] },
  source: { simulator: ["simulated", "phone"], demo: ["neutral", "info"], clinic: ["neutral", "check"] },
};

/** statusPill("dose", "late_minor") or statusPill("clinicRow", "tried", {count: 2}). */
export function statusPill(domain, value, vars) {
  const entry = MAP[domain]?.[value];
  if (!entry) throw new Error(`statusPill: unknown ${domain}/${value}`);
  const [tone, iconName] = entry;
  return pill({ tone, iconName, text: t(`status.${domain}.${value}`, vars) });
}

export const STATUS_DOMAINS = MAP;
