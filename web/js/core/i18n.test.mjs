import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync, readdirSync } from "node:fs";

import { chooseLang, flatten, initI18n, resetBundles, setLang, t, installBundle } from "./i18n.js";
import { STATUS_DOMAINS } from "../ui/pill.js";
import { ICON_NAMES } from "../ui/icons.js";

const load = (lang) => flatten(JSON.parse(readFileSync(new URL(`../../i18n/common.${lang}.json`, import.meta.url), "utf8")));
const raw = (lang) => JSON.parse(readFileSync(new URL(`../../i18n/common.${lang}.json`, import.meta.url), "utf8"));
const slots = (s) => (s.match(/\{\w+\}/g) ?? []).sort().join();
const en = load("en");

test("Hindi has every English key with the same placeholders", () => {
  const hi = load("hi");
  assert.deepEqual(Object.keys(en).filter((k) => !(k in hi)), []);
  assert.deepEqual(Object.keys(hi).filter((k) => !(k in en)), []);
  for (const k of Object.keys(hi)) assert.equal(slots(hi[k]), slots(en[k]), k);
});

test("Dogri and Urdu are labelled subsets of English, flagged for a native check", () => {
  for (const lang of ["doi", "ur"]) {
    const sub = load(lang);
    assert.deepEqual(Object.keys(sub).filter((k) => !(k in en)), [], lang);
    for (const k of Object.keys(sub)) assert.equal(slots(sub[k]), slots(en[k]), `${lang} ${k}`);
    assert.equal(raw(lang)._meta.needs_native_check, true);
  }
});

test("the patient frame, toast and error states find every string they use in Dogri and Urdu", () => {
  // keys read by frame.js (patient mode), toast.js, state.js (retry/offline), api.js errors, qr.js
  const needed = [
    "frame.skip", "frame.home", "frame.brand", "frame.language",
    "frame.demo.region", "frame.demo.label", "frame.demo.note", "frame.demo.clock",
    "buttons.retry", "buttons.close", "states.offline", "states.notifications",
    "errors.network", "errors.timeout", "errors.server", "errors.generic", "qr.alt", "qr.missing",
  ];
  for (const lang of ["doi", "ur"]) {
    const sub = load(lang);
    assert.deepEqual(needed.filter((k) => !(k in sub)), [], lang);
  }
});

test("never-on-screen words are in no bundle (SPEC s1.9, clinical safety)", () => {
  const banned = /\b(safe|safely|clear|all fine|rabies detected|diagnosis|fully sterili[sz]ed|restart|extra dose)\b/i;
  for (const lang of ["en", "hi", "doi", "ur"]) {
    for (const [k, v] of Object.entries(load(lang))) assert.doesNotMatch(v, banned, `${lang} ${k}`);
  }
});

test("honesty pins sit in every language that has the frame", () => {
  for (const lang of ["en", "hi", "doi", "ur"]) assert.equal(load(lang)["frame.demo.label"], "DEMO DATA:", lang);
  assert.match(en["status.location.matched"], /estimate/i);
  assert.match(en["share.explain"], /interval/i);
});

test("every status pill has an icon that exists and words in English and Hindi; only the system domain is red", () => {
  const hi = load("hi");
  for (const [domain, values] of Object.entries(STATUS_DOMAINS)) {
    for (const [value, [tone, iconName]] of Object.entries(values)) {
      assert.ok(ICON_NAMES.includes(iconName), `${domain}/${value} icon ${iconName}`);
      const key = `status.${domain}.${value}`;
      for (const table of [en, hi]) assert.ok(key in table || `${key}.other` in table, key);
      if (tone === "alarm") assert.equal(domain, "system", `${domain}/${value} is red`);
    }
  }
});

test("a language and a bundle are only remembered once they loaded", async () => {
  const store = new Map();
  globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  const attrs = {};
  globalThis.document = {
    documentElement: { setAttribute: (k, v) => (attrs[k] = v) },
    querySelectorAll: () => [],
    dispatchEvent: () => true,
  };
  globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init?.detail; } };
  const bundles = { "common.en": raw("en"), "common.hi": raw("hi") };
  globalThis.fetch = async (url) => {
    const name = String(url).match(/\/i18n\/(.+)\.json/)[1];
    return name in bundles ? new Response(JSON.stringify(bundles[name])) : new Response("", { status: 404 });
  };
  resetBundles();
  assert.equal(await initI18n({ bundles: ["common"], audience: "patient", lang: "hi" }), "hi");
  assert.equal(attrs.lang, "hi");
  assert.equal(attrs.dir, "ltr");
  await assert.rejects(setLang("doi"), /cannot load common\.doi\.json/);
  assert.equal(store.has("ct.plang"), false, "a failed switch must not be remembered");
  assert.equal(t("buttons.retry"), load("hi")["buttons.retry"], "the old language is still loaded");
  await assert.rejects(initI18n({ bundles: ["common"], audience: "staff", lang: "ur" }), /not offered on staff/);
  bundles["common.ur"] = raw("ur");
  await setLang("ur");
  assert.equal(attrs.dir, "rtl");
  assert.equal(store.get("ct.plang"), "ur");
  assert.equal(chooseLang({ audience: "patient", search: "?lang=en" }), "en");
  assert.equal(chooseLang({ audience: "patient", search: "?lang=xx" }), "ur", "an unknown ?lang= is ignored, the remembered one wins");
  assert.equal(chooseLang({ audience: "staff", search: "?lang=ur" }), "en", "staff pages offer only en and hi");
});

