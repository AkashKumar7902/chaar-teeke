import assert from "node:assert/strict";
import { test } from "node:test";
import { addWords, englishByClip, englishForUrls, parseClipUrl, slotValues } from "./captions.js";

test("parseClipUrl reads the language and the key under it", () => {
  assert.deepEqual(parseClipUrl("/media/prompts/hi/FC_CLOSE.2.wav"), { lang: "hi", key: "FC_CLOSE.2" });
  assert.deepEqual(parseClipUrl("/media/prompts/ur/slot/day_22.wav"), { lang: "ur", key: "slot/day_22" });
  assert.deepEqual(parseClipUrl("http://x.test/media/prompts/doi/SYS_BYE.wav"), { lang: "doi", key: "SYS_BYE" });
  assert.equal(parseClipUrl("/media/prompts/en/SYS_BYE.wav"), null);
  assert.equal(parseClipUrl("/somewhere/else.wav"), null);
});

test("addWords continues a line in the same language and breaks it when the language changes", () => {
  const lines = [];
  addWords(lines, "hi", "हिंदी के लिए 1 दबाएँ।");
  addWords(lines, "hi", "और");
  addWords(lines, "ur", "اردو کے لیے 3 دبائیں۔");
  assert.deepEqual(lines, [
    { lang: "hi", text: "हिंदी के लिए 1 दबाएँ। और" },
    { lang: "ur", text: "اردو کے لیے 3 دبائیں۔" },
  ]);
});

test("addWords hangs a punctuation-led segment on the word before it", () => {
  const lines = [];
  addWords(lines, "hi", "डेस्क");
  addWords(lines, "hi", ", जम्मू के");
  addWords(lines, "ur", "کلینک");
  addWords(lines, "ur", "، جموں");
  assert.deepEqual(lines, [
    { lang: "hi", text: "डेस्क, जम्मू के" },
    { lang: "ur", text: "کلینک، جموں" },
  ]);
});

test("addWords keeps the English words of each clip beside its line, and adds none for a clip without them", () => {
  const lines = [];
  addWords(lines, "hi", "नमस्ते।", "Hello.");
  addWords(lines, "hi", "क्लिनिक आइए।", "Come to the clinic.");
  addWords(lines, "hi", "शुक्रिया।", null);
  addWords(lines, "ur", "اردو", "Urdu.");
  assert.deepEqual(lines, [
    { lang: "hi", text: "नमस्ते। क्लिनिक आइए। शुक्रिया।", en: "Hello. Come to the clinic." },
    { lang: "ur", text: "اردو", en: "Urdu." },
  ]);
});

// The English words as the server sends them (GET /api/phone/captions?lang=hi&with=en): a whole line on the
// key of its first clip, with {run.place} where a slot goes, and the English of each slot clip.
const EN = {
  texts: {
    SYS_BYE: "Thank you. Goodbye.",
    "FC_CLOSE.0": "Your next injection is injection {1.0} of {0.0}, on {1.1}, at {2.0}, at {2.1}. Press 9 to stop.",
  },
  slots: {
    "slot/num_5": "5",
    "slot/num_2": "2",
    "slot/weekday_3": "Thursday",
    "slot/day_22": "22",
    "slot/month_10": "October",
    "slot/time_0900": "9:00 am",
    "slot/clinic_cln_gmc": "Government Medical College and Hospital, Jammu",
  },
};
// what FC_CLOSE plays in Hindi: the numbers come in Hindi order (total, then the dose), then the date
const FC_CLOSE_HI = ["FC_CLOSE.0", "slot/num_5", "FC_CLOSE.1", "slot/num_2", "slot/weekday_3", "slot/day_22", "slot/month_10", "FC_CLOSE.2", "slot/time_0900", "slot/clinic_cln_gmc", "FC_CLOSE.3"];

