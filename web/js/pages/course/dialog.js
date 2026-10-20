// A dialog whose confirm button can refuse (a missing reason) or fail (the server said no) and stay open.
// Same markup and classes as ui/dialog.js; native <dialog> traps focus and closes on Esc.

import { h, uid } from "/js/core/dom.js";
import { t } from "/js/core/i18n.js";

/** formDialog({title, body, confirmLabel, onConfirm(button) -> Promise<boolean>}) -> Promise<boolean> (true when confirmed). */
export function formDialog({ title, body, confirmLabel, onConfirm }) {
  return new Promise((resolve) => {
    const titleId = uid("dlg");
    let confirmed = false;
    const confirm = h("button", { type: "button", class: "btn btn--primary" }, confirmLabel);
    const dialog = h(
      "dialog",
      { class: "dialog", "aria-labelledby": titleId },
      h("div", { class: "dialog__body" }, h("h2", { id: titleId }, title), body),
      h("div", { class: "dialog__foot" }, h("button", { type: "button", class: "btn", onClick: () => dialog.close() }, t("buttons.cancel")), confirm),
    );
    confirm.addEventListener("click", async () => {
      if (await onConfirm(confirm)) {
        confirmed = true;
        dialog.close();
      }
    });
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(confirmed);
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}
