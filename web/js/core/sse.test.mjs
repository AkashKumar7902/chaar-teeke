import assert from "node:assert/strict";
import test from "node:test";

import { BACKOFF_MAX_MS, BACKOFF_MIN_MS, EVENT_TYPES, EventStream, PING_EVENT, SILENCE_MS, backoffDelay, leaveWhenSignedOut } from "./sse.js";

test("the first retry waits exactly 5 s", () => {
  assert.equal(backoffDelay(0, () => 0), BACKOFF_MIN_MS);
  assert.equal(backoffDelay(0, () => 0.999), BACKOFF_MIN_MS);
});

test("the ceiling doubles from 5 s up to 60 s", () => {
  const top = (attempt) => backoffDelay(attempt, () => 0.999999);
  assert.ok(top(1) <= 10000 && top(1) > 9990);
  assert.ok(top(2) <= 20000 && top(2) > 19990);
  assert.ok(top(3) <= 40000);
  assert.equal(top(4), BACKOFF_MAX_MS);
  assert.equal(top(50), BACKOFF_MAX_MS);
});

test("the delay is always between 5 s and 60 s and uses jitter", () => {
  const seen = new Set();
  for (let attempt = 0; attempt < 12; attempt += 1) {
    for (const r of [0, 0.1, 0.5, 0.9, 0.999999]) {
      const d = backoffDelay(attempt, () => r);
      assert.ok(d >= BACKOFF_MIN_MS && d <= BACKOFF_MAX_MS, `attempt ${attempt} r ${r} gave ${d}`);
      seen.add(`${attempt}:${d}`);
    }
  }
  assert.ok(backoffDelay(5, () => 0.2) !== backoffDelay(5, () => 0.8));
  assert.ok(seen.size > 20);
});

test("a bad attempt number is refused", () => {
  assert.throws(() => backoffDelay(-1), RangeError);
  assert.throws(() => backoffDelay(1.5), RangeError);
});

// ---- the stream, with a fake EventSource and fake timers
function harness() {
  const sources = [];
  const timers = []; // the reconnect timer
  const silence = []; // the silence timer, kept apart so the retry tests read the same as before
  let clockMs = 0;
  class FakeES {
    constructor(url) {
      this.url = url;
      this.closed = false;
      this.listeners = new Map();
      sources.push(this);
    }
    addEventListener(type, fn) {
      this.listeners.set(type, fn);
    }
    close() {
      this.closed = true;
    }
    emit(type, data) {
      this.listeners.get(type)({ data: JSON.stringify(data) });
    }
    ping() {
      this.listeners.get(PING_EVENT)({ data: "" });
    }
  }
  const stream = new EventStream({
    factory: (u) => new FakeES(u),
    timers: {
      setTimeout: (fn, ms) => {
        const rec = { fn, ms, cleared: false };
        (ms === SILENCE_MS ? silence : timers).push(rec);
        return rec;
      },
      clearTimeout: (rec) => {
        rec.cleared = true;
      },
    },
    random: () => 0.5,
    now: () => clockMs,
  });
  return { stream, sources, timers, silence, advance: (ms) => (clockMs += ms) };
}

test("an error closes the source, waits, reopens, and resyncs once", () => {
  const { stream, sources, timers } = harness();
  const statuses = [];
  let resyncs = 0;
  stream.onStatus((s) => statuses.push(s));
  stream.onResync(() => (resyncs += 1));
  stream.open();
  sources[0].onopen();
  assert.equal(stream.status, "live");
  assert.equal(resyncs, 0, "the first open is not a resync");

  sources[0].onerror();
  assert.ok(sources[0].closed, "the failed EventSource is closed by us");
  assert.equal(stream.status, "reconnecting");
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, BACKOFF_MIN_MS);

  timers[0].fn();
  assert.equal(sources.length, 2);
  sources[1].onopen();
  assert.equal(stream.status, "live");
  assert.equal(resyncs, 1);
  assert.equal(stream.attempt, 0, "a successful open resets the back-off");
  assert.deepEqual(statuses.filter((s, i, a) => s !== a[i - 1]), ["closed", "connecting", "live", "reconnecting", "live"]);
});

