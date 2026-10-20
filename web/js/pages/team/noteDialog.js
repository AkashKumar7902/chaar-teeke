// A small modal that asks for a note. Used to close an alert: "Acted" (note optional) and "Not acted" (a reason
// is required, and the server checks it too). Resolves with the text, or null when cancelled.

import { h, uid } from "/js/core/dom.js";
import { t } from "/js/core/i18n.js";
import { field } from "/js/ui/index.js";

export function noteDialog({ title, label, required, confirmLabel, maxlength = 300 }) {
  return new Promise((resolve) => {
    const titleId = uid("dlg");
    let result = null;
    const area = h("textarea", { class: "textarea", name: "note", rows: 3, maxlength });
    const f = field({ label, control: area });
    const save = () => {
      const text = area.value.trim();
      if (required && !text) {
        f.setError(t("alerts.dialog.needReason"));
        area.focus();
        return;
      }
      result = text;
      dialog.close();
    };
    const dialog = h(
      "dialog",
      { class: "dialog", "aria-labelledby": titleId },
      h("form", { method: "dialog", onSubmit: (e) => { e.preventDefault(); save(); } },
        h("div", { class: "dialog__body" }, h("h2", { id: titleId }, title), f.el),
        h(
          "div",
          { class: "dialog__foot" },
          h("button", { type: "button", class: "btn", onClick: () => dialog.close() }, t("buttons.cancel")),
          h("button", { type: "submit", class: "btn btn--primary" }, confirmLabel),
        ),
      ),
    );
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(result);
    });
    document.body.append(dialog);
    dialog.showModal();
    area.focus();
  });
}
