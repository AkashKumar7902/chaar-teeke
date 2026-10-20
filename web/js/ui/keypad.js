// Phone keypad for the browser phone (SIMULATOR). 0-9, * and #. Hints sit under the digits that mean
// something in the call (0 hear again, 9 withdraw). The pad never mirrors in Urdu.

import { h } from "../core/dom.js";

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"];

/** keypad({onKey, hints: {"0": "hear again", "9": "withdraw"}, disabled}) -> {el, setDisabled} */
export function keypad({ onKey, hints = {}, disabled = false, label }) {
  const buttons = KEYS.map((k) =>
    h(
      "button",
      {
        type: "button",
        class: "keypad__key",
        "data-key": k,
        "aria-label": hints[k] ? `${k}, ${hints[k]}` : k,
        onClick: () => onKey(k),
      },
      h("span", { class: "keypad__digit", "aria-hidden": "true" }, k),
      hints[k] ? h("span", { class: "keypad__hint", "aria-hidden": "true" }, hints[k]) : null,
    ),
  );
  const el = h("div", { class: "keypad", role: "group", "aria-label": label, dir: "ltr" }, buttons);
  function setDisabled(value) {
    for (const b of buttons) b.disabled = value;
  }
  setDisabled(disabled);
  return { el, setDisabled };
}
