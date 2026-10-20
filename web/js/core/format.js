// Dates, times and numbers. All clock times are IST (Asia/Kolkata); Western digits in every language.
// Shares follow SPEC s10: a percentage is never shown without its interval, n and the could-not-see count.

import { currentLang, t } from "./i18n.js";

export const TZ = "Asia/Kolkata";
const LOCALES = { en: "en-IN-u-nu-latn", hi: "hi-IN-u-nu-latn", doi: "hi-IN-u-nu-latn", ur: "ur-IN-u-nu-latn" };

function locale(lang) {
  return LOCALES[lang ?? currentLang()] ?? LOCALES.en;
}

const BARE_DAY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * True for a bare local day ("2026-10-19"), which names a day and no moment. The server serves some things
 * (an alert's `created_at`, `local_day`) as a day only; read as a moment it would be midnight UTC, and show
 * as 05:30 in IST. A day is therefore never given a time of day: see formatTime.
 */
export function isDay(value) {
  return typeof value === "string" && BARE_DAY.test(value);
}

function toDate(value) {
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) throw new RangeError(`format: not a date: ${String(value)}`);
  return d;
}

function parts(value, options, lang) {
  const out = {};
  for (const p of new Intl.DateTimeFormat(locale(lang), { timeZone: TZ, ...options }).formatToParts(toDate(value))) {
    out[p.type] = p.value;
  }
  return out;
}

/** "YYYY-MM-DD" of the IST calendar day. */
export function istDate(value) {
  if (isDay(value)) return value;
  const p = parts(value, { year: "numeric", month: "2-digit", day: "2-digit" }, "en");
  return `${p.year}-${p.month}-${p.day}`;
}

/** "10:40" (24 hour, IST). A bare day has no time of day, so it is refused rather than shown as 05:30. */
export function formatTime(value, lang) {
  if (isDay(value)) throw new RangeError(`format: a day has no time of day: ${value}`);
  const p = parts(value, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }, lang);
  return `${p.hour}:${p.minute}`;
}

function dateText(value, lang, weekday) {
  const d = isDay(value) ? new Date(`${value}T12:00:00+05:30`) : toDate(value);
  return new Intl.DateTimeFormat(locale(lang), {
    timeZone: TZ,
    ...(weekday ? { weekday: "short" } : {}),
    day: "numeric",
    month: "short",
  })
    .format(d)
    .replace(/,/g, "")
    .replace(/\bSept\b/, "Sep"); // en-IN spells September "Sept"
}

/** "Mon 19 Oct". A bare "YYYY-MM-DD" is a local date and is not shifted. */
export function formatDate(value, lang) {
  return dateText(value, lang, true);
}

/** "Mon 19 Oct, 10:40". A bare day is the day alone, "Mon 19 Oct": its time is not known. */
export function formatDateTime(value, lang) {
  return isDay(value) ? formatDate(value, lang) : `${formatDate(value, lang)}, ${formatTime(value, lang)}`;
}

/** "19 Oct, 10:40": the day without its weekday, for a phone-width strip. A bare day is "19 Oct". */
export function formatDayTime(value, lang) {
  return isDay(value) ? dateText(value, lang, false) : `${dateText(value, lang, false)}, ${formatTime(value, lang)}`;
}

/** "since 10:40" today, "since Sun 18 Oct, 10:40" on another day, "since Mon 19 Oct" from a bare day. `now` is the demo clock. */
export function sinceText(value, now, lang) {
  const when = !isDay(value) && istDate(value) === istDate(now) ? formatTime(value, lang) : formatDateTime(value, lang);
  return t("format.since", { when });
}

/** "5 min ago", "2 h ago", "yesterday". */
export function agoText(value, now, lang) {
  const seconds = Math.round((toDate(now).getTime() - toDate(value).getTime()) / 1000);
  const rtf = new Intl.RelativeTimeFormat(locale(lang), { numeric: "auto", style: "short" });
  const abs = Math.abs(seconds);
  const sign = seconds >= 0 ? -1 : 1;
  if (abs < 60) return rtf.format(0, "second");
  if (abs < 3600) return rtf.format(sign * Math.round(abs / 60), "minute");
  if (abs < 86400) return rtf.format(sign * Math.round(abs / 3600), "hour");
  return rtf.format(sign * Math.round(abs / 86400), "day");
}

