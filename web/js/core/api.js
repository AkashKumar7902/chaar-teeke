// Fetch wrapper. JSON in, JSON out, session cookie sent, X-CT-CSRF added on writes (the token comes from
// GET /api/me). Errors carry the server's own plain-words message. Nothing is retried or cached
// silently: a failed call throws an ApiError and the page says so where the person acts.
//
// Options on every call: `signal`, `timeoutMs`, `as` ("json" | "text"), `csrf`, and
//   `quiet: true`  the caller shows a refusal itself (a dialog), so the frame must not also toast the
//                  read-only 403. The call still throws; only the shared toast is skipped.

import { t } from "./i18n.js";

export class ApiError extends Error {
  /**
   * @param {number} status HTTP status; 0 for "never reached the server"
   * @param {string} code the server's error code, or "network" / "timeout" / "bad_response"
   * @param {string} message plain words, safe to show
   * @param {object|null} body the parsed error body, if any
   */
  constructor(status, code, message, body = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
  get isNetwork() {
    return this.status === 0;
  }
  get isReadOnly() {
    return this.status === 403 && this.code === "read_only";
  }
  get isForbidden() {
    return this.status === 403;
  }
  get isAuth() {
    return this.status === 401;
  }
}


const TIMEOUT_MS = 15000;
const errorListeners = new Set();

/** Hear about every failed call (the frame shows the read-only toast and the offline banner). */
export function onApiError(fn) {
  errorListeners.add(fn);
  return () => errorListeners.delete(fn);
}

function fail(error, path = null, quiet = false) {
  error.path = path;
  error.quiet = quiet;
  for (const fn of errorListeners) fn(error);
  return error;
}

let mePromise = null;
let meValue = null;
let modePromise = null;

function buildUrl(path, query) {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  const qs = params.toString();
  return qs ? `${path}${path.includes("?") ? "&" : "?"}${qs}` : path;
}

/** Sign-in, visitor entry and sign-out change who is signed in, so the cached /api/me (and its CSRF token) is stale after them. */
const AUTH_WRITE = /^\/api\/auth\/(signin|signout|visitor)$/;

/**
 * The CSRF token for a write. Sign-in and visitor entry happen before any session exists, so /api/me
 * answers 401 and there is no token: those two calls go without one and the server decides. Any other
 * write without a session fails with that 401.
 */
async function csrfFor(path, csrf) {
  if (csrf !== undefined) return csrf;
  // Sign-in and visitor entry come before any session: /api/mode says so without a red 401 from /api/me.
  if (AUTH_WRITE.test(path) && !(await getMode()).signed_in) return null;
  try {
    return (await getMe()).csrf;
  } catch (err) {
    if (err instanceof ApiError && err.isAuth && AUTH_WRITE.test(path)) return null;
    throw err;
  }
}

async function send(method, path, { body, query, signal, timeoutMs = TIMEOUT_MS, as = "json", csrf, quiet = false } = {}) {
  const headers = { Accept: as === "json" ? "application/json" : "*/*" };
  const init = { method, headers, credentials: "same-origin" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  if (method !== "GET" && method !== "HEAD") {
    const token = await csrfFor(path, csrf);
    if (token) headers["X-CT-CSRF"] = token;
  }
  if (signal?.aborted) throw signal.reason ?? new DOMException("aborted", "AbortError");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new DOMException("timeout", "TimeoutError")), timeoutMs);
  const onCallerAbort = () => controller.abort(signal.reason);
  signal?.addEventListener("abort", onCallerAbort, { once: true });
  init.signal = controller.signal;

  try {
    let response;
    try {
      response = await fetch(buildUrl(path, query), init);
    } catch (err) {
      if (signal?.aborted) throw err;
      if (controller.signal.reason?.name === "TimeoutError") throw fail(new ApiError(0, "timeout", t("errors.timeout")), path, quiet);
      throw fail(new ApiError(0, "network", t("errors.network")), path, quiet);
    }

    if (!response.ok) {
      let parsed = null;
      try {
        parsed = await response.json();
      } catch {
        parsed = null;
      }
      const code = typeof parsed?.error === "string" ? parsed.error : "bad_response";
      const message = typeof parsed?.message === "string" ? parsed.message : t("errors.server");
      throw fail(new ApiError(response.status, code, message, parsed), path, quiet);
    }
    if (AUTH_WRITE.test(path)) resetMe();
    if (response.status === 204) return null;
    try {
      return as === "text" ? await response.text() : await response.json();
    } catch (err) {
      if (signal?.aborted) throw err;
      if (controller.signal.reason?.name === "TimeoutError") throw fail(new ApiError(0, "timeout", t("errors.timeout")), path, quiet);
      throw fail(new ApiError(response.status, "bad_response", t("errors.server")), path, quiet);
    }
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onCallerAbort);
  }
}

export const api = {
  get: (path, query, options) => send("GET", path, { query, ...options }),
  text: (path, query, options) => send("GET", path, { query, as: "text", ...options }),
  post: (path, body = {}, options) => send("POST", path, { body, ...options }),
  put: (path, body = {}, options) => send("PUT", path, { body, ...options }),
  del: (path, options) => send("DELETE", path, options),
};

/**
 * GET /api/mode once per page: {mode, basemap, public_url, signed_in, view}. Public, so it answers with no
 * session and never with a 401. Ask it first to learn whether /api/me would be answered at all.
 */
export function getMode() {
  if (!modePromise) {
    modePromise = send("GET", "/api/mode").catch((err) => {
      modePromise = null;
      throw err;
    });
  }
  return modePromise;
}

/**
 * GET /api/me once per page; the CSRF token lives here. Call resetMe() after sign-in or sign-out.
 * /api/mode is asked first: when it says nobody is signed in this rejects with the 401 the server would give,
 * without sending the request, so an anonymous visitor's console stays clean.
 */
export function getMe() {
  if (!mePromise) {
    mePromise = getMode()
      .then((mode) => {
        if (!mode.signed_in) throw new ApiError(401, "signed_out", t("errors.signedOut"));
        return send("GET", "/api/me");
      })
      .then(
        (me) => {
          meValue = me;
          return me;
        },
        (err) => {
          mePromise = null;
          throw err;
        },
      );
  }
  return mePromise;
}

/** Forget who is signed in (the cached /api/me and /api/mode): call it after sign-in, visitor entry and sign-out. */
export function resetMe() {
  mePromise = null;
  meValue = null;
  modePromise = null;
}

/** The last /api/me answer, or null before it loaded. */
export function currentMe() {
  return meValue;
}
