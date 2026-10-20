// The patient notice, in Hindi, Dogri, Urdu and English: what is kept, why, for how long, how to stop, how
// to complain, who is responsible. It holds no phone numbers. Dogri and Urdu are machine-written and say so.

import { h } from "/js/core/dom.js";
import { mountFrame } from "/js/core/frame.js";
import { has, initI18n, t } from "/js/core/i18n.js";

const ORDER = ["kept", "why", "register", "long", "stop", "complain", "who"];
const main = document.getElementById("main");

function render() {
  document.title = t("privacy.page");
  main.replaceChildren(
    h("h1", null, t("privacy.title")),
    // a machine-written version says so before the answers, not after them
    ...(has("privacy.draft") ? [h("p", { class: "privacy__draft muted small", role: "note" }, t("privacy.draft"))] : []),
    h(
      "dl",
      { class: "privacy__list" },
      ORDER.map((key) => h("div", { class: "privacy__item" }, h("dt", null, t(`privacy.items.${key}.h`)), h("dd", null, t(`privacy.items.${key}.p`)))),
    ),
  );
  main.removeAttribute("aria-busy");
}

async function boot() {
  await initI18n({ bundles: ["common", "privacy"], audience: "patient" });
  await mountFrame({ mode: "patient" });
  render();
  document.addEventListener("ct:langchange", render);
}

boot().catch((err) => {
  console.error(err);
  main.removeAttribute("aria-busy");
  main.replaceChildren(h("p", { role: "alert" }, "Chaar Teeke could not load this page. Reload it."));
});
