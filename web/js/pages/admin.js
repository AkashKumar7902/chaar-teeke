// Admin: five tabs, one job each. M1 Clinics, M2 People, M3 Prompts, M4 Retention, M5 System.
// Admin role writes; a visitor viewing as admin reads and every change is refused. Any other role is told
// the page is not for it.

import { getMe } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { mountFrame, effectiveRole } from "/js/core/frame.js";
import { initI18n, t } from "/js/core/i18n.js";
import { emptyState, tabs } from "/js/ui/index.js";
import { clinicsTab } from "./admin/clinics.js";
import { peopleTab } from "./admin/people.js";
import { promptsTab } from "./admin/prompts.js";
import { retentionTab } from "./admin/retention.js";
import { systemTab } from "./admin/system.js";

await initI18n({ bundles: ["common", "admin"], audience: "staff" });
let { me } = await mountFrame({ mode: "staff" });

const TAB_IDS = ["clinics", "people", "prompts", "retention", "system"];
const main = document.getElementById("main");
const setTitle = () => {
  document.title = `${t("admin.title")} · ${t("frame.brand")}`;
};
setTitle();

let current = TAB_IDS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "clinics";
let page = null; // { controller, tabs }

function build() {
  page?.controller.abort();
  const controller = new AbortController();
  const { signal } = controller;
  main.replaceChildren(h("div", { class: "page-title" }, h("h1", null, t("admin.title"))));
  if (effectiveRole(me) !== "admin") {
    main.append(emptyState({ title: t("states.forbidden"), iconName: "info" }));
    page = { controller, tabs: null };
    return;
  }
  const make = { clinics: clinicsTab, people: peopleTab, prompts: promptsTab, retention: retentionTab, system: systemTab };
  const group = tabs({
    label: t("admin.tabsLabel"),
    selected: current,
    onChange: (id) => {
      current = id;
      try {
        history.replaceState(null, "", `#${id}`);
      } catch {
        // an address that cannot change is not a problem
      }
    },
    items: TAB_IDS.map((id) => ({
      id,
      label: t(`admin.tabs.${id}`),
      render: () => make[id]({ me, signal, openPrompts: () => group.select("prompts"), onReset: afterReset }),
    })),
  });
  main.append(group.el);
  page = { controller, tabs: group };
}

/** The reset ended every other staff login and gave this one a new CSRF token: read /api/me again, then draw the page from fresh data. */
async function afterReset() {
  me = await getMe();
  build();
}

build();
document.addEventListener("ct:langchange", () => {
  setTitle();
  build();
});
window.addEventListener("hashchange", () => {
  const id = location.hash.slice(1);
  if (TAB_IDS.includes(id) && page?.tabs && page.tabs.current !== id) page.tabs.select(id, { focus: false });
});