test("slotValues reads a run of slot clips as the values they speak", () => {
  const run = ["slot/num_2", "slot/weekday_3", "slot/day_22", "slot/month_10"];
  assert.deepEqual(slotValues(run, EN.slots), ["2", "Thursday 22 October"]);
  assert.deepEqual(slotValues(["slot/num_5", "slot/num_2"], EN.slots), ["52"]); // the digits of one number
  assert.deepEqual(slotValues(["slot/time_0900", "slot/clinic_cln_gmc"], EN.slots), ["9:00 am", "Government Medical College and Hospital, Jammu"]);
  assert.deepEqual(slotValues(["slot/ward_6"], EN.slots), [null]); // no English for this clip: no value, nothing made up
});

test("englishByClip puts the whole English line, slots filled, on the first clip of a line", () => {
  const lines = englishByClip(FC_CLOSE_HI, EN);
  assert.equal(
    lines[0],
    "Your next injection is injection 2 of 5, on Thursday 22 October, at 9:00 am, at Government Medical College and Hospital, Jammu. Press 9 to stop.",
  );
  assert.deepEqual(lines.slice(1), Array(10).fill(null));
});

test("englishByClip tells one line from the next, and a line with no slots is its own clip", () => {
  const keys = ["SYS_BYE", ...FC_CLOSE_HI, "SYS_BYE"];
  const lines = englishByClip(keys, EN);
  assert.equal(lines[0], "Thank you. Goodbye.");
  assert.match(lines[1], /^Your next injection is injection 2 of 5/);
  assert.equal(lines[12], "Thank you. Goodbye.");
  assert.equal(lines.filter(Boolean).length, 3);
});

test("englishByClip gives no English for a line it cannot fill, never a half-filled one", () => {
  const noClinic = { ...EN, slots: { ...EN.slots, "slot/clinic_cln_gmc": undefined } };
  assert.equal(englishByClip(FC_CLOSE_HI, noClinic)[0], null);
  assert.deepEqual(englishByClip(["FC_NOTICE"], EN), [null]); // the data has no English for this one
});

test("englishByClip captions the short notice and the full notice each on its own clip", () => {
  const notices = {
    texts: {
      FC_NOTICE: "We call this number before each injection. Press 9 at any time to stop, or press 0 for the full notice.",
      FC_NOTICE_FULL: "We will call this number before each injection. If you have a complaint, you can write to the Data Protection Board of India.",
    },
    slots: {},
  };
  const keys = ["FC_NOTICE", "FC_NOTICE_FULL", "FC_NOTICE_FULL"]; // the 0 key plays the full notice, here twice
  const lines = englishByClip(keys, notices);
  assert.match(lines[0], /press 0 for the full notice\.$/);
  assert.match(lines[1], /Data Protection Board of India\.$/);
  assert.equal(lines[2], lines[1]); // asked for again: the same line again, not a gap
  assert.deepEqual(englishByClip(["FC_NOTICE_FULL"], EN), [null]); // no English loaded: none made up
});

test("englishForUrls reads a play a language at a time", () => {
  const urls = ["/media/prompts/hi/SYS_BYE.wav", "/media/prompts/ur/SYS_BYE.wav"];
  const ur = { texts: { SYS_BYE: "Thank you. Goodbye. (Urdu)" }, slots: {} };
  assert.deepEqual(englishForUrls(urls, new Map([["hi", EN], ["ur", ur]])), ["Thank you. Goodbye.", "Thank you. Goodbye. (Urdu)"]);
  assert.deepEqual(englishForUrls(urls, new Map([["hi", EN]])), ["Thank you. Goodbye.", null]); // Urdu English did not load
  assert.deepEqual(englishForUrls(["/somewhere/else.wav"], new Map([["hi", EN]])), [null]);
});

test("englishForUrls says the same English line once when the language menu repeats it for each language", () => {
  const menu = { texts: { SYS_LANG: "For Hindi, press 1. For Dogri, press 2. For Urdu, press 3." }, slots: {} };
  const urls = ["hi", "doi", "ur"].map((lang) => `/media/prompts/${lang}/SYS_LANG.wav`);
  const lines = englishForUrls(urls, new Map(["hi", "doi", "ur"].map((lang) => [lang, menu])));
  assert.deepEqual(lines, ["For Hindi, press 1. For Dogri, press 2. For Urdu, press 3.", null, null]);
});
