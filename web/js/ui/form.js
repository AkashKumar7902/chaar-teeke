// Form fields with inline errors and an error summary. Errors are amber (never red) with an icon and words.
// Typed input is never cleared by an error; a failed send is marked "Not sent" and the values stay.

import { append, h, uid } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { icon } from "./icons.js";

export function textInput({ name, value = "", type = "text", inputmode, autocomplete, required = false, maxlength, placeholder, disabled = false } = {}) {
  return h("input", { class: "input", name, type, value, inputmode, autocomplete, required: required || null, maxlength, placeholder, disabled: disabled || null });
}

export function selectInput({ name, options, value, disabled = false }) {
  return h(
    "select",
    { class: "select", name, disabled: disabled || null },
    options.map((o) => h("option", { value: o.value, selected: o.value === value ? true : null }, o.label)),
  );
}

/**
 * field({label, hint, control, error}) -> {el, control, setError(message|null)}.
 * `control` is a Node from textInput/selectInput or your own; it gets the id, hint and error links.
 */
export function field({ label, hint = null, control, error = null }) {
  const id = control.id || (control.id = uid("f"));
  const hintId = `${id}-hint`;
  const errId = `${id}-err`;
  const wrap = h("div", { class: "field" });
  const labelEl = h("label", { class: "field__label", for: id }, label);
  const hintEl = hint ? h("p", { class: "field__hint", id: hintId }, hint) : null;
  let errEl = null;

  function describe() {
    const ids = [hintEl ? hintId : null, errEl ? errId : null].filter(Boolean).join(" ");
    if (ids) control.setAttribute("aria-describedby", ids);
    else control.removeAttribute("aria-describedby");
  }
  function setError(message) {
    errEl?.remove();
    errEl = null;
    wrap.classList.toggle("field--error", Boolean(message));
    if (message) {
      errEl = h("p", { class: "field__error", id: errId }, icon("alert"), message);
      control.setAttribute("aria-invalid", "true");
      wrap.append(errEl);
    } else {
      control.removeAttribute("aria-invalid");
    }
    describe();
  }
  append(wrap, labelEl, hintEl, control);
  setError(error);
  return { el: wrap, control, setError };
}

/** errorSummary([{id, message}]) -> Node | null. Put it at the top of the form and call .focus() on it. */
export function errorSummary(errors) {
  if (errors.length === 0) return null;
  const el = h(
    "div",
    { class: "error-summary", role: "alert", tabindex: "-1" },
    h("h2", null, icon("alert"), t("form.problem", { count: errors.length })),
    h(
      "ul",
      null,
      errors.map((e) =>
        h(
          "li",
          null,
          h(
            "a",
            {
              href: `#${e.id}`,
              onClick: (event) => {
                event.preventDefault();
                document.getElementById(e.id)?.focus();
              },
            },
            e.message,
          ),
        ),
      ),
    ),
  );
  return el;
}

/** "Not sent" next to a form whose send failed. The typed values stay on screen. */
export function unsentNote() {
  return h("p", { class: "field__unsent", role: "status" }, icon("alert", { className: "" }), " ", t("states.notSent"));
}

export function checkbox({ name, label, checked = false }) {
  const id = uid("c");
  return h("label", { class: "check", for: id }, h("input", { type: "checkbox", id, name, checked: checked || null }), h("span", null, label));
}
