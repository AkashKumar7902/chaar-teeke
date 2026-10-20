// Shared by the oversight pages (summary, weekly digest, method) and the review pages (queue, known names).
// Boot, one live data view with the cross-cutting states, and the text builders for shares and counts.
//
// The text builders take a translator `tr(key, vars)` and a language, so the screen (the page language) and
// the printed digest (English, Hindi or both at once) use the same code.

import { api, getMode } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { formatDate, formatTime } from "/js/core/format.js";
import { mountFrame } from "/js/core/frame.js";
import { currentLang, has, initI18n, t } from "/js/core/i18n.js";
import { sharedStream } from "/js/core/sse.js";
import { errorFor, errorState, icon, infoButton, skeleton } from "/js/ui/index.js";

/**
 * Load strings, mount the frame, set the page title, return who is here (null on an `open` page with nobody
 * signed in: the method page needs no login, so it gets the public frame there).
 */
export async function boot(bundles, titleKey, { open = false } = {}) {
  await initI18n({ bundles: ["common", ...bundles], audience: "staff" });
  const site = await getMode();
  const { me } = await mountFrame({ mode: open && !site.signed_in ? "public" : "staff" });
  const setTitle = () => {
    document.title = `${t(titleKey)} · ${t("frame.brand")}`;
  };
  setTitle();
  document.addEventListener("ct:langchange", setTitle);
  return me;
}

/** The role whose pages this person sees (a visitor sees the view they chose). */
export function viewOf(me) {
  return me.role === "visitor" ? (me.acting_as ?? "abc") : me.role;
}

export function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, ms);
  };
}

/** "Now" on the server's clock: the demo clock in demo mode, else the browser's. */
export async function serverNow(me) {
  if (me.mode !== "demo") return new Date();
  try {
    return new Date((await api.get("/api/demo/state")).now);
  } catch (err) {
    console.error(err);
    return new Date();
  }
}

/** "Last checked 10:12". */
export async function lastCheckedText(me) {
  return t("states.lastChecked", { when: formatTime(await serverNow(me)) });
}

/**
 * One data region with every cross-cutting state: skeleton after 300 ms (CSS), the server's own error with one
 * Retry, offline (no stale data: the region is replaced), 403, and a reload when the stream resyncs.
 *   const view = dataView(host, { load, render, isEmpty, events: ["report"] })
 * Language changes repaint from the data already held, without a request.
 */
export function dataView(host, { load, render, isEmpty = null, events = [], lines = 4, block = false }) {
  let ticket = 0;
  let data = null;
  let offline = false;

  function paint() {
    const node = (isEmpty ? isEmpty(data) : null) ?? render(data);
    // The same node again (a page that keeps its controls) is left where it is, so focus stays.
    if (host.childNodes.length !== 1 || host.firstChild !== node) host.replaceChildren(node);
  }

  async function reload({ quiet = false } = {}) {
    if (offline) {
      data = null;
      host.replaceChildren(errorState({ message: t("states.offline"), onRetry: () => reload() }));
      return;
    }
    const mine = (ticket += 1);
    if (!quiet || data === null) host.replaceChildren(skeleton({ lines, block }));
    host.setAttribute("aria-busy", "true");
    try {
      const next = await load();
      if (mine !== ticket) return;
      data = next;
      paint();
    } catch (err) {
      if (mine !== ticket) return;
      data = null;
      if (err?.name !== "ApiError") console.error(err);
      host.replaceChildren(errorFor(err, { onRetry: () => reload() }));
    } finally {
      if (mine === ticket) host.removeAttribute("aria-busy");
    }
  }

  const quietReload = debounce(() => reload({ quiet: true }), 600);
  const stream = sharedStream();
  for (const type of events) stream.on(type, quietReload);
  stream.onResync(() => reload({ quiet: true }));
  document.addEventListener("ct:offline", (event) => {
    offline = Boolean(event.detail?.offline);
    if (offline) {
      ticket += 1;
      data = null;
      host.replaceChildren(errorState({ message: t("states.offline"), onRetry: () => reload() }));
    } else reload({ quiet: true });
  });
  document.addEventListener("ct:langchange", () => {
    if (data !== null) paint();
  });
  reload();
  return {
    reload,
    get data() {
      return data;
    },
    paint,
  };
}

// ------------------------------------------------------------------ text builders (any language)
const LOCALES = { en: "en-IN-u-nu-latn", hi: "hi-IN-u-nu-latn" };

/** A count. "<5" passes through, "hidden" is translated, whole numbers get Indian grouping. */
export function count(tr, lang, value) {
  if (value === "hidden") return tr("format.hidden");
  if (typeof value === "string") return value;
  return new Intl.NumberFormat(LOCALES[lang] ?? LOCALES.en).format(value);
}

export const percent = (fraction) => `${Math.round(fraction * 100)}%`;
export const range = (tr, low, high) => tr("format.range", { low: Math.round(low * 100), high: Math.round(high * 100) });