/** The server's word for a cell it withholds so that n cannot be subtracted back (stats/intervals.py WITHHELD). */
const WITHHELD = "hidden";

/** A count. "<5" passes through untouched, "hidden" is translated; whole numbers get Indian grouping. */
export function formatCount(value, lang) {
  if (value === WITHHELD) return t("format.hidden");
  if (typeof value === "string") return value;
  if (!Number.isInteger(value)) throw new TypeError(`format: count must be a whole number or a string, got ${value}`);
  return new Intl.NumberFormat(locale(lang)).format(value);
}

function pct(fraction) {
  if (typeof fraction !== "number" || Number.isNaN(fraction) || fraction < 0 || fraction > 1) {
    throw new RangeError(`format: a share is a fraction between 0 and 1, got ${fraction}`);
  }
  return Math.round(fraction * 100);
}

/** "62%" from 0.62. Refuses values that are not fractions, so a percent-versus-fraction slip is loud. */
export function formatPercent(fraction) {
  return `${pct(fraction)}%`;
}

/** "54 to 70" (no percent signs; the % is on the headline). */
export function formatRange(low, high) {
  return t("format.range", { low: pct(low), high: pct(high) });
}

/**
 * The share line. Ok or bracketed: "62% (54 to 70)" and, with `withN`, ", n = 112".
 * Too few answers: no percentage at all, only "n = 11 of 25 needed".
 * No data: the words "No calls yet".
 * `needed` on the contract is the number still needed, so the minimum is n + needed.
 */
export function formatShare(share, { withN = false } = {}) {
  switch (share.status) {
    case "ok":
    case "bracketed": {
      if (share.share === null || share.ci_low === null || share.ci_high === null) {
        throw new Error("format: a share with status ok needs share, ci_low and ci_high");
      }
      const line = `${formatPercent(share.share)} (${formatRange(share.ci_low, share.ci_high)})`;
      return withN ? `${line}, ${t("share.n", { n: formatCount(share.n) })}` : line;
    }
    case "too_few":
      if (!Number.isInteger(share.needed) || share.needed < 1) {
        throw new Error("format: a too_few share needs `needed` (how many more answers), not a guess");
      }
      return t("share.tooFew", { n: formatCount(share.n), min: formatCount(share.n + share.needed) });
    case "no_data":
      return t("states.noCalls");
    default:
      throw new Error(`format: unknown share status "${share.status}"`);
  }
}

/**
 * The reference-band label in the reader's language: the server sends a stable key (`claim_label_key`, a
 * key in the common bundle that takes {lo} and {hi}), never a sentence to translate by pattern. Takes the
 * Coverage model or anything with `claim_band` and `claim_label_key`.
 */
export function claimLabel(coverage) {
  if (!coverage.claim_label_key) throw new Error("format: coverage has no claim_label_key");
  const [lo, hi] = coverage.claim_band;
  return t(coverage.claim_label_key, { lo: pct(lo), hi: pct(hi) });
}

/**
 * Why an area is flagged "look here", in the reader's language, from the numbers the server sends
 * (`look_here_reason_data`). Null when the area is not flagged. The English sentence in `look_here_reason`
 * is for API readers; pages show this.
 */
export function lookHereReason(cluster) {
  const data = cluster.look_here_reason_data;
  if (!data) return null;
  switch (data.kind) {
    case "strong":
      return t("lookHere.strong", { n: formatCount(data.n) });
    default:
      throw new Error(`format: unknown look-here reason "${data.kind}"`);
  }
}

/** "•••• 4821"; a purged number says so instead of showing blanks. */
export function maskedPhone(last4) {
  return last4 ? t("format.phoneMasked", { last4 }) : t("format.phonePurged");
}

/** "Dose 3". */
export function doseLabel(n) {
  return t("format.dose", { n });
}
