// Native <dialog> with showModal(): focus is trapped, Esc closes, the page behind is inert.

import { h, uid } from "../core/dom.js";
import { t } from "../core/i18n.js";

/**
 * openDialog({title, body, actions}) -> Promise<value>. `actions` is [{label, value, primary?}]; the
 * promise resolves with the chosen value, or null when closed with Esc.
 */
export function openDialog({ title, body, actions }) {
  return new Promise((resolve) => {
    const titleId = uid("dlg");
    let result = null;
    const dialog = h(
      "dialog",
      { class: "dialog", "aria-labelledby": titleId },
      h(
        "div",
        { class: "dialog__body" },
        h("h2", { id: titleId }, title),
        body,
      ),
      h(
        "div",
        { class: "dialog__foot" },
        actions.map((a) =>
          h(
            "button",
            {
              type: "button",
              class: ["btn", a.primary ? "btn--primary" : ""],
              onClick: () => {
                result = a.value;
                dialog.close();
              },
            },
            a.label,
          ),
        ),
      ),
    );
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(result);
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}

/** confirmDialog({title, body, confirmLabel}) -> Promise<boolean>. The confirm button is the one filled button. */
export async function confirmDialog({ title, body, confirmLabel, cancelLabel = t("buttons.cancel") }) {
  const answer = await openDialog({
    title,
    body: typeof body === "string" ? h("p", null, body) : body,
    actions: [
      { label: cancelLabel, value: false },
      { label: confirmLabel, value: true, primary: true },
    ],
  });
  return answer === true;
}
