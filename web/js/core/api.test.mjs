import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

import { api, ApiError, currentMe, getMe, getMode, onApiError, resetMe } from "./api.js";
import { installBundle, resetBundles } from "./i18n.js";

const bundle = JSON.parse(readFileSync(new URL("../../i18n/common.en.json", import.meta.url), "utf8"));
const SIGNED_IN = { mode: "demo", basemap: "none", public_url: null, signed_in: true, view: "clinic" };
const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Install a fake fetch that answers from `routes` ("METHOD /path" -> Response | fn) and records every call. */
function fakeFetch(routes) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    const key = `${init.method} ${String(url).split("?")[0]}`;
    calls.push({ key, url: String(url), headers: init.headers, body: init.body, signal: init.signal });
    const route = routes[key];
    if (!route) throw new Error(`unexpected ${key}`);
    return typeof route === "function" ? route(init) : route.clone();
  };
  return calls;
}

test.beforeEach(() => {
  resetBundles();
  installBundle("common", "en", bundle);
  resetMe();
});

test("GET sends no CSRF header and never fetches /api/me", async () => {
  const calls = fakeFetch({ "GET /api/clinic/today": json(200, { due: [] }) });
  assert.deepEqual(await api.get("/api/clinic/today", { clinic_id: "cln_1", empty: "", none: null }), { due: [] });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/clinic/today?clinic_id=cln_1");
  assert.equal(calls[0].headers["X-CT-CSRF"], undefined);
});

test("a write carries the CSRF token from /api/me, fetched once", async () => {
  const calls = fakeFetch({
    "GET /api/mode": json(200, SIGNED_IN),
    "GET /api/me": json(200, { role: "clinic", csrf: "tok-1", mode: "demo" }),
    "POST /api/courses": json(200, { id: "crs_1" }),
    "POST /api/courses/crs_1/stop": json(200, {}),
  });
  await api.post("/api/courses", { a: 1 });
  await api.post("/api/courses/crs_1/stop", { reason: "moved" });
  assert.equal(calls.filter((c) => c.key === "GET /api/me").length, 1);
  const posts = calls.filter((c) => c.key.startsWith("POST"));
  assert.equal(posts[0].headers["X-CT-CSRF"], "tok-1");
  assert.equal(posts[1].headers["X-CT-CSRF"], "tok-1");
  assert.equal(posts[0].body, JSON.stringify({ a: 1 }));
  assert.equal(currentMe().role, "clinic");
});

test("sign-in works with no session: /api/mode says so, /api/me is never asked, no CSRF is sent", async () => {
  const calls = fakeFetch({
    "GET /api/mode": json(200, { mode: "demo", signed_in: false, view: null }),
    "POST /api/auth/signin": json(200, { ok: true }),
  });
  await api.post("/api/auth/signin", { username: "a", password: "b" });
  assert.deepEqual(calls.map((c) => c.key), ["GET /api/mode", "POST /api/auth/signin"]);
  assert.equal(calls[1].headers["X-CT-CSRF"], undefined);
  assert.equal(currentMe(), null);
});

test("signing in again while signed in sends the CSRF token of the old login, and forgets it after", async () => {
  const calls = fakeFetch({
    "GET /api/mode": json(200, SIGNED_IN),
    "GET /api/me": json(200, { csrf: "old" }),
    "POST /api/auth/visitor": json(200, { ok: true }),
  });
  await api.post("/api/auth/visitor", { as: "abc" });
  assert.equal(calls.find((c) => c.key === "POST /api/auth/visitor").headers["X-CT-CSRF"], "old");
  assert.equal(currentMe(), null);
});

test("/api/mode is asked once per page and forgotten when the login changes", async () => {
  const calls = fakeFetch({ "GET /api/mode": json(200, { mode: "demo", signed_in: false, view: null }) });
  assert.equal((await getMode()).signed_in, false);
  await getMode();
  assert.equal(calls.length, 1);
  resetMe();
  await getMode();
  assert.equal(calls.length, 2);
});

