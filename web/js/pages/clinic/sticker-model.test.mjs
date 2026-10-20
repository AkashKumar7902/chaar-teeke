import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { PER_SHEET, STICKER_LANGS, callTarget, canPrint, codeBase, codeLinks, privacyLabels } from "./sticker-model.js";

const bundle = (lang) => JSON.parse(readFileSync(new URL(`../../../i18n/sticker.${lang}.json`, import.meta.url), "utf8"));

test("demo: the browser phone, which is a simulator, whatever number is configured", () => {
  assert.deepEqual(callTarget({ mode: "demo", ctNumber: null }), { kind: "simulator" });
  assert.deepEqual(callTarget({ mode: "demo", ctNumber: "0123" }), { kind: "simulator" });
});

test("pilot: the clinic's Chaar Teeke number from the config", () => {
  assert.deepEqual(callTarget({ mode: "pilot", ctNumber: " 1800-000-0000 " }), { kind: "number", number: "1800-000-0000" });
});

test("pilot with no number set says so: nothing stands in for the number, and nothing is printed", () => {
  for (const ctNumber of [null, undefined, "", "   "]) {
    const target = callTarget({ mode: "pilot", ctNumber });
    assert.deepEqual(target, { kind: "unset" });
    assert.equal(canPrint(target), false);
  }
  assert.equal(canPrint({ kind: "simulator" }), true);
  assert.equal(canPrint({ kind: "number", number: "1" }), true);
});

test("the codes: the privacy notice always, the browser phone only for the simulator", () => {
  assert.deepEqual(codeLinks({ base: "https://ct.example", target: { kind: "simulator" } }), { privacy: "https://ct.example/privacy", phone: "https://ct.example/phone" });
  assert.deepEqual(codeLinks({ base: "https://ct.example", target: { kind: "number", number: "1" } }), { privacy: "https://ct.example/privacy", phone: null });
});

test("codeBase: the public address when there is one, else this computer's own address and it says so", () => {
  assert.deepEqual(codeBase({ publicUrl: "https://ct.example//", origin: "http://127.0.0.1:8080" }), { base: "https://ct.example", localOnly: false });
  assert.deepEqual(codeBase({ publicUrl: null, origin: "http://127.0.0.1:8080" }), { base: "http://127.0.0.1:8080", localOnly: true });
  assert.throws(() => codeBase({ publicUrl: null, origin: "null" }), /no web address/);
});

test("a sheet is 8 stickers, in Hindi, Dogri and Urdu in that order", () => {
  assert.equal(PER_SHEET, 8);
  assert.deepEqual(STICKER_LANGS, ["hi", "doi", "ur"]);
});

test("the privacy label is said once where two languages share a word", () => {
  assert.deepEqual(privacyLabels({ hi: "A", doi: "A", ur: "B" }), ["A", "B"]);
});

test("every sticker line is in the English and Hindi bundles, the same in both, and carries the 7-day promise and key 9", () => {
  const en = bundle("en").sticker;
  const hi = bundle("hi").sticker;
  assert.deepEqual(Object.keys(hi), Object.keys(en));
  assert.deepEqual(hi.lines, en.lines);
  assert.deepEqual(hi.privacy, en.privacy);
  for (const lang of STICKER_LANGS) {
    assert.ok(en.lines[lang].call.length > 0, `${lang} call line`);
    assert.match(en.lines[lang].keep, /7/, `${lang} deletion promise`);
    assert.match(en.lines[lang].keep, /9/, `${lang} press 9`);
    assert.ok(en.privacy[lang].length > 0, `${lang} privacy label`);
  }
  assert.equal(en.simulator, "SIMULATOR");
  assert.equal(hi.simulator, "SIMULATOR");
  assert.equal(bundle("en")._meta.needs_native_check, true);
});
