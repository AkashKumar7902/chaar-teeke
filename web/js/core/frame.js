// The page frame: one header (mark, role nav, language toggle, account menu) and, in demo mode, one strip:
// "DEMO DATA: synthetic, not real patients", the demo clock and a "Move clock" menu. Nothing else repeats DEMO.
// On a phone the strip is one line (DEMO DATA, the clock, Move clock) and the rest of the notice opens on a tap.
//
//   await initI18n({ bundles: ["common", "clinic"] });
//   const frame = await mountFrame({ mode: "staff" });
//
// Modes:
//   "staff"    needs a login. GET /api/mode is read first; a browser with no login goes to /signin (this
//              promise then never settles, because the page is leaving) and /api/me is never asked, so an
//              anonymous visitor sees no red 401. Role nav, staff language toggle, account menu, Move clock.
//              The clinic, team and admin views also read /api/system/status and listen to the `system` event: while
//              the phone line is down an amber banner says calls are not going out, and why (SPEC s8).
//   "public"   works with no login: brand and the staff language toggle (en, hi); the DEMO strip in demo mode.
//              The landing page and sign-in page use it. No stream, no /api/me.
//   "patient"  as public, with the patient language toggle (hi, doi, ur, en). A language nobody native has read
//              yet (Dogri, Urdu) carries a dashed "draft translation" note under the header (frame.draft).
// The DEMO strip comes from /api/mode, so it shows for anonymous visitors too; the demo clock is added
// when somebody is signed in (GET /api/demo/state needs a login).
//
// Pages put their content in <main id="main">. The frame inserts itself before it.

import { api, ApiError, getMe, getMode, onApiError, resetMe } from "./api.js";
import { h, ltr } from "./dom.js";
import { formatDateTime, formatDayTime } from "./format.js";
import { AUTONYMS, DRAFT_LANGS, PATIENT_LANGS, STAFF_LANGS, currentLang, setLang, t } from "./i18n.js";
import { leaveWhenSignedOut, sharedStream } from "./sse.js";
import { icon } from "../ui/icons.js";
import { menuButton } from "../ui/popover.js";
import { toast } from "../ui/toast.js";

/** Where each role lands and what its nav holds. Labels are nav.* keys in common.*.json. */
export const HOME = {
  clinic: "/clinic",
  abc: "/team",
  vet: "/team",
  oversight: "/oversight",
  reviewer: "/review",
  admin: "/admin",
};
const TEAM_NAV = [
  ["nav.coverage", "/team"],
  ["nav.alerts", "/team/alerts"],
  ["nav.worked", "/team/worked"],
  ["nav.sight", "/team/sight"],
];
export const NAV = {
  clinic: [
    ["nav.today", "/clinic"],
    ["nav.enrol", "/clinic/enrol"],
  ],
  abc: TEAM_NAV,
  vet: TEAM_NAV,
  oversight: [
    ["nav.summary", "/oversight"],
    ["nav.digest", "/oversight/digest"],
    ["nav.method", "/method"],
  ],
  reviewer: [
    ["nav.review", "/review"],
    ["nav.names", "/review/names"],
  ],
  admin: [["nav.admin", "/admin"]],
};
/** Roles a visitor can view as (POST /api/auth/visitor). */
export const VISITOR_VIEWS = ["abc", "clinic", "oversight", "reviewer", "admin"];

/** Views that show calls or place them, so they hear when the phone line is down. */
export const TELEPHONY_VIEWS = ["clinic", "abc", "vet", "admin"];
/** The demo strip folds to one line at this width (matches the phone breakpoint in components.css). */
const NARROW = "(max-width: 40rem)";

/** The role whose nav applies: a visitor sees the nav of the role they chose. */
export function effectiveRole(me) {
  return me.role === "visitor" ? (me.acting_as ?? "abc") : me.role;
}

/** The nav link that matches the path best (the longest prefix), or null. Pure. */
export function currentNavHref(items, pathname) {
  let best = null;
  for (const [, href] of items) {
    const hit = pathname === href || pathname.startsWith(`${href}/`);
    if (hit && (best === null || href.length > best.length)) best = href;
  }
  return best;
}

