// Landing (J1). In demo mode a person with no session enters as a visitor viewing the ABC team and sees a
// working product at once: the city-wide notched share with its interval, a compact cluster map, a live
// feed from the event stream, the "Try the call" card with its QR, and buttons to see it as another role.
// In pilot mode this page sends people to /signin. Nothing here writes data: a visitor can only look.

import { api, ApiError, getMe } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { formatDate, formatDayTime, formatTime } from "/js/core/format.js";
import { HOME, effectiveRole, mountFrame } from "/js/core/frame.js";
import { currentLang, initI18n, t } from "/js/core/i18n.js";
import { qrCode } from "/js/core/qr.js";
import { sharedStream } from "/js/core/sse.js";
import { demoDataInfo, emptyState, errorFor, icon, infoButton, pill, shareDisplay, skeleton, toast } from "/js/ui/index.js";
import { mapLegend, mapView } from "/js/ui/mapView.js";

const FEED_MAX = 6; // how many rows the list shows; GET /api/feed is capped by policy feed.max_items to the same
const REFRESH_AFTER_EVENT_MS = 1500;
const SEE_AS = ["clinic", "abc", "oversight", "reviewer"];
// A place that a reviewer has still to decide is not "not known": the word differs, and no ward is guessed.
const PLACE_WAITING = new Set(["needs_review", "pending"]);
// The id of the thing an event is about, so a live row and the same row read again after a reload are one row.
// An alert is not here: its row comes from GET /api/feed, which knows its day, its place and whether it is real.
const ENTITY_ID = { report: (d) => d.report_id, missed_call: (d) => d.id, call: (d) => d.call_id };

const main = document.getElementById("main");

// ------------------------------------------------------------------ boot
async function readMode() {
  const response = await fetch("/api/mode", { credentials: "same-origin" });
  if (!response.ok) throw new ApiError(response.status, "bad_response", `GET /api/mode answered ${response.status}`);
  return response.json();
}

/** A visitor viewing the ABC team; a signed-in person goes to their own home instead. */
async function enter() {
  let me = null;
  try {
    me = await getMe();
  } catch (err) {
    if (!(err instanceof ApiError && err.isAuth)) throw err;
  }
  if (me && me.role !== "visitor") return { redirect: HOME[effectiveRole(me)] ?? "/" };
  if (!me || me.acting_as !== "abc") await api.post("/api/auth/visitor", { as: "abc" });
  return { redirect: null };
}

function failed(err, retry) {
  main.removeAttribute("aria-busy");
  main.replaceChildren(h("h1", null, t("landing.head.title")), errorFor(err, { onRetry: retry }));
}

async function boot() {
  let mode;
  try {
    [mode] = await Promise.all([readMode(), initI18n({ bundles: ["common", "landing"], audience: "staff" })]);
  } catch (err) {
    console.error(err);
    main.removeAttribute("aria-busy");
    main.replaceChildren(h("p", { role: "alert" }, "Chaar Teeke could not start. Reload the page."));
    return;
  }
  if (mode.mode !== "demo") {
    location.replace("/signin");
    return;
  }
  document.title = t("landing.page.title");
  try {
    const { redirect } = await enter();
    if (redirect) {
      location.replace(redirect);
      return;
    }
    await mountFrame({ mode: "staff" });
  } catch (err) {
    failed(err, () => location.reload());
    return;
  }
  start(mode);
}

