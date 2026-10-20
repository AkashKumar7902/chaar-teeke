// Sign-in. Username and password, plain errors. In demo mode, "Look around without signing in" enters as a
// read-only visitor in one of four views. The page has no session, so it mounts the frame in "public" mode:
// brand, the staff language toggle and the DEMO strip.

import { api, ApiError, getMe } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { HOME, effectiveRole, mountFrame } from "/js/core/frame.js";
import { initI18n, t } from "/js/core/i18n.js";
import { errorSummary, field, icon, textInput, toast } from "/js/ui/index.js";
import { safeNextPath } from "./signin/next.js";

const VIEWS = ["clinic", "abc", "oversight", "reviewer"];
const main = document.getElementById("main");

/** Only a path on this site: the browser's own reading of ?next=, never another host. */
function safeNext() {
  return safeNextPath(new URLSearchParams(location.search).get("next"), location.origin);
}

function render(mode) {
  document.title = t("signin.page");

  const user = field({ label: t("signin.username"), control: textInput({ name: "username", autocomplete: "username", required: true }) });
  const pass = field({ label: t("signin.password"), control: textInput({ name: "password", type: "password", autocomplete: "current-password", required: true }) });
  user.control.setAttribute("autocapitalize", "none");
  user.control.setAttribute("spellcheck", "false");
  const summaryHost = h("div");
  const failure = h("div", { "aria-live": "assertive" });
  const submit = h("button", { type: "submit", class: "btn btn--primary btn--block" }, t("signin.submit"));

  const form = h("form", { class: "card stack", novalidate: true, onSubmit: onSubmit }, summaryHost, user.el, pass.el, failure, submit);

  async function onSubmit(event) {
    event.preventDefault();
    failure.replaceChildren();
    const problems = [];
    user.setError(null);
    pass.setError(null);
    if (!user.control.value.trim()) {
      user.setError(t("signin.required"));
      problems.push({ id: user.control.id, message: `${t("signin.username")}: ${t("signin.required").toLowerCase()}` });
    }
    if (!pass.control.value) {
      pass.setError(t("signin.required"));
      problems.push({ id: pass.control.id, message: `${t("signin.password")}: ${t("signin.required").toLowerCase()}` });
    }
    const summary = errorSummary(problems);
    summaryHost.replaceChildren(...(summary ? [summary] : []));
    if (summary) {
      summary.focus();
      return;
    }
    submit.setAttribute("aria-busy", "true");
    submit.disabled = true;
    try {
      const me = await api.post("/api/auth/signin", { username: user.control.value.trim(), password: pass.control.value });
      location.assign(safeNext() ?? HOME[effectiveRole(me)] ?? "/");
    } catch (err) {
      submit.removeAttribute("aria-busy");
      submit.disabled = false;
      const message = err instanceof ApiError && err.code === "bad_credentials" ? t("signin.wrong") : err instanceof ApiError ? err.message : t("errors.generic");
      failure.replaceChildren(h("p", { class: "field__error", role: "alert" }, icon("alert"), message));
      pass.control.value = "";
      pass.control.focus();
    }
  }

  const look =
    mode === "demo"
      ? h(
          "section",
          { class: "stack signin__look" },
          h("h2", { class: "section-title" }, t("signin.look.title")),
          h(
            "div",
            { class: "signin__views" },
            VIEWS.map((role) =>
              h(
                "button",
                {
                  type: "button",
                  class: "btn",
                  onClick: async (event) => {
                    const button = event.currentTarget;
                    button.setAttribute("aria-busy", "true");
                    try {
                      await api.post("/api/auth/visitor", { as: role });
                      location.assign(HOME[role]);
                    } catch (err) {
                      button.removeAttribute("aria-busy");
                      toast(err instanceof ApiError ? err.message : t("signin.look.failed"), { tone: "attention" });
                    }
                  },
                },
                t(`roles.${role}`),
              ),
            ),
          ),
          h("p", { class: "muted small" }, t("signin.look.note")),
        )
      : null;

  main.replaceChildren(h("h1", null, t("signin.title")), form, ...(look ? [look] : []));
  main.removeAttribute("aria-busy");
}

async function boot() {
  const [modeResponse] = await Promise.all([fetch("/api/mode", { credentials: "same-origin" }), initI18n({ bundles: ["common", "signin"], audience: "staff" })]);
  if (!modeResponse.ok) throw new ApiError(modeResponse.status, "bad_response", t("errors.server"));
  const { mode } = await modeResponse.json();
  try {
    const me = await getMe();
    if (me.role !== "visitor") {
      location.replace(safeNext() ?? HOME[effectiveRole(me)] ?? "/");
      return;
    }
  } catch (err) {
    if (!(err instanceof ApiError && err.isAuth)) throw err;
  }
  await mountFrame({ mode: "public" });
  render(mode);
  document.addEventListener("ct:langchange", () => render(mode));
}

boot().catch((err) => {
  console.error(err);
  main.removeAttribute("aria-busy");
  main.replaceChildren(h("p", { class: "field__error", role: "alert" }, icon("alert"), err instanceof ApiError ? err.message : "Chaar Teeke could not start. Reload the page."));
});