function langToggle(langs, onPick) {
  return h(
    "div",
    { class: "seg", role: "group", "aria-label": t("frame.language") },
    langs.map((code) =>
      h(
        "button",
        { type: "button", class: "seg__btn", lang: code, "aria-pressed": String(code === currentLang()), onClick: () => onPick(code) },
        AUTONYMS[code],
      ),
    ),
  );
}

async function pickLang(code) {
  try {
    await setLang(code);
  } catch (err) {
    toast(err.message, { tone: "attention" });
  }
}

/** Where a browser with no login (or an ended one) goes. Nothing else happens on the page after this. */
function goToSignIn(pathname) {
  if (pathname.startsWith("/signin")) return;
  location.replace(`/signin?next=${encodeURIComponent(pathname + location.search)}`);
}

/** A promise that never settles: the page is leaving for /signin, so the code after `await mountFrame()` must not run. */
const leaving = () => new Promise(() => {});

/** A translated sentence with one {when} in it, as nodes with the date or time set left to right. */
function withLtr(key, text) {
  const mark = "\u0001";
  const [before, after = ""] = t(key, { when: mark }).split(mark);
  return [before, ltr(text), after];
}

export async function mountFrame({ mode = "staff", pathname = location.pathname } = {}) {
  if (!["staff", "public", "patient"].includes(mode)) throw new Error(`mountFrame: unknown mode "${mode}"`);
  const host = document.body;
  const skip = h("a", { class: "skip-link", href: "#main" }, t("frame.skip"));
  const header = h("header", { class: "frame" });
  const strip = h("div", { class: "demo-strip", role: "region", hidden: true });
  const bannerSlot = h("div", { class: "banner-slot" });
  host.prepend(skip, header, strip, bannerSlot);

  let me = null;
  let demo = null; // DemoState

  // ------------------------------------------------------------ who is here
  // /api/mode is public and never a 401, so it is asked first; /api/me only when somebody is signed in.
  const site = await getMode();
  if (mode === "staff" && !site.signed_in && pathname.startsWith("/signin")) mode = "public"; // the sign-in page itself
  if (mode === "staff") {
    if (!site.signed_in) {
      goToSignIn(pathname);
      return leaving();
    }
    try {
      me = await getMe();
    } catch (err) {
      if (err instanceof ApiError && err.isAuth) {
        goToSignIn(pathname);
        return leaving();
      }
      throw err;
    }
  }

  // ---------------------------------------------------------------- render
  function render() {
    header.replaceChildren(buildBar());
    renderStrip();
  }

  function buildBar() {
    const home = me && mode === "staff" ? HOME[effectiveRole(me)] : "/";
    const brand = h("a", { class: "brand", href: home, "aria-label": t("frame.home") }, h("span", { class: "mark", "aria-hidden": "true" }), h("span", null, t("frame.brand")));
    if (mode !== "staff") {
      const langs = mode === "patient" ? PATIENT_LANGS : STAFF_LANGS;
      return h("div", { class: "frame__bar" }, brand, h("div", { class: "frame__nav" }, langToggle(langs, pickLang)));
    }
    const items = NAV[effectiveRole(me)] ?? [];
    const active = currentNavHref(items, pathname);
    const nav = h(
      "nav",
      { class: "frame__nav", "aria-label": t("frame.mainNav") },
      items.map(([key, href]) => h("a", { class: "nav-link", href, "aria-current": href === active ? "page" : null }, t(key))),
    );
    return h("div", { class: "frame__bar" }, brand, nav, h("div", { class: "frame__tools" }, langToggle(STAFF_LANGS, pickLang), accountMenu()));
  }

  function accountMenu() {
    const items = [];
    if (me.role === "visitor") {
      items.push({ id: "_who", label: t("frame.visitorNote"), disabled: true });
      for (const role of VISITOR_VIEWS) items.push({ id: `view:${role}`, label: t("frame.viewAs", { role: t(`roles.${role}`) }), current: me.acting_as === role });
    } else {
      items.push({ id: "_who", label: me.display_name, disabled: true });
    }
    items.push({ id: "signout", label: t("frame.signOut") });
    const roleName = t(`roles.${me.role === "visitor" ? (me.acting_as ?? "abc") : me.role}`);
    return menuButton({
      label: t("frame.account"),
      items,
      onSelect: async (id) => {
        try {
          if (id === "signout") {
            await api.post("/api/auth/signout");
            resetMe();
            location.assign("/");
          } else if (id.startsWith("view:")) {
            const role = id.slice(5);
            await api.post("/api/auth/visitor", { as: role });
            resetMe();
            location.assign(HOME[role]);
          }
        } catch (err) {
          toast(err.message, { tone: "attention" });
        }
      },
      triggerLabel: `${t("frame.account")}: ${roleName}`,
      buttonContent: [icon("user"), h("span", { class: "account__name" }, roleName), icon("chevronDown", { className: "account__name" })],
    });
  }

  // ------------------------------------------------------------- demo strip
  let clockEl = null;
  let noteOpen = false; // on a phone: the rest of the notice is open
  const narrow = window.matchMedia(NARROW);
  // The label and the date are Latin or digits inside a line that may run right to left (Urdu): each is its
  // own left-to-right island, so the colon and the comma stay where they belong. The words before the date
  // ("Demo clock:") are dropped from view on a phone, where the date beside "Move clock" says it, and stay for
  // a screen reader.
  const clockNodes = () => {
    const [before, date, after] = withLtr("frame.demo.clock", (narrow.matches ? formatDayTime : formatDateTime)(demo.now));
    return [h("span", { class: "demo-strip__clock-label" }, before), date, after];
  };

  function renderStrip() {
    const isDemo = site.mode === "demo";
    strip.hidden = !isDemo;
    if (!isDemo) {
      strip.replaceChildren();
      return;
    }
    strip.setAttribute("aria-label", t("frame.demo.region"));
    const label = ltr(t("frame.demo.label"));
    const head = narrow.matches
      ? h(
          "button",
          {
            type: "button",
            class: "demo-strip__label demo-strip__toggle",
            "aria-expanded": String(noteOpen),
            "aria-controls": "demo-note",
            onClick: () => {
              noteOpen = !noteOpen;
              renderStrip();
            },
          },
          label,
          icon("info"),
        )
      : h("span", { class: "demo-strip__label" }, label);
    const parts = [head, " ", h("span", { id: "demo-note", class: "demo-strip__note", hidden: narrow.matches && !noteOpen }, t("frame.demo.note"))];
    clockEl = null;
    if (demo) {
      clockEl = h("span", { class: "demo-strip__clock" }, clockNodes());
      parts.push(clockEl);
    }
    if (mode === "staff" && demo) parts.push(jumpMenu());
    strip.replaceChildren(h("div", { class: ["demo-strip__inner", noteOpen ? "is-open" : ""] }, parts));
  }
  narrow.addEventListener("change", () => renderStrip());

  function jumpMenu() {
    const items = demo.jump_targets.map((to) => ({ id: to, label: t(`jump.${to}`), disabled: to === "next_call" && !demo.next_call_at }));
    return menuButton({
      label: t("frame.jump"),
      items,
      onSelect: async (to) => {
        try {
          demo = await api.post("/api/demo/jump", { to }, { quiet: true });
          renderStrip();
          toast(t("jump.done", { what: t(`jump.${to}`) }));
          document.dispatchEvent(new CustomEvent("ct:demo-jump", { detail: demo }));
        } catch (err) {
          toast(err.message, { tone: "attention" });
        }
      },
    });
  }

  /** The clock event: only the clock text changes, so an open Move clock menu is not rebuilt under the presenter's hand. */
  function followClock(event) {
    if (!demo) return;
    demo = { ...demo, now: event.data.now, local_now: event.data.local_now };
    if (clockEl) clockEl.replaceChildren(...clockNodes());
  }

  async function loadDemo() {
    // The demo clock needs a login (GET /api/demo/state); the strip itself does not.
    if (site.mode !== "demo" || !site.signed_in) return;
    demo = await api.get("/api/demo/state");
    renderStrip();
  }

  // ------------------------------------------------------ offline and 403s
  let banner = null;
  function setOffline(offline) {
    document.documentElement.toggleAttribute("data-offline", offline);
    if (offline && !banner) {
      banner = h("div", { class: "banner", role: "status" }, icon("offline"), h("span", { class: "grow" }, t("states.offline")));
      bannerSlot.prepend(banner);
    } else if (!offline && banner) {
      banner.remove();
      banner = null;
    }
    document.dispatchEvent(new CustomEvent("ct:offline", { detail: { offline } }));
  }

  // ------------------------------------------------------- phone line down
  // SPEC s8: when calls cannot go out the people who watch calls are told where they look, not only on the
  // admin page. The server's check says whether the line is down and why; the `system` event says it changed.
  let lineBanner = null;
  let lineReason = null; // null: the line is not down
  function renderLineBanner() {
    if (lineReason === null) {
      lineBanner?.remove();
      lineBanner = null;
      return;
    }
    const content = [
      icon("alert"),
      h(
        "span",
        { class: "grow" },
        t("frame.lineDown"),
        lineReason ? [" ", h("span", { class: "banner__reason", lang: "en", dir: "auto" }, lineReason)] : null,
      ),
    ];
    if (!lineBanner) {
      lineBanner = h("div", { class: "banner banner--line", role: "status" });
      bannerSlot.append(lineBanner);
    }
    lineBanner.replaceChildren(...content);
  }
  async function readLine() {
    try {
      const status = await api.get("/api/system/status");
      const check = status.checks.find((c) => c.name === "telephony");
      lineReason = check?.code === "telephony.down" ? String(check.params?.reason ?? "") : null;
      renderLineBanner();
    } catch (err) {
      // the offline banner covers an unreachable server; anything else is said once in the console, not hidden
      console.warn(`frame: could not read the phone line status: ${err.message}`);
    }
  }

  // ------------------------------------------------- draft translation note
  let draftNote = null;
  function renderDraftNote() {
    draftNote?.remove();
    draftNote = null;
    if (mode !== "patient" || !DRAFT_LANGS.includes(currentLang())) return;
    draftNote = h("p", { class: "draft-note", role: "note" }, t("frame.draft"));
    bannerSlot.append(draftNote);
  }

  onApiError((error) => {
    // A call made with { quiet: true } shows its own refusal (a dialog), so the shared toast stays out of it.
    if (error.isReadOnly) {
      if (!error.quiet) toast(error.message, { tone: "attention" });
    }
    // The session ended while the page was open (staff pages only; a patient page has no session).
    else if (mode === "staff" && error.isAuth && !error.path?.startsWith("/api/auth/")) goToSignIn(pathname);
  });

  render();
  renderDraftNote();
  if (mode === "staff") {
    await loadDemo();
    const stream = sharedStream();
    stream.onStatus((status) => {
      if (status === "reconnecting") setOffline(true);
      else if (status === "live") setOffline(false);
    });
    // A stream that keeps failing because the login ended goes to /signin instead of retrying forever.
    leaveWhenSignedOut(stream, { fetchMode: () => api.get("/api/mode"), onSignedOut: () => goToSignIn(pathname) });
    stream.on("clock", followClock);
    if (TELEPHONY_VIEWS.includes(effectiveRole(me))) {
      readLine();
      // the event says only that the line changed; the check says whether it is down and why
      stream.on("system", (event) => {
        if (String(event.data?.check ?? "").startsWith("telephony")) readLine();
      });
    }
    stream.onResync(() => {
      loadDemo().catch((err) => console.error(err));
      if (TELEPHONY_VIEWS.includes(effectiveRole(me))) readLine();
    });
  } else {
    await loadDemo().catch((err) => console.warn(`frame: ${err.message}`));
    // The demo clock is shared. This page has no Move clock menu (the browser phone), but a jump made in
    // another tab, or by the presenter, moves the clock here too: follow it like the staff pages do.
    if (demo) {
      const stream = sharedStream();
      stream.on("clock", followClock);
      stream.onResync(() => loadDemo().catch((err) => console.warn(`frame: ${err.message}`)));
    }
  }
  document.addEventListener("ct:langchange", () => {
    render();
    renderDraftNote();
    renderLineBanner();
    banner?.querySelector("span.grow")?.replaceChildren(t("states.offline"));
  });
  return { me, refresh: render };
}
