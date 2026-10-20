import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

import {
  agoText,
  claimLabel,
  doseLabel,
  formatCount,
  formatDate,
  formatDateTime,
  formatDayTime,
  formatPercent,
  formatRange,
  formatShare,
  formatTime,
  isDay,
  istDate,
  lookHereReason,
  maskedPhone,
  sinceText,
} from "./format.js";
import { flatten, installBundle, resetBundles, t } from "./i18n.js";

const bundle = (lang) => JSON.parse(readFileSync(new URL(`../../i18n/common.${lang}.json`, import.meta.url), "utf8"));

test.beforeEach(() => {
  resetBundles();
  installBundle("common", "en", bundle("en"));
});

test("times are IST, 24 hour, Western digits", () => {
  assert.equal(formatTime("2026-10-19T05:10:00Z"), "10:40");
  assert.equal(formatTime("2026-10-19T18:30:00Z"), "00:00");
  assert.equal(formatTime("2026-10-19T00:00:00Z"), "05:30");
});

test("dates read 'Mon 19 Oct'; a bare local date is not shifted by the time zone", () => {
  assert.equal(formatDate("2026-10-19T05:10:00Z"), "Mon 19 Oct");
  assert.equal(formatDate("2026-10-19"), "Mon 19 Oct");
  assert.equal(formatDate("2026-09-10"), "Thu 10 Sep", "en-IN would say Sept");
  assert.equal(formatDateTime("2026-10-19T05:10:00Z"), "Mon 19 Oct, 10:40");
  assert.equal(formatDayTime("2026-10-19T05:10:00Z"), "19 Oct, 10:40");
  assert.equal(istDate("2026-10-18T19:00:00Z"), "2026-10-19");
});

test("'since' uses the time today and the date on another day", () => {
  const now = "2026-10-19T06:00:00Z";
  assert.equal(sinceText("2026-10-19T05:10:00Z", now), "since 10:40");
  assert.equal(sinceText("2026-10-18T05:10:00Z", now), "since Sun 18 Oct, 10:40");
});

test("a bare day is a day, never a time of day: it does not show as 05:30", () => {
  // an alert is served with its day only ("2026-10-19"); read as a moment it is midnight UTC, 05:30 in IST
  assert.throws(() => formatTime("2026-10-19"), /a day has no time of day/);
  assert.equal(formatDateTime("2026-10-19"), "Mon 19 Oct");
  assert.equal(formatDayTime("2026-10-19"), "19 Oct");
  assert.equal(istDate("2026-10-19"), "2026-10-19");
  assert.equal(isDay("2026-10-19"), true);
  assert.equal(isDay("2026-10-19T00:00:00Z"), false);
  const now = "2026-10-19T06:00:00Z";
  assert.equal(sinceText("2026-10-19", now), "since Mon 19 Oct"); // the same day: still no invented time
  assert.equal(sinceText("2026-10-18", now), "since Sun 18 Oct");
});

test("ago", () => {
  const now = "2026-10-19T06:00:00Z";
  assert.match(agoText("2026-10-19T05:55:00Z", now), /5 min/);
  assert.match(agoText("2026-10-19T03:00:00Z", now), /3 hr|3 h/);
  assert.match(agoText("2026-10-18T05:00:00Z", now), /yesterday/);
});

test("a bad date is refused, not shown as 'Invalid Date'", () => {
  assert.throws(() => formatTime("not a date"), RangeError);
});

test("counts: suppressed strings pass through, numbers get Indian grouping, non-integers are refused", () => {
  assert.equal(formatCount("<5"), "<5");
  assert.equal(formatCount(1234567), "12,34,567");
  assert.throws(() => formatCount(2.5), TypeError);
});

test("percent and range come from fractions; 62 instead of 0.62 is refused", () => {
  assert.equal(formatPercent(0.625), "63%");
  assert.equal(formatPercent(0.62), "62%");
  assert.equal(formatRange(0.54, 0.7), "54 to 70");
  assert.throws(() => formatPercent(62), RangeError);
  assert.throws(() => formatPercent(-0.1), RangeError);
});

test("a share shows its interval, and n when asked", () => {
  const share = { n: 112, status: "ok", share: 0.62, ci_low: 0.54, ci_high: 0.7, unsure: 14 };
  assert.equal(formatShare(share), "62% (54 to 70)");
  assert.equal(formatShare(share, { withN: true }), "62% (54 to 70), n = 112");
  assert.equal(formatShare({ ...share, status: "bracketed" }), "62% (54 to 70)");
});

test("too few answers shows no percentage at all", () => {
  const text = formatShare({ n: 11, status: "too_few", share: null, needed: 14 });
  assert.equal(text, "n = 11 of 25 needed");
  assert.ok(!text.includes("%"));
});