/** The share on one line: percentage with interval and n, or "n = 11 of 25 needed", or "No calls yet". Never a bare percentage. */
export function shareText(tr, lang, share) {
  if (share.status === "ok" || share.status === "bracketed") {
    // the "n = 89" part never breaks across lines
    return tr("oversight.shareLine", {
      pct: percent(share.share),
      range: range(tr, share.ci_low, share.ci_high),
      n: count(tr, lang, share.n),
    }).replace(/n = (\S+)$/, "n\u00a0=\u00a0$1");
  }
  if (share.status === "too_few") {
    return tr("share.tooFew", { n: count(tr, lang, share.n), min: count(tr, lang, share.n + share.needed) });
  }
  return tr("states.noCalls");
}

/** "Could not see: 18 · Fewer than 50 answers" (nothing when there are no answers at all). */
export function shareNotes(tr, lang, share) {
  if (share.status === "no_data") return null;
  const parts = [tr("oversight.couldNotSee", { n: count(tr, lang, share.unsure) })];
  if (share.status === "bracketed") parts.push(tr("oversight.fewer50"));
  return parts.join(" · ");
}

/** The reference-band label in the sheet's language: the server's key in that language's common bundle (see core `claimLabel`). */
export function claimText(tr, coverage) {
  if (!coverage.claim_label_key) throw new Error("oversight: coverage has no claim_label_key");
  const [lo, hi] = coverage.claim_band;
  return tr(coverage.claim_label_key, { lo: Math.round(lo * 100), hi: Math.round(hi * 100) });
}

export const windowText = (tr, lang, start, end) =>
  tr("oversight.window", { from: formatDate(start, lang), to: formatDate(end, lang) });

/**
 * The server writes the completion basis as one English sentence. Read its numbers back so the page can say
 * the same thing in either language. Returns null for a sentence this code does not know.
 */
export function parseBasis(text) {
  let m = /^(<5|\d+) of (\d+) courses whose last dose date has passed were marked complete by the clinic \(95% interval (\d+)% to (\d+)%\); (<5|\d+|hidden) still running are left out\./.exec(text);
  if (m) return { kind: "rate", completed: numOrText(m[1]), ended: Number(m[2]), low: Number(m[3]) / 100, high: Number(m[4]) / 100, still: numOrText(m[5]) };
  m = /^n = (<5|\d+) of (\d+) needed: too few courses have reached/.exec(text);
  if (m) return { kind: "few", n: numOrText(m[1]), min: Number(m[2]) };
  if (/^No course has reached its last dose date yet\./.test(text)) return { kind: "none" };
  return null;
}
const numOrText = (v) => (/^\d+$/.test(v) ? Number(v) : v);

/** The basis in words: { rate: "73% interval 70 to 75" | null, text }. When the sentence is unknown the server's own words stand. */
export function completion(tr, lang, summary) {
  const parsed = parseBasis(summary.completion_basis);
  if (!parsed) {
    console.error(`oversight: unknown completion basis: ${summary.completion_basis}`);
    return { rate: null, interval: null, text: summary.completion_basis };
  }
  const noRegister = tr("oversight.noRegister");
  if (parsed.kind === "rate") {
    if (summary.completion_rate === null) throw new Error("oversight: a completion basis with a rate needs completion_rate");
    return {
      rate: percent(summary.completion_rate),
      interval: tr("oversight.interval", { range: range(tr, parsed.low, parsed.high) }),
      text: `${tr("oversight.basisRate", { done: count(tr, lang, parsed.completed), ended: count(tr, lang, parsed.ended), still: count(tr, lang, parsed.still) })} ${noRegister}`,
    };
  }
  if (parsed.kind === "few") {
    return { rate: null, interval: null, text: `${tr("oversight.basisFew", { n: count(tr, lang, parsed.n), min: count(tr, lang, parsed.min) })} ${noRegister}` };
  }
  return { rate: null, interval: null, text: `${tr("oversight.basisNone")} ${noRegister}` };
}

/** A count that is zero because nothing happened yet is said in words, never as "0". */
export function countOrNone(tr, lang, value, noneKey) {
  return value === 0 ? tr(noneKey) : count(tr, lang, value);
}

/** The (i) beside a heading: a short answer and a link to the method page. */
export function methodInfo({ topic, text, anchor }) {
  return infoButton({
    topic,
    title: t("buttons.about", { topic }),
    content: h("div", { class: "stack stack--s" }, [].concat(text).map((p) => h("p", null, p)), h("a", { href: `/method#${anchor}` }, t("oversight.methodLink"))),
  });
}

/** A plain link to a part of the method page. */
export function methodLink(anchor, label = t("oversight.methodLink")) {
  return h("a", { class: "method-link", href: `/method#${anchor}` }, icon("info"), label);
}

/** i18n keys numbered key.0, key.1 ... in order (a list of paragraphs). */
export function lines(prefix) {
  const out = [];
  for (let i = 0; has(`${prefix}.${i}`); i += 1) out.push(t(`${prefix}.${i}`));
  return out;
}

export { currentLang, h };
