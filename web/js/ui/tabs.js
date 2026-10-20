// Tabs (WAI-ARIA tabs pattern). Arrow keys move, Home/End jump. Panels are built lazily on first show.

import { h, uid } from "../core/dom.js";

/**
 * tabs({label, items: [{id, label, render: () => Node}], selected, onChange}) -> {el, select(id), current}
 * `label` names the tab group for screen readers.
 */
export function tabs({ label, items, selected = items[0]?.id, onChange }) {
  const base = uid("tabs");
  const rendered = new Set();
  const buttons = new Map();
  const panels = new Map();
  let current = null;

  const list = h(
    "div",
    { class: "tablist", role: "tablist", "aria-label": label },
    items.map((item) => {
      const b = h(
        "button",
        {
          type: "button",
          role: "tab",
          class: "tab",
          id: `${base}-tab-${item.id}`,
          "aria-controls": `${base}-panel-${item.id}`,
          "aria-selected": "false",
          tabindex: "-1",
          onClick: () => select(item.id, { focus: false }),
        },
        item.label,
      );
      buttons.set(item.id, b);
      return b;
    }),
  );
  const panelNodes = items.map((item) => {
    const p = h("div", {
      class: "tabpanel",
      role: "tabpanel",
      id: `${base}-panel-${item.id}`,
      "aria-labelledby": `${base}-tab-${item.id}`,
      tabindex: "0",
      hidden: true,
    });
    panels.set(item.id, p);
    return p;
  });

  function select(id, { focus = true, silent = false } = {}) {
    const item = items.find((i) => i.id === id);
    if (!item) throw new Error(`tabs: no tab "${id}"`);
    for (const [key, b] of buttons) {
      const on = key === id;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
      panels.get(key).hidden = !on;
    }
    if (!rendered.has(id)) {
      rendered.add(id);
      panels.get(id).append(item.render());
    }
    if (focus) buttons.get(id).focus();
    current = id;
    if (!silent) onChange?.(id);
  }

  list.addEventListener("keydown", (event) => {
    const ids = items.map((i) => i.id);
    const i = ids.indexOf(current);
    const rtl = getComputedStyle(list).direction === "rtl";
    let next = null;
    if (event.key === (rtl ? "ArrowLeft" : "ArrowRight")) next = ids[(i + 1) % ids.length];
    else if (event.key === (rtl ? "ArrowRight" : "ArrowLeft")) next = ids[(i - 1 + ids.length) % ids.length];
    else if (event.key === "Home") next = ids[0];
    else if (event.key === "End") next = ids[ids.length - 1];
    if (next) {
      event.preventDefault();
      select(next);
    }
  });

  const el = h("div", { class: "tabs" }, list, panelNodes);
  select(selected, { focus: false, silent: true });
  return {
    el,
    select,
    get current() {
      return current;
    },
  };
}
