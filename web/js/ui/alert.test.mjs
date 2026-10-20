import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import "./fakedom.mjs";
import { installBundle, resetBundles } from "../core/i18n.js";
import { alertCard, placeChip, placeLabel } from "./alert.js";

const bundle = (lang) => JSON.parse(readFileSync(new URL(`../../i18n/common.${lang}.json`, import.meta.url), "utf8"));

const alert = {
  id: "alr_1",
  ward_no: 48,
  ward_name: "Bahu (West)",
  cluster_id: "C3",
  place_type: "home_street",
  local_day: "2026-10-19",
  status: "new",
  claimed_by: null,
  claimed_at: null,
  outcome_note: null,
  created_at: "2026-10-19", // the server serves a real alert with its day only
  made_at: null,
  callback_status: null,
  simulated: false,
  claimed_by_me: false,
};

function titleOf(card) {
  return card.byClass("alert-card__title")[0].textContent;
}

test("a machine-matched ward on an alert card reads 'estimate', in English and Hindi", () => {
  for (const [lang, word, ward] of [["en", "estimate", "Ward 48, Bahu (West)"], ["hi", "अनुमान", "वार्ड 48, Bahu (West)"]]) {
    resetBundles();
    installBundle("common", lang, bundle(lang));
    const card = alertCard({ ...alert, location_status: "matched" });
    assert.match(titleOf(card), new RegExp(`${ward.replace(/[()]/g, "\\$&")} · ${word}`), lang);
    assert.equal(card.byClass("pill--estimate").length, 1, `${lang}: the ward sits in the dashed estimate pill`);
  }
});

test("only a reviewer-decided ward is bare", () => {
  resetBundles();
  installBundle("common", "en", bundle("en"));
  const decided = alertCard({ ...alert, location_status: "decided" });
  assert.equal(titleOf(decided).includes("estimate"), false);
  assert.equal(titleOf(decided).includes("Ward 48, Bahu (West)"), true);
  assert.equal(decided.byClass("pill--estimate").length, 0);
});

test("a card whose status is missing is not shown as decided", () => {
  resetBundles();
  installBundle("common", "en", bundle("en"));
  assert.match(titleOf(alertCard({ ...alert })), /estimate/);
});

test("no place is plain 'Place not known', never an estimate", () => {
  resetBundles();
  installBundle("common", "en", bundle("en"));
  const none = alertCard({ ...alert, ward_no: null, ward_name: null, cluster_id: null, location_status: "matched" });
  assert.equal(titleOf(none).includes("Place not known"), true);
  assert.equal(titleOf(none).includes("estimate"), false);
});

test("a feed row's ward chip (the landing feed uses placeChip) says 'estimate' beside a matched ward", () => {
  resetBundles();
  installBundle("common", "en", bundle("en"));
  const matched = placeChip({ wardNo: 48, wardName: null, clusterId: "C3", areaName: "Bahu", locationStatus: "matched" });
  assert.equal(matched.textContent, "Ward 48, Bahu · estimate");
  assert.match(matched.getAttribute("class"), /pill--estimate/);
  const decided = placeChip({ wardNo: 48, wardName: null, clusterId: "C3", areaName: "Bahu", locationStatus: "decided" });
  assert.equal(decided.textContent, "Ward 48, Bahu");
  assert.deepEqual(placeLabel({ wardNo: 48, wardName: "Bahu (West)", locationStatus: "matched" }), { text: "Ward 48, Bahu (West)", estimate: true });
});

const timeOf = (card) => card.byClass("alert-card__time")[0].textContent;

test("a real alert shows its day, never a time of day (a bare day is midnight UTC, which read as 05:30)", () => {
  resetBundles();
  installBundle("common", "en", bundle("en"));
  const card = alertCard({ ...alert, location_status: "decided" });
  assert.equal(timeOf(card), "Mon 19 Oct");
  assert.equal(card.textContent.includes("05:30"), false);
  assert.equal(card.textContent.split("Mon 19 Oct").length - 1, 1, "the day is said once on the card");
});

test("the visitor's own alert shows the day and the time it was made", () => {
  resetBundles();
  installBundle("common", "en", bundle("en"));
  const card = alertCard({ ...alert, simulated: true, made_at: "2026-10-19T05:10:00Z", location_status: "decided" });
  assert.equal(timeOf(card), "19 Oct, 10:40");
  assert.equal(card.textContent.includes("05:30"), false);
});

test("the day reads in Hindi too", () => {
  resetBundles();
  installBundle("common", "hi", bundle("hi"));
  assert.match(timeOf(alertCard({ ...alert })), /19/);
});
