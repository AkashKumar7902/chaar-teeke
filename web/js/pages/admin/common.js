// Shared by the five admin tabs. Nothing here draws a screen on its own.

import { ApiError, api, getMe, resetMe } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { t } from "/js/core/i18n.js";
import { sharedStream } from "/js/core/sse.js";
import { errorFor, errorState, icon, skeleton, toast, unsentNote } from "/js/ui/index.js";

export { ApiError, api, getMe, resetMe };

export const isOffline = () => document.documentElement.hasAttribute("data-offline");

/** Attributes for a button that writes. A visitor's button looks disabled and says why; pressing it only explains. */
export function writeAttrs(me) {
  return me.can_write ? {} : { "aria-disabled": "true", title: t("admin.readOnlyTitle") };
}

/** Wrap a write handler: a read-only role is told once, in the page's words, and nothing is sent. */
export function guarded(me, fn) {
  return (event) => {
    if (!me.can_write) {
      toast(t("states.forbidden"), { tone: "attention" });
      return undefined;
    }
    return fn(event);
  };
}

/** "Now" on the server's clock: the demo clock in demo mode, real time otherwise. */
export async function serverNow(me) {
  if (me.mode !== "demo") return new Date().toISOString();
  return (await api.get("/api/demo/state")).now;
}

/**
 * Run a write from a button. The button shows busy and cannot be pressed twice. A read-only refusal is
 * already toasted by the frame; any other refusal goes to `onError` (default: an amber toast).
 */
export async function runWrite(button, fn, { onError } = {}) {
  if (button?.getAttribute("aria-busy") === "true") return undefined;
  button?.setAttribute("aria-busy", "true");
  try {
    return await fn();
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    if (onError) onError(err);
    else if (!err.isReadOnly) toast(err.message, { tone: "attention" });
    return undefined;
  } finally {
    button?.removeAttribute("aria-busy");
  }
}

