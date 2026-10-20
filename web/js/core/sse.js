// Server-sent events with recovery (an EventSource closed by an HTTP error is never reopened by the
// browser). On error we close it ourselves and reopen after 5 s to 60 s of
// back-off with jitter, and call onResync after every reconnect so the page reloads what it missed.

export const BACKOFF_MIN_MS = 5000;
export const BACKOFF_MAX_MS = 60000;
/** The server sends `event: ping` every 15 s. A stream that has said nothing for 40 s is dead, even if the
 *  browser still thinks it is open (a half-open connection raises no error, so only silence gives it away). */
export const SILENCE_MS = 40000;

/** Event types the server sends (MODULES.md, "HTTP and SSE"). */
export const EVENT_TYPES = ["call", "missed_call", "report", "alert", "dose", "review", "clock", "system", "course", "callback"];
/** The keep-alive. It is heard (it resets the silence timer) but never handed to a page. */
export const PING_EVENT = "ping";

/**
 * Delay before reconnect number `attempt` (0 for the first). Pure, so it can be tested under node.
 * The ceiling doubles from 5 s up to 60 s and the delay is drawn between 5 s and that ceiling.
 * @param {number} attempt failed attempts so far, 0-based
 * @param {() => number} random returns [0, 1)
 */
export function backoffDelay(attempt, random = Math.random, min = BACKOFF_MIN_MS, max = BACKOFF_MAX_MS) {
  if (!Number.isInteger(attempt) || attempt < 0) throw new RangeError(`backoffDelay: bad attempt ${attempt}`);
  const ceiling = Math.min(max, min * 2 ** Math.min(attempt, 30));
  return Math.round(min + random() * (ceiling - min));
}

/**
 * One stream. Status is "connecting" | "live" | "reconnecting" | "closed".
 * `factory`, `timers` and `random` are injectable for tests.
 */
export class EventStream {
  constructor({
    url = "/api/events",
    types = EVENT_TYPES,
    factory = (u) => new EventSource(u, { withCredentials: true }),
    timers = { setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (id) => clearTimeout(id) },
    random = Math.random,
    now = () => Date.now(),
  } = {}) {
    this.url = url;
    this.types = types;
    this.factory = factory;
    this.timers = timers;
    this.random = random;
    this.now = now;
    this.status = "closed";
    this.attempt = 0;
    this.needsResync = false; // set when a connection was lost; cleared by the next open
    this.es = null;
    this.timer = null;
    this.silence = null; // timer that fires when nothing (not even a ping) has arrived for SILENCE_MS
    this.heardAt = null; // when the last event, ping or open arrived
    this.handlers = new Map(); // type -> Set<fn>
    this.statusListeners = new Set();
    this.resyncListeners = new Set();
    this.errorListeners = new Set();
  }

