// Big - / + stepper for counts (the sight-count form). A number input sits between the buttons so the
// value can also be typed. Nothing is saved until the page sends it.

import { h, uid } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { icon } from "./icons.js";

/** stepper({label, value, min, max, step, onChange}) -> {el, get, set} */
export function stepper({ label, value = 0, min = 0, max = 999, step = 1, onChange }) {
  const id = uid("step");
  let current = clamp(value);

  function clamp(n) {
    return Math.min(max, Math.max(min, Number.isFinite(n) ? Math.round(n) : min));
  }

  const input = h("input", {
    class: "stepper__input",
    id,
    type: "number",
    inputmode: "numeric",
    min,
    max,
    step,
    value: current,
    onInput: () => commit(Number(input.value), { fromInput: true }),
    onChange: () => commit(Number(input.value)),
  });
  const less = h(
    "button",
    { type: "button", class: "stepper__btn", "aria-label": t("buttons.fewer", { label }), onClick: () => commit(current - step) },
    icon("minus"),
  );
  const more = h(
    "button",
    { type: "button", class: "stepper__btn", "aria-label": t("buttons.more", { label }), onClick: () => commit(current + step) },
    icon("plus"),
  );

  function commit(n, { fromInput = false } = {}) {
    const next = clamp(n);
    if (!fromInput || String(next) === input.value) input.value = String(next);
    const changed = next !== current;
    current = next;
    less.disabled = current <= min;
    more.disabled = current >= max;
    if (changed) onChange?.(current);
  }

  const el = h(
    "div",
    { class: "stepper" },
    h("label", { class: "stepper__label", for: id }, label),
    h("div", { class: "stepper__ctl" }, less, input, more),
  );
  commit(current);
  return { el, get: () => current, set: (n) => commit(n) };
}
