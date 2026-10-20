// A disclosure popover (the (i) button) and a menu button (the Jump menu, "view as"). Plain DOM, no library:
// the trigger toggles aria-expanded, Esc closes and returns focus, a click elsewhere or any scroll closes.

import { h, uid } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { icon } from "./icons.js";

let openOne = null;
const EDGE = 8;

function place(trigger, panel) {
  const t = trigger.getBoundingClientRect();
  const rtl = getComputedStyle(trigger).direction === "rtl";
  panel.style.setProperty("--pop-x", "0px");
  panel.style.setProperty("--pop-y", "0px");
  const p = panel.getBoundingClientRect();
  let x = rtl ? t.right - p.width : t.left;
  x = Math.max(EDGE, Math.min(x, window.innerWidth - p.width - EDGE));
  let y = t.bottom + 4;
  if (y + p.height > window.innerHeight - EDGE && t.top - 4 - p.height >= EDGE) y = t.top - 4 - p.height;
  panel.style.setProperty("--pop-x", `${Math.round(x)}px`);
  panel.style.setProperty("--pop-y", `${Math.round(Math.max(EDGE, y))}px`);
}

/** Wire trigger + panel. Returns {open, close, toggle, isOpen}. */
export function attachPopover(trigger, panel, { onOpen, onClose } = {}) {
  const id = panel.id || (panel.id = uid("pop"));
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-controls", id);
  panel.hidden = true;

  const api = {
    get isOpen() {
      return !panel.hidden;
    },
    open() {
      if (openOne && openOne !== api) openOne.close();
      panel.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      place(trigger, panel);
      openOne = api;
      document.addEventListener("pointerdown", outside, true);
      document.addEventListener("keydown", onKey, true);
      window.addEventListener("resize", onViewport);
      window.addEventListener("scroll", onViewport, true);
      onOpen?.();
    },
    close({ restoreFocus = false } = {}) {
      if (panel.hidden) return;
      panel.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      if (openOne === api) openOne = null;
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", onViewport);
      window.removeEventListener("scroll", onViewport, true);
      if (restoreFocus) trigger.focus();
      onClose?.();
    },
    toggle() {
      if (api.isOpen) api.close();
      else api.open();
    },
  };

  function outside(event) {
    if (!panel.contains(event.target) && !trigger.contains(event.target)) api.close();
  }
  function onKey(event) {
    if (event.key === "Escape") {
      event.stopPropagation();
      api.close({ restoreFocus: true });
    }
  }
  function onViewport(event) {
    if (event.type === "scroll" && panel.contains(event.target)) return;
    api.close();
  }
  // Tab out of an open popover closes it; a click elsewhere is handled by `outside`.
  const onFocusOut = (event) => {
    const to = event.relatedTarget;
    if (to && !panel.contains(to) && !trigger.contains(to)) api.close();
  };
  trigger.addEventListener("focusout", onFocusOut);
  panel.addEventListener("focusout", onFocusOut);
  trigger.addEventListener("click", () => api.toggle());
  return api;
}

/**
 * The (i) button. `topic` is the name of what it explains, so the accessible name is "About <topic>",
 * never "More info". `content` is a Node or text (untrusted text must already be a text node).
 */
export function infoButton({ topic, title, content }) {
  const label = t("buttons.about", { topic });
  const trigger = h(
    "button",
    { type: "button", class: "info-btn", "aria-label": label, title: label },
    icon("info"),
  );
  const panel = h(
    "div",
    { class: "pop__panel", role: "group", "aria-label": label },
    h("p", { class: "pop__title" }, title ?? topic),
    content,
  );
  const wrap = h("span", { class: "pop" }, trigger, panel);
  const pop = attachPopover(trigger, panel);
  wrap.controller = pop;
  return wrap;
}

/**
 * A button that opens a short menu. items: [{id, label, current?, disabled?}]. onSelect(id) runs on choice.
 * Arrow keys move, Home/End jump, Enter or Space chooses (native buttons), Esc closes.
 */
export function menuButton({ label, items, onSelect, className = "btn", buttonContent, triggerLabel = null }) {
  const trigger = h(
    "button",
    { type: "button", class: className, "aria-haspopup": "menu", "aria-label": triggerLabel },
    buttonContent ?? [label, icon("chevronDown")],
  );
  const buttons = items.map((item) =>
    h(
      "button",
      {
        type: "button",
        role: "menuitem",
        class: "menu__item",
        disabled: item.disabled ? true : null,
        "aria-current": item.current ? "true" : null,
        onClick: () => {
          pop.close({ restoreFocus: true });
          onSelect(item.id);
        },
      },
      item.label,
    ),
  );
  const panel = h("div", { class: "pop__panel menu", role: "menu", "aria-label": label }, buttons);
  const wrap = h("span", { class: "pop" }, trigger, panel);
  const pop = attachPopover(trigger, panel, { onOpen: () => buttons.find((b) => !b.disabled)?.focus() });
  panel.addEventListener("keydown", (event) => {
    const enabled = buttons.filter((b) => !b.disabled);
    const i = enabled.indexOf(document.activeElement);
    const go = (n) => {
      event.preventDefault();
      enabled[(n + enabled.length) % enabled.length]?.focus();
    };
    if (event.key === "ArrowDown") go(i + 1);
    else if (event.key === "ArrowUp") go(i - 1);
    else if (event.key === "Home") go(0);
    else if (event.key === "End") go(enabled.length - 1);
  });
  wrap.controller = pop;
  wrap.trigger = trigger;
  return wrap;
}
