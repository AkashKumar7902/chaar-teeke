// Strings. Bundles are web/i18n/<bundle>.<lang>.json (nested keys, flattened here). There is no
// silent fallback to another language: a missing key shows as [missing: key] and logs an error,
// so the UI check and the tests catch it.

import { readPref, writePref } from "./storage.js";

export const STAFF_LANGS = ["en", "hi"];
export const PATIENT_LANGS = ["hi", "doi", "ur", "en"];
/** Language names in their own script. Fixed data, not translations. */
export const AUTONYMS = { en: "English", hi: "हिन्दी", doi: "डोगरी", ur: "اردو" };
const RTL = new Set(["ur"]);
/** Languages no native speaker has read yet: patient pages show the dashed "draft translation" note in them. */
export const DRAFT_LANGS = ["doi", "ur"];
// Dogri has no Intl plural data; it counts like Hindi, and without this the default locale would decide.
const PLURAL_LOCALE = { doi: "hi" };

const STAFF_PREF = "ct.lang";
const PATIENT_PREF = "ct.plang";

let current = "en";
let table = new Map();
const loaded = new Map(); // "bundle.lang" -> flat object

export function flatten(obj, prefix = "", out = {}) {
  for (const [k, v] of Object.entries(obj)) {
    if (k === "_meta") continue;
    const key = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === "object") flatten(v, key, out);
    else out[key] = String(v);
  }
  return out;
}

export function direction(lang) {
  return RTL.has(lang) ? "rtl" : "ltr";
}

export function currentLang() {
  return current;
}

/** Install a bundle from an object and make its language the current one (used by tests and tools). */
export function installBundle(name, lang, obj) {
  loaded.set(`${name}.${lang}`, flatten(obj));
  current = lang;
  rebuild(lang);
}

function rebuild(lang) {
  const merged = new Map();
  for (const [id, flat] of loaded) {
    if (!id.endsWith(`.${lang}`)) continue;
    for (const [k, v] of Object.entries(flat)) merged.set(k, v);
  }
  table = merged;
}

export function resetBundles() {
  loaded.clear();
  table = new Map();
}

/** Pick the plural form key (key.one / key.other ...) when vars.count is a number. */
function pluralKey(key, vars) {
  if (!vars || typeof vars.count !== "number") return key;
  const rule = new Intl.PluralRules(PLURAL_LOCALE[current] ?? current).select(vars.count);
  if (table.has(`${key}.${rule}`)) return `${key}.${rule}`;
  if (table.has(`${key}.other`)) return `${key}.other`;
  return key;
}

export function has(key) {
  return table.has(key);
}

/** t("frame.demo.label") or t("clinic.due", {count: 3}). {name} placeholders are filled from vars. */
export function t(key, vars) {
  const k = pluralKey(key, vars);
  const text = table.get(k);
  if (text === undefined) {
    console.error(`i18n: missing key "${key}" for language "${current}"`);
    return `[missing: ${key}]`;
  }
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (whole, name) => {
    if (!(name in vars)) {
      console.error(`i18n: key "${key}" needs "${name}"`);
      return `[?${name}]`;
    }
    return String(vars[name]);
  });
}

/** Which language to use: ?lang= on patient pages, then the remembered choice, then the page default. */
export function chooseLang({ audience = "staff", search = globalThis.location?.search ?? "" } = {}) {
  const allowed = audience === "patient" ? PATIENT_LANGS : STAFF_LANGS;
  const asked = new URLSearchParams(search).get("lang");
  if (asked && allowed.includes(asked)) return asked;
  const remembered = readPref(audience === "patient" ? PATIENT_PREF : STAFF_PREF);
  if (remembered && allowed.includes(remembered)) return remembered;
  return audience === "patient" ? "hi" : "en";
}

export function applyDocumentLang(lang) {
  const root = document.documentElement;
  root.setAttribute("lang", lang);
  root.setAttribute("dir", direction(lang));
}

async function fetchBundle(name, lang) {
  const response = await fetch(`/i18n/${name}.${lang}.json`, { credentials: "same-origin" });
  if (!response.ok) throw new Error(`i18n: cannot load ${name}.${lang}.json (${response.status})`);
  return response.json();
}

/**
 * Load bundles and set <html lang dir>. Pages call it first:
 *   await initI18n({ bundles: ["common", "clinic"], audience: "staff" });
 * A bundle that does not exist for the language is an error, not a silent switch to English.
 */
export async function initI18n({ bundles = ["common"], audience = "staff", lang } = {}) {
  const chosen = lang ?? chooseLang({ audience });
  const allowed = audience === "patient" ? PATIENT_LANGS : STAFF_LANGS;
  if (!allowed.includes(chosen)) throw new Error(`i18n: language "${chosen}" is not offered on ${audience} pages`);
  const objs = await Promise.all(bundles.map((b) => fetchBundle(b, chosen)));
  loaded.clear();
  bundles.forEach((b, i) => loaded.set(`${b}.${chosen}`, flatten(objs[i])));
  current = chosen;
  rebuild(chosen);
  applyDocumentLang(chosen);
  activeBundles = { bundles: [...bundles], audience };
  translateDom(document);
  return chosen;
}

let activeBundles = { bundles: ["common"], audience: "staff" };

/**
 * Switch language without a reload (typed input stays on screen). Reloads the bundles, retranslates
 * [data-i18n] nodes and fires "ct:langchange" on document so pages re-render what they built in JS.
 */
export async function setLang(lang) {
  const { bundles, audience } = activeBundles;
  const allowed = audience === "patient" ? PATIENT_LANGS : STAFF_LANGS;
  if (!allowed.includes(lang)) throw new Error(`i18n: language "${lang}" is not offered on ${audience} pages`);
  await initI18n({ bundles, audience, lang });
  // Remembered only once the bundles loaded: a language a page has no bundle for must not lock the next visit.
  writePref(audience === "patient" ? PATIENT_PREF : STAFF_PREF, lang);
  document.dispatchEvent(new CustomEvent("ct:langchange", { detail: { lang } }));
}

/** Fill static markup: data-i18n="key" sets the text; data-i18n-attr="aria-label:key;title:key2" sets attributes. */
export function translateDom(root = document) {
  for (const el of root.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
  for (const el of root.querySelectorAll("[data-i18n-attr]")) {
    for (const pair of el.dataset.i18nAttr.split(";")) {
      const [attr, key] = pair.split(":").map((x) => x.trim());
      if (attr && key) el.setAttribute(attr, t(key));
    }
  }
}
