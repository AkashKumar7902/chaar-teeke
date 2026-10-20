// Page not found. Plain words, one link to the start. Any of the four languages (the person may hold a
// patient card), and the one the visitor last chose is used.

import { h } from "/js/core/dom.js";
import { mountFrame } from "/js/core/frame.js";
import { PATIENT_LANGS, initI18n, t } from "/js/core/i18n.js";
import { readPref } from "/js/core/storage.js";

const main = document.getElementById("main");

function pickLang() {
  const asked = new URLSearchParams(location.search).get("lang");
  for (const candidate of [asked, readPref("ct.plang"), readPref("ct.lang")]) if (candidate && PATIENT_LANGS.includes(candidate)) return candidate;
  return "en";
}

function render() {
  document.title = t("notfound.page");
  main.replaceChildren(h("h1", null, t("notfound.title")), h("p", null, t("notfound.body")), h("div", null, h("a", { class: "btn btn--primary", href: "/" }, t("notfound.home"))));
}

async function boot() {
  await initI18n({ bundles: ["common", "notfound"], audience: "patient", lang: pickLang() });
  await mountFrame({ mode: "patient" });
  render();
  document.addEventListener("ct:langchange", render);
}

boot().catch((err) => {
  console.error(err);
  main.replaceChildren(h("p", { role: "alert" }, "Page not found."), h("a", { href: "/" }, "/"));
});