test("no data says 'No calls yet', never 0", () => {
  assert.equal(formatShare({ n: 0, status: "no_data" }), "No calls yet");
});

test("an ok share missing its interval is an error, not a bare percentage", () => {
  assert.throws(() => formatShare({ n: 50, status: "ok", share: 0.6, ci_low: null, ci_high: null }));
  assert.throws(() => formatShare({ n: 50, status: "weird" }));
});

test("masked phone and dose labels", () => {
  assert.equal(maskedPhone("4821"), "ending 4821");
  assert.equal(maskedPhone(null), "number removed");
  assert.equal(doseLabel(3), "Dose 3");
});

test("Hindi: Hindi words, Western digits, IST", () => {
  resetBundles();
  installBundle("common", "hi", bundle("hi"));
  const share = { n: 112, status: "ok", share: 0.62, ci_low: 0.54, ci_high: 0.7, unsure: 14 };
  assert.equal(formatShare(share, { withN: true }), "62% (54 से 70), n = 112");
  assert.equal(formatShare({ n: 11, status: "too_few", needed: 14 }), "n = 11, कम से कम 25 चाहिए");
  assert.equal(formatTime("2026-10-19T05:10:00Z"), "10:40");
  assert.equal(sinceText("2026-10-19T05:10:00Z", "2026-10-19T06:00:00Z"), "10:40 से");
  assert.match(formatDate("2026-10-19T05:10:00Z"), /19/);
  assert.equal(doseLabel(3), "खुराक 3");
});

test("a missing key is loud, not blank", () => {
  const original = console.error;
  const seen = [];
  console.error = (m) => seen.push(m);
  try {
    assert.equal(t("no.such.key"), "[missing: no.such.key]");
  } finally {
    console.error = original;
  }
  assert.equal(seen.length, 1);
});

test("plural keys follow the count", () => {
  assert.equal(t("status.clinicRow.tried", { count: 1 }), "Tried 1 time");
  assert.equal(t("status.clinicRow.tried", { count: 2 }), "Tried 2 times");
});

test("the bundle flattener keeps nested keys and skips _meta", () => {
  assert.deepEqual(flatten({ _meta: { x: 1 }, a: { b: "c" } }), { "a.b": "c" });
});

test("a withheld cell is translated; a too_few share without `needed` is refused, not guessed", () => {
  assert.equal(formatCount("hidden"), "hidden");
  assert.equal(formatCount("<5"), "<5");
  assert.throws(() => formatShare({ n: 11, status: "too_few", needed: null }), /needs `needed`/);
  assert.throws(() => formatShare({ n: 11, status: "too_few" }), /needs `needed`/);
  assert.equal(formatShare({ n: 11, status: "too_few", needed: 14 }), "n = 11 of 25 needed");
  resetBundles();
  installBundle("common", "hi", bundle("hi"));
  assert.equal(formatCount("hidden"), "छिपा हुआ");
});

test("the reference-band label is the key from the server with the band filled in, in either language", () => {
  const coverage = { claim_band: [0.78, 0.8], claim_label_key: "share.claim" };
  assert.equal(claimLabel(coverage), "78-80% claimed: expected only if sterilised and unsterilised dogs bite equally");
  installBundle("common", "hi", bundle("hi"));
  assert.match(claimLabel(coverage), /^78 से 80% का दावा/);
  assert.throws(() => claimLabel({ claim_band: [0.78, 0.8] }), /claim_label_key/);
});

test("a band key the bundle does not have shows as missing, loudly, not as English", () => {
  const seen = [];
  const original = console.error;
  console.error = (m) => seen.push(m);
  try {
    assert.equal(claimLabel({ claim_band: [0.78, 0.8], claim_label_key: "share.nope" }), "[missing: share.nope]");
  } finally {
    console.error = original;
  }
  assert.equal(seen.length, 1);
});

test("look-here reasons are built from the numbers, in the reader's language", () => {
  const strong = { look_here_reason_data: { kind: "strong", n: 1200 } };
  assert.match(lookHereReason(strong), /by more than chance explains \(1,200 answers here\)\.$/);
  assert.equal(lookHereReason({ look_here_reason_data: null }), null);
  assert.equal(lookHereReason({}), null);
  installBundle("common", "hi", bundle("hi"));
  assert.match(lookHereReason(strong), /\(यहाँ 1,200 जवाब\)।$/);
  assert.throws(() => lookHereReason({ look_here_reason_data: { kind: "other" } }), /unknown look-here reason/);
  // the old "two periods running" rule is gone: a server that still sent it would be a fault, not a sentence
  assert.throws(() => lookHereReason({ look_here_reason_data: { kind: "runs", before: 58, now: 61 } }), /unknown look-here reason/);
});