/** Keep keyboard focus on the same control (data-key) across a re-render. */
export function keepFocus(root, rebuild) {
  const key = root.contains(document.activeElement) ? document.activeElement.dataset?.key : null;
  rebuild();
  if (key) root.querySelector(`[data-key="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
}

/** A server refusal, in its own words: amber, an icon, never red. */
export function formError(message) {
  return h("p", { class: "inline-error", role: "alert" }, icon("alert"), h("span", null, message));
}

/**
 * One tab's data region with every cross-cutting state: skeleton after 300 ms (CSS), error with Retry,
 * offline (no stale data: the region is replaced), 403, and a quiet reload on the events named.
 *   const view = region(host, { load, render, events: ["system"], signal })
 * `render(data)` returns the node to show. A language change repaints from the data already held.
 */
export function region(host, { load, render, events = [], signal, lines = 5, block = false }) {
  let ticket = 0;
  let data = null;
  let offline = isOffline();
  let timer = null;

  function paint() {
    keepFocus(host, () => host.replaceChildren(render(data)));
  }

  function showOffline() {
    ticket += 1;
    data = null;
    host.removeAttribute("aria-busy");
    host.replaceChildren(errorState({ message: t("states.offline"), onRetry: () => reload() }));
  }

  async function reload({ quiet = false } = {}) {
    if (offline) return showOffline();
    const mine = (ticket += 1);
    if (!quiet || data === null) host.replaceChildren(skeleton({ lines, block }));
    host.setAttribute("aria-busy", "true");
    try {
      const next = await load();
      if (mine !== ticket) return undefined;
      data = next;
      paint();
    } catch (err) {
      if (mine !== ticket) return undefined;
      data = null;
      if (!(err instanceof ApiError)) console.error(err);
      host.replaceChildren(errorFor(err, { onRetry: () => reload() }));
    } finally {
      if (mine === ticket) host.removeAttribute("aria-busy");
    }
    return undefined;
  }

  const nudge = () => {
    clearTimeout(timer);
    timer = setTimeout(() => reload({ quiet: true }), 600);
  };
  const stream = sharedStream();
  const off = events.map((type) => stream.on(type, nudge));
  off.push(stream.onResync(() => reload({ quiet: true })));
  const onOffline = (event) => {
    offline = Boolean(event.detail?.offline);
    if (offline) showOffline();
    else reload({ quiet: true });
  };
  const onLang = () => {
    if (data !== null) paint();
  };
  document.addEventListener("ct:offline", onOffline);
  document.addEventListener("ct:langchange", onLang);
  signal?.addEventListener(
    "abort",
    () => {
      ticket += 1;
      clearTimeout(timer);
      off.forEach((fn) => fn());
      document.removeEventListener("ct:offline", onOffline);
      document.removeEventListener("ct:langchange", onLang);
    },
    { once: true },
  );
  reload();
  return {
    reload,
    get data() {
      return data;
    },
    paint,
  };
}

/**
 * A dialog whose confirm button can refuse (a field is wrong) or fail (the server said no) and stay open.
 * `body` is a Node; Enter in a field confirms. `onConfirm(button)` resolves true to close the dialog.
 * Same markup and classes as ui/dialog.js; native <dialog> traps focus and closes on Esc.
 */
export function formDialog({ title, body, confirmLabel, onConfirm, wide = false, cancelLabel = t("buttons.cancel") }) {
  return new Promise((resolve) => {
    const titleId = `dlg-${Math.random().toString(36).slice(2, 8)}`;
    let confirmed = false;
    const confirm = h("button", { type: "submit", class: "btn btn--primary" }, confirmLabel);
    const form = h(
      "form",
      { novalidate: true },
      h("div", { class: "dialog__body" }, h("h2", { id: titleId }, title), body),
      h("div", { class: "dialog__foot" }, h("button", { type: "button", class: "btn", onClick: () => dialog.close() }, cancelLabel), confirm),
    );
    const dialog = h("dialog", { class: ["dialog", wide ? "adm-dialog--wide" : ""], "aria-labelledby": titleId }, form);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (confirm.getAttribute("aria-busy") === "true") return;
      confirm.setAttribute("aria-busy", "true");
      try {
        if (await onConfirm(confirm)) {
          confirmed = true;
          dialog.close();
        } else {
          // A long form scrolls: bring the first refusal into view so a failed Save is never silent.
          const bad = dialog.querySelector('[aria-invalid="true"]');
          if (bad) {
            bad.focus({ preventScroll: true });
            (bad.closest(".field") ?? bad).scrollIntoView({ block: "center" });
          }
          else dialog.querySelector('[role="alert"]')?.scrollIntoView({ block: "nearest" });
        }
      } finally {
        confirm.removeAttribute("aria-busy");
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

/**
 * Show a server refusal in a form: the error slot gets the server's words, and a network failure also says
 * "Not sent" (what was typed stays). Returns true when the value is a refusal that was shown.
 */
export function showRefusal(slot, err, { field = null } = {}) {
  if (!(err instanceof ApiError)) throw err;
  slot.replaceChildren(formError(err.message), err.isNetwork ? unsentNote() : null);
  field?.setError?.(null);
  return true;
}

/** A password for a new sign-in or a reset: 12 letters and digits in three groups, no look-alike characters. */
export function makePassword() {
  const alphabet = "abcdefghjkmnpqrstuvwxyz23456789";
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  const chars = Array.from(bytes, (b) => alphabet[b % alphabet.length]);
  return [chars.slice(0, 4), chars.slice(4, 8), chars.slice(8, 12)].map((g) => g.join("")).join("-");
}

/** Shown once: the password in a box with a copy button. Resolves when the dialog is closed. */
export function passwordDialog({ title, lead, password }) {
  return new Promise((resolve) => {
    const titleId = `dlg-${Math.random().toString(36).slice(2, 8)}`;
    const status = h("p", { class: "muted", role: "status" });
    const box = h("input", { class: "input adm-secret", type: "text", readonly: true, value: password, "aria-label": t("admin.people.passwordLabel"), dir: "ltr" });
    const copy = h(
      "button",
      {
        type: "button",
        class: "btn",
        onClick: async () => {
          try {
            await navigator.clipboard.writeText(password);
            status.textContent = t("admin.people.copied");
          } catch {
            box.focus();
            box.select();
            status.textContent = t("admin.people.copyFailed");
          }
        },
      },
      t("admin.people.copy"),
    );
    const dialog = h(
      "dialog",
      { class: "dialog", "aria-labelledby": titleId },
      h("div", { class: "dialog__body" }, h("h2", { id: titleId }, title), h("p", null, lead), h("div", { class: "adm-secret-row" }, box, copy), status),
      h("div", { class: "dialog__foot" }, h("button", { type: "button", class: "btn btn--primary", onClick: () => dialog.close() }, t("admin.people.done"))),
    );
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve();
    });
    document.body.append(dialog);
    dialog.showModal();
    copy.focus();
  });
}