test("repeated failures back off further and further, a first-connect failure still resyncs", () => {
  const { stream, sources, timers } = harness();
  let resyncs = 0;
  stream.onResync(() => (resyncs += 1));
  stream.open();
  for (let i = 0; i < 6; i += 1) {
    sources[i].onerror();
    timers[i].fn();
  }
  const waits = timers.map((t) => t.ms);
  assert.equal(waits[0], 5000);
  assert.ok(waits[3] > waits[1]);
  assert.ok(waits.every((w) => w >= 5000 && w <= 60000));
  sources[6].onopen();
  assert.equal(resyncs, 1);
});

test("events reach their handlers, '*' hears all, and bad JSON is reported not swallowed", () => {
  const { stream, sources } = harness();
  const got = [];
  const errors = [];
  stream.on("alert", (p) => got.push(["alert", p.id]));
  stream.on("*", (p, type) => got.push(["*", type]));
  stream.onError((e) => errors.push(e.message));
  stream.open();
  sources[0].emit("alert", { id: "e1", type: "alert", data: {} });
  sources[0].listeners.get("clock")({ data: "{not json" });
  assert.deepEqual(got, [["alert", "e1"], ["*", "alert"]]);
  assert.equal(errors.length, 1);
});

test("close() stops everything", () => {
  const { stream, sources, timers } = harness();
  stream.open();
  sources[0].onerror();
  stream.close();
  assert.equal(stream.status, "closed");
  assert.equal(stream.timer, null);
  assert.ok(timers.length === 1);
});

test("the stream listens for the call-back event as well", () => {
  assert.ok(EVENT_TYPES.includes("callback"));
  const { stream, sources } = harness();
  const got = [];
  stream.on("callback", (p) => got.push(p.data.status));
  stream.open();
  sources[0].emit("callback", { id: 1, type: "callback", data: { callback_id: "cb_1", alert_id: "a", status: "open" } });
  assert.deepEqual(got, ["open"]);
});

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("a stream that keeps failing with nobody signed in sends the page to sign-in, once", async () => {
  const { stream, sources, timers } = harness();
  let asked = 0;
  let left = 0;
  leaveWhenSignedOut(stream, { fetchMode: async () => (asked += 1, { signed_in: false }), onSignedOut: () => (left += 1) });
  stream.open();
  sources[0].onerror();
  await flush();
  assert.equal(asked, 0, "one failure is not 'repeatedly'");
  timers[0].fn();
  sources[1].onerror();
  await flush();
  assert.equal(asked, 1);
  assert.equal(left, 1);
});

test("a stream that fails while the login is fine keeps retrying and does not leave", async () => {
  const { stream, sources, timers } = harness();
  let left = 0;
  leaveWhenSignedOut(stream, { fetchMode: async () => ({ signed_in: true }), onSignedOut: () => (left += 1) });
  stream.open();
  for (let i = 0; i < 4; i += 1) {
    sources[i].onerror();
    await flush();
    timers[i].fn();
  }
  assert.equal(left, 0);
  assert.equal(sources.length, 5, "it is still reconnecting");
});

test("if /api/mode cannot be reached the page stays and asks again on the next failure", async () => {
  const { stream, sources, timers } = harness();
  let asked = 0;
  let left = 0;
  const answers = [() => Promise.reject(new Error("offline")), async () => ({ signed_in: false })];
  leaveWhenSignedOut(stream, { fetchMode: () => answers[Math.min(asked++, 1)](), onSignedOut: () => (left += 1) });
  stream.open();
  for (let i = 0; i < 3; i += 1) {
    sources[i].onerror();
    await flush();
    timers[i].fn();
  }
  assert.equal(asked, 2);
  assert.equal(left, 1);
});

test("a second check is not started while the first is still waiting", async () => {
  const { stream, sources, timers } = harness();
  let asked = 0;
  let release;
  leaveWhenSignedOut(stream, { fetchMode: () => (asked += 1, new Promise((r) => (release = r))), onSignedOut: () => {} });
  stream.open();
  for (let i = 0; i < 4; i += 1) {
    sources[i].onerror();
    timers[i].fn();
  }
  assert.equal(asked, 1);
  release({ signed_in: true });
  await flush();
});

