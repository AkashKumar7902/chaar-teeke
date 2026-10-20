// O3 Method. One page, plain words, readable by every signed-in role and by visitors: what is measured, the proxy
// caveat, what could not be seen, the minimum and the "<5" rule, deletion, who sees what. The sections carry fixed
// anchors that (i) links on other pages point to.

import { getMode } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { t } from "/js/core/i18n.js";
import { boot, lines } from "./oversight/lib.js";

await boot(["method"], "method.title", { open: true });

/** The anchors other pages link to (/method#proxy). Do not rename without changing every (i) link. */
export const SECTIONS = ["measured", "proxy", "could-not-see", "minimum", "deletion", "who-sees", "demo-data"];

const main = document.getElementById("main");
// The demo-data section is about the demo history, so it is only there in demo mode.
const shown = (await getMode()).mode === "demo" ? SECTIONS : SECTIONS.filter((id) => id !== "demo-data");

function section(id) {
  const items = lines(`method.${id}`);
  const list = id === "deletion" || id === "who-sees";
  return h(
    "section",
    { class: "method__sec stack stack--s", id, "aria-labelledby": `${id}-h` },
    h("h2", { id: `${id}-h` }, t(`method.${id}.title`)),
    list ? h("ul", { class: "method__list" }, items.map((x) => h("li", null, x))) : items.map((x) => h("p", null, x)),
  );
}

function build() {
  main.replaceChildren(
    h("div", { class: "stack stack--s" }, h("h1", null, t("method.title")), h("p", { class: "muted" }, t("method.intro"))),
    h(
      "nav",
      { class: "method__toc", "aria-label": t("method.contents") },
      h("p", { class: "method__toc-title" }, t("method.contents")),
      h("ul", null, shown.map((id) => h("li", null, h("a", { href: `#${id}` }, t(`method.${id}.title`))))),
    ),
    ...shown.map(section),
  );
  // arriving on /method#proxy: scroll once the sections exist
  if (location.hash) document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView();
}

build();
document.addEventListener("ct:langchange", build);