test("a failed /api/mode is not cached", async () => {
  let n = 0;
  globalThis.fetch = async () => {
    n += 1;
    if (n === 1) throw new TypeError("offline");
    return json(200, { signed_in: true });
  };
  await assert.rejects(getMode(), (e) => e.code === "network");
  assert.equal((await getMode()).signed_in, true);
});

test("any other write without a session fails with the 401 without asking /api/me, and is not sent", async () => {
  const calls = fakeFetch({ "GET /api/mode": json(200, { ...SIGNED_IN, signed_in: false, view: null }), "POST /api/courses": json(200, {}) });
  await assert.rejects(api.post("/api/courses", {}), (e) => e instanceof ApiError && e.isAuth && e.message === "Sign in to continue.");
  assert.deepEqual(calls.map((c) => c.key), ["GET /api/mode"]);
});

test("getMe asks the server only when /api/mode says somebody is signed in", async () => {
  const calls = fakeFetch({ "GET /api/mode": json(200, SIGNED_IN), "GET /api/me": json(401, { error: "signed_out", message: "Sign in to continue" }) });
  await assert.rejects(getMe(), (e) => e.isAuth); // the session ended since /api/mode: the server's own 401
  assert.deepEqual(calls.map((c) => c.key), ["GET /api/mode", "GET /api/me"]);
});

test("the server's own plain words and code come through", async () => {
  fakeFetch({
    "GET /api/mode": json(200, SIGNED_IN),
    "GET /api/me": json(200, { csrf: "t" }),
    "POST /api/courses": json(403, { error: "read_only", message: "Your role can't do this" }),
    "GET /api/x": json(500, "<html>boom</html>"),
  });
  const seen = [];
  const off = onApiError((e) => seen.push(e));
  await assert.rejects(api.post("/api/courses", {}), (e) => e.isReadOnly && e.isForbidden && e.message === "Your role can't do this" && e.path === "/api/courses");
  await assert.rejects(api.get("/api/x"), (e) => e.status === 500 && e.code === "bad_response" && e.message === "Something went wrong on the server.");
  off();
  assert.equal(seen.length, 2);
});

test("unreachable server is a network error, not an exception from fetch", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  await assert.rejects(api.get("/api/x"), (e) => e.isNetwork && e.code === "network" && /Can't reach/.test(e.message));
});

test("a slow server is a timeout, and says so", async () => {
  globalThis.fetch = (url, init) =>
    new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(init.signal.reason)));
  await assert.rejects(api.get("/api/x", undefined, { timeoutMs: 20 }), (e) => e.code === "timeout" && e.isNetwork && /too long/.test(e.message));
});

test("the caller's own abort is passed through untouched", async () => {
  globalThis.fetch = (url, init) => new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(init.signal.reason)));
  const ctl = new AbortController();
  const pending = api.get("/api/x", undefined, { signal: ctl.signal });
  ctl.abort(new DOMException("gone", "AbortError"));
  await assert.rejects(pending, (e) => e.name === "AbortError");
  const done = new AbortController();
  done.abort();
  await assert.rejects(api.get("/api/x", undefined, { signal: done.signal }), (e) => e.name === "AbortError");
});

test("a 200 that is not JSON is reported, not returned as empty", async () => {
  fakeFetch({ "GET /api/x": new Response("not json", { status: 200 }) });
  await assert.rejects(api.get("/api/x"), (e) => e.code === "bad_response");
});

test("a call marked quiet tells the frame not to toast its refusal; others do not", async () => {
  fakeFetch({
    "GET /api/mode": json(200, SIGNED_IN),
    "GET /api/me": json(200, { csrf: "t" }),
    "POST /api/a": json(403, { error: "read_only", message: "Your role can't do this" }),
    "POST /api/b": json(403, { error: "read_only", message: "Your role can't do this" }),
  });
  const seen = [];
  const off = onApiError((e) => seen.push([e.path, e.quiet]));
  await assert.rejects(api.post("/api/a", {}, { quiet: true }), (e) => e.isReadOnly && e.quiet === true);
  await assert.rejects(api.post("/api/b", {}), (e) => e.isReadOnly && e.quiet === false);
  off();
  assert.deepEqual(seen, [["/api/a", true], ["/api/b", false]]);
});