// ---- half-open streams: silence, visibility, network

const live = (h) => {
  h.stream.open();
  h.sources[0].onopen();
};
const armed = (h) => h.silence.filter((t) => !t.cleared);

test("silence for 40 s is a lost stream: closed, banner on, reopened after the back-off", () => {
  const h = harness();
  const statuses = [];
  h.stream.onStatus((s) => statuses.push(s));
  live(h);
  assert.equal(armed(h).length, 1, "one silence timer is running");
  armed(h)[0].fn();
  assert.ok(h.sources[0].closed, "the quiet connection is closed by us");
  assert.equal(h.stream.status, "reconnecting");
  assert.equal(h.timers.length, 1);
  assert.equal(h.timers[0].ms, BACKOFF_MIN_MS);
  h.timers[0].fn();
  h.sources[1].onopen();
  assert.equal(h.stream.status, "live");
  assert.ok(statuses.includes("reconnecting") && statuses.at(-1) === "live");
});

test("a ping and any event start the silence timer again, and a ping reaches no page", () => {
  const h = harness();
  const got = [];
  h.stream.on("*", (p, type) => got.push(type));
  live(h);
  const first = armed(h)[0];
  h.sources[0].ping();
  assert.equal(first.cleared, true, "the ping restarted the timer");
  const second = armed(h)[0];
  h.sources[0].emit("clock", { id: 1, type: "clock", data: {} });
  assert.equal(second.cleared, true, "an ordinary event does too");
  assert.deepEqual(got, ["clock"], "only real events are handed on");
  assert.equal(armed(h).length, 1);
});

test("the silence timer is stopped when the stream is lost on its own or closed", () => {
  const h = harness();
  live(h);
  h.sources[0].onerror();
  assert.equal(armed(h).length, 0);
  h.timers[0].fn();
  assert.equal(armed(h).length, 1, "reopening starts it again");
  h.stream.close();
  assert.equal(armed(h).length, 0);
});

test("a tab that comes back after more than 40 s of silence reopens at once, without the offline banner, and resyncs", () => {
  const h = harness();
  const statuses = [];
  let resyncs = 0;
  live(h);
  h.stream.onStatus((s) => statuses.push(s));
  h.stream.onResync(() => (resyncs += 1));
  h.advance(SILENCE_MS - 1);
  h.stream.onVisible();
  assert.equal(h.sources.length, 1, "39 s of quiet is still a healthy stream");
  h.advance(2);
  h.stream.onVisible();
  assert.ok(h.sources[0].closed);
  assert.equal(h.sources.length, 2, "reopened now, no waiting for a back-off");
  assert.equal(h.timers.length, 0);
  assert.ok(!statuses.includes("reconnecting"), "a hidden tab is not an outage");
  h.sources[1].onopen();
  assert.equal(resyncs, 1);
});

test("coming back online reopens whatever the stream looked like, and resyncs", () => {
  const h = harness();
  let resyncs = 0;
  live(h);
  h.stream.onResync(() => (resyncs += 1));
  h.stream.onOnline();
  assert.ok(h.sources[0].closed);
  assert.equal(h.sources.length, 2);
  h.sources[1].onopen();
  assert.equal(resyncs, 1);
  assert.equal(h.stream.status, "live");
});

test("online while a retry is waiting cancels the wait and retries now", () => {
  const h = harness();
  live(h);
  h.sources[0].onerror();
  assert.equal(h.stream.status, "reconnecting");
  h.stream.onOnline();
  assert.equal(h.timers[0].cleared, true);
  assert.equal(h.sources.length, 2);
  assert.equal(h.stream.status, "reconnecting", "still offline until it opens");
  h.sources[1].onopen();
  assert.equal(h.stream.status, "live");
});

test("a stream that was never opened or was closed on purpose is left alone by online and visible", () => {
  const h = harness();
  h.stream.onOnline();
  h.stream.onVisible();
  assert.equal(h.sources.length, 0);
  live(h);
  h.stream.close();
  h.stream.onOnline();
  assert.equal(h.sources.length, 1);
});