test("Dogri counts like Hindi, not like the machine's default locale", () => {
  resetBundles();
  installBundle("common", "doi", { x: { one: "ik", other: "kai" } });
  assert.equal(t("x", { count: 1 }), "ik");
  assert.equal(t("x", { count: 5 }), "kai");
});

// ---- the Hindi glossary (docs/DESIGN.md "Hindi glossary"): one word for one thing, in every hi bundle
const I18N_DIR = new URL("../../i18n/", import.meta.url);
const hiBundles = readdirSync(I18N_DIR).filter((f) => f.endsWith(".hi.json"));
const hiStrings = hiBundles.flatMap((f) => Object.entries(flatten(JSON.parse(readFileSync(new URL(f, I18N_DIR), "utf8")))).map(([k, v]) => [`${f} ${k}`, v]));

const FORBIDDEN_HI = [
  [/निशान/, "the notch is V-कट; tick is टिक करें"],
  [/सड़क (के|का) कुत्त/, "street dog is गली के कुत्ते"],
  [/चेतावनी/, "alert is अलर्ट"],
  [/इलाक[ेा]|इलाक़[ेा]/, "area is क्षेत्र"],
  [/यहाँ देखिए/, "look here is यहाँ देखें"],
  [/नकली/, "simulated is सिम्युलेटेड"],
  [/सिमुलेट/, "spell it सिम्युलेट..."],
  [/वैक्सीन/, "vaccine is टीका"],
  [/योजना|कार्यक्रम/, "schedule is शेड्यूल"],
  [/मशीन आवाज़/, "machine voice is मशीनी आवाज़ का मसौदा"],
  [/आवाज़ संदेश/, "prompts are वॉइस संदेश"],
  [/बिंदीदार/, "dashed is टूटी रेखा"],
  [/वापस कॉल|वापसी फ़ोन/, "call-back is कॉल-बैक"],
  [/वापसी की सूचना/, "withdrawal is नाम वापस लेना"],
  [/खत्म|ख़तम|खतम/, "spell it ख़त्म"],
  [/खराब/, "spell it ख़राब"],
  [/तारीख(?!़)/, "spell it तारीख़"],
  [/बाकी/, "spell it बाक़ी"],
  [/हिंदी/, "spell it हिन्दी"],
  [/पन्ना/, "page is पेज"],
];

test("the Hindi bundles use the glossary: no forbidden variant appears", () => {
  assert.ok(hiStrings.length > 800, "all the hi bundles were read");
  const hits = [];
  for (const [where, text] of hiStrings) for (const [pattern, why] of FORBIDDEN_HI) if (pattern.test(text)) hits.push(`${where}: ${pattern} (${why}): ${text.slice(0, 60)}`);
  assert.deepEqual(hits, []);
});

test("the Hindi glossary words are the ones in use", () => {
  const hi = load("hi");
  const all = hiStrings.map(([, v]) => v).join("\n");
  for (const word of ["गली के कुत्त", "V-कट", "अलर्ट", "क्षेत्र", "यहाँ देखें", "मशीनी आवाज़ का मसौदा", "सिम्युलेटेड", "टीका", "खुराक", "शेड्यूल", "कॉल-बैक", "नाम वापस लेने", "JMC के बाहर", "टिक करें", "वॉइस संदेश", "टूटी रेखा"]) {
    assert.ok(all.includes(word), `${word} is used somewhere`);
  }
  const landing = Object.fromEntries(hiStrings.filter(([w]) => w.startsWith("landing.hi.json ")).map(([w, v]) => [w.slice("landing.hi.json ".length), v]));
  assert.equal(landing["landing.feed.alert"], "अलर्ट");
  assert.match(landing["landing.feed.empty"], /अलर्ट/);
  assert.equal(hi["frame.jump"], "घड़ी आगे करें");
});

test("the clock control and the two team pages carry their plain names", () => {
  assert.equal(en["frame.jump"], "Move clock");
  assert.equal(en["nav.worked"], "Wards the team covered");
  assert.equal(en["nav.sight"], "Dogs seen on rounds");
  assert.ok(!/worked wards|sight count/i.test(Object.values(en).join("\n")));
});

test("the nurse's read of the clinic, enrol and course Hindi is recorded as not done until it is done", () => {
  const meta = raw("hi")._meta;
  assert.match(meta.nurse_check, /^(NOT DONE|DONE by a nurse)/);
});

test("the draft-translation note exists in every language, and only Dogri and Urdu show it", async () => {
  const { DRAFT_LANGS } = await import("./i18n.js");
  assert.deepEqual(DRAFT_LANGS, ["doi", "ur"]);
  for (const lang of ["en", "hi", "doi", "ur"]) assert.ok(load(lang)["frame.draft"], lang);
  for (const lang of ["doi", "ur"]) assert.equal(JSON.stringify(JSON.parse(readFileSync(new URL(`privacy.${lang}.json`, I18N_DIR), "utf8"))).includes('"draft"'), false, "the privacy page no longer carries its own copy of the note");
});