  /** Listen to one event type ("*" for all). Returns an unsubscribe function. */
  on(type, fn) {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set());
    this.handlers.get(type).add(fn);
    return () => this.handlers.get(type)?.delete(fn);
  }

  onStatus(fn) {
    this.statusListeners.add(fn);
    fn(this.status, { attempt: this.attempt, retryInMs: null });
    return () => this.statusListeners.delete(fn);
  }

  /** Called after every reconnect (an open that follows a lost connection, not a clean first open): reload lists, counts and the clock. */
  onResync(fn) {
    this.resyncListeners.add(fn);
    return () => this.resyncListeners.delete(fn);
  }

  /** Called with an Error when an event could not be read. Never swallowed silently. */
  onError(fn) {
    this.errorListeners.add(fn);
    return () => this.errorListeners.delete(fn);
  }

  setStatus(status, extra = {}) {
    this.status = status;
    for (const fn of this.statusListeners) fn(status, { attempt: this.attempt, retryInMs: null, ...extra });
  }

  /** `quiet`: the page came back or the network did, so a reopen is not a failure and must not show the offline banner. */
  open({ quiet = false } = {}) {
    if (this.es || this.timer !== null) return this;
    this.setStatus(this.status === "reconnecting" || (this.needsResync && !quiet) ? "reconnecting" : "connecting");
    const es = this.factory(this.url);
    this.es = es;
    this.heard(); // the silence timer runs from the moment we ask, so a connection that never opens is also caught
    es.onopen = () => {
      const reconnect = this.needsResync;
      this.needsResync = false;
      this.attempt = 0;
      this.heard();
      this.setStatus("live");
      if (reconnect) for (const fn of this.resyncListeners) fn();
    };
    es.onerror = () => this.lost();
    for (const type of this.types) es.addEventListener(type, (event) => this.deliver(type, event));
    es.addEventListener(PING_EVENT, () => this.heard());
    return this;
  }

  /** Something arrived: note when, and start the silence timer again. */
  heard() {
    this.heardAt = this.now();
    if (this.silence !== null) this.timers.clearTimeout(this.silence);
    this.silence = this.timers.setTimeout(() => {
      this.silence = null;
      if (this.es) this.lost();
    }, SILENCE_MS);
  }

  stopSilenceTimer() {
    if (this.silence !== null) this.timers.clearTimeout(this.silence);
    this.silence = null;
  }

  /**
   * The tab became visible again. Browsers slow timers in a hidden tab, so the silence timer may not have
   * fired: if nothing has been heard for SILENCE_MS, treat the stream as lost and reopen at once.
   */
  onVisible() {
    if (this.es && this.heardAt !== null && this.now() - this.heardAt >= SILENCE_MS) this.reopenNow();
  }

  /** The network came back. Whatever the stream looked like before, it is not to be trusted: reopen and resync. */
  onOnline() {
    this.reopenNow();
  }

  /** Drop the current connection (or a waiting retry) and open a new one now, then resync what was missed. */
  reopenNow() {
    if (this.status === "closed" && !this.es && this.timer === null) return; // never opened, or closed on purpose
    if (this.timer !== null) this.timers.clearTimeout(this.timer);
    this.timer = null;
    this.stopSilenceTimer();
    this.es?.close();
    this.es = null;
    this.needsResync = true;
    this.open({ quiet: true });
  }

  deliver(type, event) {
    this.heard();
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (err) {
      const error = new Error(`events: unreadable ${type} event`, { cause: err });
      for (const fn of this.errorListeners) fn(error);
      if (this.errorListeners.size === 0) console.error(error);
      return;
    }
    for (const key of [type, "*"]) for (const fn of this.handlers.get(key) ?? []) fn(payload, type);
  }

  lost() {
    this.stopSilenceTimer();
    this.es?.close();
    this.es = null;
    this.needsResync = true;
    const delay = backoffDelay(this.attempt, this.random);
    this.attempt += 1;
    this.setStatus("reconnecting", { retryInMs: delay });
    this.timer = this.timers.setTimeout(() => {
      this.timer = null;
      this.open();
    }, delay);
  }

  close() {
    if (this.timer !== null) this.timers.clearTimeout(this.timer);
    this.timer = null;
    this.stopSilenceTimer();
    this.es?.close();
    this.es = null;
    this.setStatus("closed");
  }
}

/**
 * A stream that keeps failing may be failing because the login ended (the server answers 401 and the
 * browser never explains). From the `failures`-th failed attempt on, ask /api/mode, which never answers 401:
 * if it says nobody is signed in, call `onSignedOut` (the frame goes to /signin) instead of retrying
 * forever. If /api/mode cannot be reached either, the offline banner already says so, and the next failed
 * attempt asks again. Returns an unsubscribe function.
 * @param {EventStream} stream
 * @param {{fetchMode: () => Promise<{signed_in: boolean}>, onSignedOut: () => void, failures?: number}} options
 */
export function leaveWhenSignedOut(stream, { fetchMode, onSignedOut, failures = 2 }) {
  let asking = false;
  return stream.onStatus((status, info) => {
    // retryInMs is set only when an attempt has just failed (reopening reports "reconnecting" again without it)
    if (status !== "reconnecting" || info.retryInMs == null || info.attempt < failures || asking) return;
    asking = true;
    fetchMode()
      .then((mode) => {
        if (mode.signed_in === false) onSignedOut();
      })
      .catch(() => {})
      .finally(() => {
        asking = false;
      });
  });
}

let shared = null;
/** The one stream per page; the frame and the page both listen to it. */
export function sharedStream() {
  if (!shared) {
    shared = new EventStream().open();
    // a hidden tab or a changed network is when a half-open stream goes unnoticed: look again when they return
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") shared.onVisible();
    });
    window.addEventListener("online", () => shared.onOnline());
  }
  return shared;
}