// ------------------------------------------------------------------ page
function start(mode) {
  const state = {
    coverage: null, // Coverage, once loaded
    coverageError: null,
    feedError: null,
    feed: [], // newest first: {id, at, day, kind, clusterId, wardNo, wardName, animal, locationStatus, simulated}
    feedLoaded: false,
    checkedAt: new Date(),
    demoNow: null, // the demo clock, read only for the "No calls yet" share
  };
  let ui = null; // the hosts of the current build
  let mapHandle = null;
  let ticket = 0;
  let refreshTimer = null;

  // The share and the feed read different routes. When both fail, the failure is said once, above, with one
  // Retry that tries both; the feed section waits behind it.
  const bothFailed = () => Boolean(state.coverageError && state.feedError && !state.coverage && !state.feedLoaded);
  const retryAll = () => {
    loadCoverage();
    loadFeed();
  };

  const clusterName = (id) => state.coverage?.clusters.find((c) => c.id === id)?.name ?? null;

  // ----------------------------------------------------------------- layout
  function build() {
    mapHandle?.destroy();
    mapHandle = null;
    ui = {
      share: h("div"),
      map: h("div"),
      mapSection: null,
      look: h("div", { class: "landing__look" }),
      call: h("div"),
      see: h("div"),
      feed: h("div", { "aria-live": "polite" }),
      feedSection: null,
    };
    main.replaceChildren(
      h("header", { class: "landing__head" }, h("div", { class: "row" }, h("h1", null, t("landing.head.title")), demoDataInfo(mode)), h("p", { class: "landing__how" }, t("landing.head.how"))),
      h(
        "div",
        { class: "landing__col landing__col--answer" },
        h("section", { class: "landing__share", "aria-label": t("landing.share.label") }, ui.share),
        h("section", { class: "landing__call" }, ui.call),
        h("section", { class: "landing__see" }, ui.see),
      ),
      // the map and the live feed sit beside the answer, so the map is on the first screen
      h(
        "div",
        { class: "landing__col landing__col--map" },
        (ui.mapSection = h("section", { class: "landing__map card", "aria-label": t("landing.map.title") }, ui.map, ui.look)),
        (ui.feedSection = h("section", { class: "landing__feed", "aria-label": t("landing.feed.title") }, h("div", { class: "list-head" }, h("h2", { class: "section-title" }, t("landing.feed.title"))), ui.feed)),
      ),
    );
    main.removeAttribute("aria-busy");
    renderAll();
  }

  function renderAll() {
    renderShare();
    renderMap();
    renderCall();
    renderSee();
    renderFeed();
  }

  // ------------------------------------------------------------------ share
  function renderShare() {
    const { coverage, coverageError } = state;
    if (!coverage) {
      ui.share.replaceChildren(coverageError ? errorFor(coverageError, { onRetry: bothFailed() ? retryAll : loadCoverage }) : skeleton({ lines: 4, block: true }));
      return;
    }
    const lastChecked = coverage.city.status === "no_data" && state.demoNow ? t("states.lastChecked", { when: state.demoNow }) : null;
    ui.share.replaceChildren(
      h(
        "div",
        { class: "card stack" },
        shareDisplay(coverage.city, {
          label: t("landing.share.label"),
          claim: { band: coverage.claim_band, labelKey: coverage.claim_label_key },
          lastChecked,
        }),
        h("p", { class: "muted small" }, t("landing.share.window", { from: formatDate(coverage.window_start), to: formatDate(coverage.window_end) })),
      ),
    );
  }

  // -------------------------------------------------------------------- map
  function renderMap() {
    const { coverage, coverageError } = state;
    if (!coverage) {
      mapHandle?.destroy();
      mapHandle = null;
      // the share above already says what failed and offers the one Retry; the map waits for it
      ui.mapSection.hidden = Boolean(coverageError);
      ui.map.replaceChildren(skeleton({ lines: 3, block: true }), h("p", { class: "muted small", role: "status" }, t("landing.map.loading")));
      ui.look.replaceChildren();
      return;
    }
    ui.mapSection.hidden = false;
    if (!mapHandle) {
      mapHandle = mapView({ basemap: mode.basemap });
      ui.map.replaceChildren(
        h(
          "div",
          { class: "row row--between" },
          h("h2", { class: "section-title" }, t("landing.map.title")),
          infoButton({ topic: t("landing.map.about"), title: t("landing.map.title"), content: mapLegend() }),
        ),
        mapHandle.el.firstElementChild, // the map box; the full legend sits behind the (i) button
        miniLegend(),
      );
    }
    mapHandle.setData({ clusters: coverage.clusters });
    const look = coverage.clusters.filter((c) => coverage.look_here.includes(c.id));
    ui.look.replaceChildren(
      ...(look.length
        ? [
            h("span", { class: "landing__look-label" }, icon("alert"), t("landing.map.lookHere")),
            ...look.map((c) =>
              h("a", { class: "pill pill--attention landing__look-link", href: `/team/cluster/${encodeURIComponent(c.id)}` }, h("bdi", { dir: "auto" }, c.name)),
            ),
          ]
        : []),
    );
  }

  // The three things worth knowing at a glance; the full legend is behind the (i).
  function miniLegend() {
    const item = (swatches, text) => h("li", { class: "landing__legend-item" }, ...swatches.map((cls) => h("span", { class: `sw ${cls}`, "aria-hidden": "true" })), h("span", null, text));
    return h(
      "ul",
      { class: "landing__legend" },
      item(["sw--r1", "sw--r5"], t("landing.map.legend.darker")),
      item(["sw--look"], t("landing.map.legend.look")),
      item(["sw--sim"], t("landing.map.legend.sim")),
    );
  }

  // ------------------------------------------------------------- try the call
  // With no public address (CT_PUBLIC_URL) a phone cannot reach this computer, so there is no code to scan
  // and no "your own phone": the browser phone opens in a new tab, and the page here stays where it is.
  function renderCall() {
    const lang = currentLang() === "hi" ? "hi" : "en";
    const open = h(
      "a",
      { class: "btn btn--primary", href: `/phone?lang=${lang}`, target: "_blank", rel: "noopener" },
      icon("phone"),
      t("landing.call.open"),
      h("span", { class: "visually-hidden" }, ` (${t("landing.call.newTab")})`),
    );
    const own = mode.public_url ? [h("p", null, t("landing.call.body")), h("div", { class: "landing__qr" }, qrCode(`${mode.public_url.replace(/\/+$/, "")}/phone?lang=${lang}`))] : [];
    ui.call.replaceChildren(h("div", { class: "card stack landing__call-card" }, h("h2", { class: "section-title" }, t("landing.call.title")), ...own, open));
  }

  // ----------------------------------------------------------------- see as
  function renderSee() {
    const buttons = SEE_AS.map((role) =>
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
              toast(err instanceof ApiError ? err.message : t("landing.see.failed"), { tone: "attention" });
            }
          },
        },
        t(`roles.${role}`),
      ),
    );
    ui.see.replaceChildren(
      h("div", { class: "row" }, h("h2", { class: "section-title" }, t("landing.see.title")), infoButton({ topic: t("landing.see.topic"), title: t("landing.see.title"), content: h("p", null, t("landing.see.about")) })),
      h("div", { class: "row landing__see-row" }, buttons),
    );
  }

  // ------------------------------------------------------------------- feed
  function feedLabel(item) {
    return t({ report: "landing.feed.report", alert: "landing.feed.alert", missed_call: "landing.feed.missedCall", call: "landing.feed.call" }[item.kind]);
  }

  function feedWhere(item) {
    if (item.kind === "call" || item.kind === "missed_call") return null;
    if (item.wardName) return `${t("landing.feed.ward", { no: item.wardNo })}, ${item.wardName}`;
    const area = clusterName(item.clusterId);
    const ward = item.wardNo ? t("landing.feed.ward", { no: item.wardNo }) : null;
    return [ward, area].filter(Boolean).join(", ") || t(PLACE_WAITING.has(item.locationStatus) ? "landing.feed.placeWaiting" : "landing.feed.noPlace");
  }

  function feedRow(item) {
    const where = feedWhere(item);
    // A real report arrives without its answer (the server withholds it); a simulated one carries it.
    const answer = item.animal ? t(`landing.feed.animal.${item.animal}`) : null;
    return h(
      "li",
      { class: ["list__row", item.simulated ? "is-simulated" : ""], "data-kind": item.kind },
      h(
        "div",
        { class: "list__main" },
        h(
          "span",
          { class: "list__title" },
          // an alert row says "Alert" to a screen reader only: the icon says it to everyone else, as on the alert card
          item.kind === "alert" ? [icon("alert"), " ", h("span", { class: "visually-hidden" }, `${feedLabel(item)}: `), h("bdi", { dir: "auto" }, where)] : feedLabel(item),
          item.kind !== "alert" && where ? h("span", { class: "muted" }, " · ", h("bdi", { dir: "auto" }, where)) : null,
          answer ? h("span", { class: "muted" }, " · ", answer) : null,
        ),
      ),
      // an instant shows as day and time; a real alert is served with its day only, so it shows the day
      h("div", { class: "list__side" }, h("span", { class: "list__meta num" }, item.at ? formatDayTime(item.at) : formatDate(item.day)), item.simulated ? pill({ tone: "simulated", iconName: "phone", text: t("status.source.simulator") }) : null),
    );
  }

  function renderFeed() {
    ui.feedSection.hidden = bothFailed();
    if (!state.feedLoaded) {
      ui.feed.replaceChildren(state.feedError ? errorFor(state.feedError, { onRetry: loadFeed }) : skeleton({ lines: 3 }));
      return;
    }
    if (state.feed.length === 0) {
      const shareSaysIt = state.coverage?.city.status === "no_data"; // the share above already has "Last checked"
      ui.feed.replaceChildren(emptyState({ title: t("landing.feed.empty"), body: shareSaysIt ? null : t("states.lastChecked", { when: formatTime(state.checkedAt) }), iconName: "phone" }));
      return;
    }
    ui.feed.replaceChildren(h("ul", { class: "list" }, state.feed.map(feedRow)));
  }

  // ------------------------------------------------------------------- data
  async function loadCoverage({ quiet = false } = {}) {
    const mine = (ticket += 1);
    if (!quiet) {
      state.coverage = null;
      state.coverageError = null;
      renderShare();
      renderMap();
    }
    try {
      const coverage = await api.get("/api/coverage");
      if (mine !== ticket) return;
      state.coverage = coverage;
      state.coverageError = null;
      state.demoNow = null;
      if (coverage.city.status === "no_data") {
        try {
          state.demoNow = formatTime((await api.get("/api/demo/state")).now);
        } catch (err) {
          console.warn(`landing: the demo clock could not be read (${err.message})`);
        }
      }
    } catch (err) {
      if (mine !== ticket) return;
      if (!(err instanceof ApiError)) console.error(err);
      if (quiet && state.coverage) return; // a background refresh that fails keeps the last numbers; the next event tries again
      state.coverage = null;
      state.coverageError = err;
    }
    renderShare();
    renderMap();
    renderFeed(); // area names in the feed come from the coverage
  }

  /** Put rows in the feed: a row with an id already there is updated in place, the rest are added; newest first. */
  function mergeFeed(items) {
    const rows = new Map(state.feed.map((item) => [item.id, item]));
    // what a later message does not say (a place, an answer) stays as the earlier one had it
    for (const item of items) {
      const known = rows.get(item.id);
      rows.set(item.id, { ...known, ...Object.fromEntries(Object.entries(item).filter(([, v]) => v !== null && v !== undefined)), at: known?.at ?? item.at, day: known?.day ?? item.day });
    }
    // newest first; a row with only a day sorts as the start of that day, after the instants of the same day
    const key = (item) => item.at ?? item.day;
    state.feed = [...rows.values()].sort((a, b) => key(b).localeCompare(key(a))).slice(0, FEED_MAX);
  }

  // A real report arrives without its answer (the server withholds it); a simulated one carries it.
  const animalOf = (animal, simulated) => (simulated ? (animal ?? null) : null);

  async function loadFeed({ quiet = false } = {}) {
    if (!quiet) {
      state.feedError = null;
      state.feedLoaded = false;
      renderFeed();
    }
    try {
      // The rows the live stream shows, read again, so a reload does not lose them: the visitor's own phone and
      // the alert summaries this view may see. The server decides what is in it and how many.
      const recent = await api.get("/api/feed");
      mergeFeed(
        recent.map((r) => ({
          id: `${r.kind}:${r.id}`,
          at: r.at,
          day: r.day,
          kind: r.kind,
          clusterId: r.cluster_id,
          wardNo: r.ward_no,
          wardName: r.ward_name,
          animal: animalOf(r.animal, r.simulated),
          locationStatus: r.location_status,
          simulated: Boolean(r.simulated),
        })),
      );
      state.checkedAt = new Date();
      state.feedLoaded = true;
      state.feedError = null;
    } catch (err) {
      if (!(err instanceof ApiError)) console.error(err);
      if (quiet && state.feedLoaded) return; // a background refresh that fails keeps the rows; the next event tries again
      state.feedError = err;
    }
    renderFeed();
  }

  function addEvent(event, type) {
    const data = event.data ?? {};
    if (type === "alert") {
      loadFeed({ quiet: true }); // the server knows its day, its place and whether it is the visitor's own
    } else if (type !== "report" || data.simulated) {
      // a real report is a nudge to read the counts again, not a row: the list shows only the visitor's own
      // phone and alert summaries, the same rows a reload brings back
      mergeFeed([
        {
          id: `${type}:${ENTITY_ID[type](data)}`,
          at: event.at,
          kind: type,
          clusterId: data.cluster_id ?? null,
          wardNo: data.ward_no ?? null,
          wardName: null,
          animal: animalOf(data.animal, data.simulated),
          locationStatus: data.location_status ?? null,
          simulated: Boolean(data.simulated),
        },
      ]);
      state.feedLoaded = true;
      state.feedError = null;
      renderFeed();
    }
    if (type === "report" || type === "alert") {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => loadCoverage({ quiet: true }), REFRESH_AFTER_EVENT_MS);
    }
  }

  // ------------------------------------------------------------------ start
  build();
  loadCoverage();
  loadFeed();

  const stream = sharedStream();
  for (const type of ["report", "alert", "missed_call", "call"]) stream.on(type, (event) => addEvent(event, type));
  stream.onResync(() => {
    loadCoverage({ quiet: true });
    loadFeed();
  });
  document.addEventListener("ct:langchange", () => {
    document.title = t("landing.page.title");
    build();
  });
}

boot().catch((err) => {
  console.error(err);
  failed(err, () => location.reload());
});
