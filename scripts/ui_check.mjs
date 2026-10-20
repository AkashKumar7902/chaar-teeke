#!/usr/bin/env node
// UI check: every page route at every width, theme and language, in a real Chrome.
//
//   node scripts/ui_check.mjs            full matrix (1440, 1280, 390; light, dark; en, hi; doi, ur on patient pages)
//   node scripts/ui_check.mjs --quick    1440 and 390, light, en and hi only
//   node scripts/ui_check.mjs --pages=/clinic,/team     only routes containing one of these texts
//   node scripts/ui_check.mjs --out=DIR  screenshots there (default ~/chaar-teeke-build/shots/ui_check)
//
// It starts its own demo server on a free port with a throwaway database (CT_ASR=off), seeds it, signs in
// each staff role and a visitor with the passwords the seed wrote, and stops the server when it ends.
// It fails (exit 1) on: console errors and failed requests, horizontal overflow, Devanagari text under
// 14 px, interactive targets under 44 px, fonts that are not self-hosted or not loaded, a language that was
// not applied, strings that did not resolve, the never-on-screen words in visible text, and a notched-share
// percentage that does not have its interval and "Could not see" in the same element or table row (a share
// whose status is too_few shows n and the number still needed instead, and is not a percentage).
// Chrome: CHROME_PATH, or the system Chrome. playwright-core: scripts/node_modules, or
// ~/chaar-teeke-build/uicheck/node_modules.

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, openSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { createRequire } from "node:module";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const HOME = homedir();

const WIDTHS_FULL = [1440, 1280, 390];
const WIDTHS_QUICK = [1440, 390];
const HEIGHT = { 1440: 900, 1280: 800, 390: 844 };
const SCHEMES_FULL = ["light", "dark"];
const STAFF_LANGS = ["en", "hi"];
const PATIENT_LANGS = ["en", "hi", "doi", "ur"];
const PATIENT_ROUTES = new Set(["/phone", "/privacy"]);
const EXTRA_WIDTHS = [1440, 390];
const FONT_FAMILIES = {
  latin: "Atkinson Hyperlegible Next",
  devanagari: "Noto Sans Devanagari",
  arabic: "Noto Nastaliq Urdu",
};
const NEVER_WORDS = ["safe", "clear", "all fine", "rabies detected", "diagnosis", "fully sterilised", "fully sterilized"];
// The words a notched-share percentage must sit beside, per language, read from the same bundles the pages use
// (common.share.interval is "interval {range}", common.share.couldNotSee is "Could not see").
const SHARE_LANGS = ["en", "hi"];
let SHARE_WORDS = null; // set in main()
let sharesChecked = 0;
const MIN_DEVANAGARI_PX = 14;
const MIN_TARGET_PX = 44;
const PAGE_READY_MS = 20000;
const SETTLE_MS = 250;
// A route with no sign-in need is visited with no cookie; every other route has the staff role it belongs to.
const PUBLIC_ROUTES = new Set(["/", "/phone", "/signin", "/privacy", "/method", "/404"]);
const ROLE_OF_PREFIX = [
  ["/clinic", "clinic"],
  ["/team", "abc"],
  ["/oversight", "oversight"],
  ["/review", "reviewer"],
  ["/admin", "admin"],
];
const VISITOR_AS = { clinic: "clinic", abc: "abc", oversight: "oversight", reviewer: "reviewer", admin: "admin" };

// ------------------------------------------------------------------------------------------ arguments
const args = process.argv.slice(2);
const quick = args.includes("--quick");
const only = (args.find((a) => a.startsWith("--pages=")) ?? "").slice(8).split(",").filter(Boolean);
const outArg = (args.find((a) => a.startsWith("--out=")) ?? "").slice(6);
const OUT = outArg ? resolve(outArg) : join(HOME, "chaar-teeke-build", "shots", "ui_check");
if (args.includes("--help") || args.includes("-h")) {
  console.log(readFileSync(fileURLToPath(import.meta.url), "utf8").split("\n").slice(1, 15).map((l) => l.replace(/^\/\/ ?/, "")).join("\n"));
  process.exit(0);
}
for (const a of args) {
  if (!["--quick", "--help", "-h"].includes(a) && !a.startsWith("--pages=") && !a.startsWith("--out=")) {
    console.error(`ui_check: unknown argument ${a}`);
    process.exit(2);
  }
}

// ------------------------------------------------------------------------------------------ helpers
const say = (msg) => console.log(msg);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const slug = (route) => (route === "/" ? "home" : route.replace(/\{[^}]+\}/g, "").replace(/^\/|\/$/g, "").replace(/\//g, "-"));

function chromePath() {
  const candidates = [
    process.env.CHROME_PATH,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  const found = candidates.find((c) => existsSync(c));
  if (!found) throw new Error(`Chrome not found (set CHROME_PATH). Looked at: ${candidates.join(", ")}`);
  return found;
}

async function loadPlaywright() {
  const roots = [import.meta.url, pathToFileURL(join(HOME, "chaar-teeke-build", "uicheck", "package.json")).href];
  for (const root of roots) {
    let file;
    try {
      file = createRequire(root).resolve("playwright-core");
    } catch {
      continue;
    }
    const mod = await import(pathToFileURL(file).href);
    return mod.chromium ?? mod.default.chromium;
  }
  throw new Error(
    "playwright-core not found. Install it in scripts/ (cd scripts && npm install) or use ~/chaar-teeke-build/uicheck/node_modules.",
  );
}

/** {en: {interval, couldNotSee}, hi: {...}} from web/i18n/common.<lang>.json; a bundle without them is an error. */
function shareWords() {
  const out = {};
  for (const lang of SHARE_LANGS) {
    const share = JSON.parse(readFileSync(join(ROOT, "web", "i18n", `common.${lang}.json`), "utf8")).share ?? {};
    const interval = String(share.interval ?? "").replace("{range}", "").trim();
    const couldNotSee = String(share.couldNotSee ?? "").trim();
    if (!interval || !couldNotSee) throw new Error(`web/i18n/common.${lang}.json has no share.interval or share.couldNotSee`);
    out[lang] = { interval, couldNotSee };
  }
  return out;
}

/** The screenshot folder is wiped before a run, so --out may only name a folder this script made (it holds
 *  report.json), an empty one, or one that does not exist yet. Never a home folder or a source tree. */
function assertSafeToClear(dir) {
  if (!existsSync(dir)) return;
  const names = readdirSync(dir);
  if (names.length === 0 || names.includes("report.json")) return;
  throw new Error(`--out=${dir} is not empty and is not a previous ui_check folder (no report.json); refusing to delete it`);
}

function freePort() {
  return new Promise((ok, fail) => {
    const s = createServer();
    s.on("error", fail);
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => ok(port));
    });
  });
}

/** The page routes the server serves, read from PAGES in chaarteeke/api/app.py. */
function pageRoutes() {
  const src = readFileSync(join(ROOT, "chaarteeke", "api", "app.py"), "utf8");
  const block = src.match(/^PAGES: dict\[str, str\] = \{([\s\S]*?)^\}/m);
  if (!block) throw new Error("PAGES not found in chaarteeke/api/app.py");
  const pages = [...block[1].matchAll(/"([^"]+)":\s*"([^"]+\.html)"/g)].map((m) => ({ route: m[1], file: m[2] }));
  if (!pages.length) throw new Error("PAGES in chaarteeke/api/app.py is empty");
  const nf = src.match(/^NOT_FOUND_PAGE\s*=\s*"([^"]+)"/m);
  if (nf) pages.push({ route: "/404", file: nf[1] });
  return pages;
}

function roleOf(route) {
  if (PUBLIC_ROUTES.has(route)) return "anon";
  const hit = ROLE_OF_PREFIX.find(([p]) => route === p || route.startsWith(`${p}/`));
  if (!hit) throw new Error(`ui_check: no role known for route ${route}; add it to ROLE_OF_PREFIX or PUBLIC_ROUTES`);
  return hit[1];
}

// ------------------------------------------------------------------------------------------ the server
let server = null;
let tmp = null;

function stopServer() {
  if (!server || server.exited) return Promise.resolve();
  return new Promise((done) => {
    const hard = setTimeout(() => {
      try {
        process.kill(-server.child.pid, "SIGKILL");
      } catch {
        /* already gone */
      }
    }, 4000);
    server.child.once("exit", () => {
      clearTimeout(hard);
      done();
    });
    try {
      process.kill(-server.child.pid, "SIGTERM");
    } catch {
      clearTimeout(hard);
      done();
    }
  });
}

function cleanupSync() {
  if (server && !server.exited) {
    try {
      process.kill(-server.child.pid, "SIGKILL");
    } catch {
      /* already gone */
    }
  }
  if (tmp) rmSync(tmp, { recursive: true, force: true });
}
process.on("exit", cleanupSync);
for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(sig, () => {
    cleanupSync();
    process.exit(130);
  });
}

function serverEnv(dir, port) {
  return {
    ...process.env,
    CT_MODE: "demo",
    CT_DB: join(dir, "ui_check.db"),
    CT_VAR_DIR: join(dir, "var"),
    CT_ASR: "off",
    CT_TELEPHONY: "sim",
    CT_BASEMAP: "none",
    CT_SCHEDULER: "false",
    CT_HOST: "127.0.0.1",
    CT_PORT: String(port),
  };
}

async function startServer(port) {
  tmp = mkdtempSync(join(tmpdir(), "ct-ui-check-"));
  const env = serverEnv(tmp, port);
  const seed = spawnSync("uv", ["run", "--frozen", "chaar-teeke", "seed-demo"], { cwd: ROOT, env, encoding: "utf8" });
  if (seed.status !== 0) throw new Error(`seed-demo failed (exit ${seed.status}):\n${seed.stdout}\n${seed.stderr}`);
  const logPath = join(tmp, "server.log");
  const log = openSync(logPath, "w");
  const child = spawn("uv", ["run", "--frozen", "chaar-teeke", "serve", "--host", "127.0.0.1", "--port", String(port)], {
    cwd: ROOT,
    env,
    detached: true,
    stdio: ["ignore", log, log],
  });
  server = { child, exited: false, logPath };
  child.on("exit", () => {
    server.exited = true;
  });
  const base = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    if (server.exited) throw new Error(`server exited early:\n${tail(logPath)}`);
    try {
      const r = await fetch(`${base}/api/mode`);
      if (r.ok) return base;
    } catch {
      /* not up yet */
    }
    await sleep(300);
  }
  throw new Error(`server not ready after 90 s:\n${tail(logPath)}`);
}

function tail(path, n = 25) {
  try {
    return readFileSync(path, "utf8").trim().split("\n").slice(-n).join("\n");
  } catch {
    return "(no log)";
  }
}

function readLogins() {
  const text = readFileSync(join(tmp, "var", "demo_logins.txt"), "utf8");
  const out = {};
  for (const line of text.split("\n")) {
    if (!line.trim() || line.startsWith("#")) continue;
    const [name, password] = line.trim().split(/\s+/);
    out[name] = password;
  }
  if (!Object.keys(out).length) throw new Error("var/demo_logins.txt has no logins");
  return out;
}

// ------------------------------------------------------------------------------------------ the audit that runs in the page
/* eslint-disable no-undef */
function auditInPage(cfg) {
  const out = { overflow: null, smallDevanagari: [], smallTargets: [], fonts: [], text: [], shares: [] };
  const de = document.documentElement;
  const body = document.body;
  const vw = de.clientWidth;

  const describe = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += `#${el.id}`;
    const cls = typeof el.className === "string" ? el.className.trim().split(/\s+/).filter(Boolean).slice(0, 2) : [];
    if (cls.length) s += `.${cls.join(".")}`;
    const label = (el.getAttribute("aria-label") || el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 30);
    return label ? `${s} "${label}"` : s;
  };
  const visible = (el) => {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || cs.visibility === "collapse") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const clipsAt = (el) => {
    for (let p = el.parentElement; p && p !== body && p !== de; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (/(auto|scroll|hidden|clip)/.test(cs.overflowX) && p.getBoundingClientRect().right <= vw + 1) return true;
    }
    return false;
  };

  // 1. horizontal overflow
  const scrollW = Math.max(de.scrollWidth, body.scrollWidth);
  if (scrollW > vw + 1) {
    const culprits = [];
    for (const el of body.querySelectorAll("*")) {
      if (culprits.length >= 4) break;
      const r = el.getBoundingClientRect();
      if (r.width > 0 && (r.right > vw + 1 || r.left < -1) && !clipsAt(el) && visible(el)) culprits.push(`${describe(el)} (${Math.round(r.left)}..${Math.round(r.right)})`);
    }
    out.overflow = { scrollWidth: scrollW, clientWidth: vw, culprits };
  }

  // 2. Devanagari under the floor
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  const seenDeva = new Set();
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const text = n.nodeValue;
    if (!/[\u0900-\u097F]/.test(text) || !text.trim()) continue;
    const el = n.parentElement;
    if (!el || /^(SCRIPT|STYLE|TITLE|NOSCRIPT)$/.test(el.tagName) || !visible(el)) continue;
    const size = parseFloat(getComputedStyle(el).fontSize);
    if (size < cfg.minDevanagariPx) {
      const key = `${describe(el)}|${size}`;
      if (!seenDeva.has(key)) {
        seenDeva.add(key);
        out.smallDevanagari.push(`${describe(el)} is ${size}px`);
      }
    }
  }

  // 3. interactive targets
  const selector =
    'a[href], button, input:not([type="hidden"]), select, textarea, summary, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="switch"], [role="checkbox"], [role="radio"], [tabindex]:not([tabindex="-1"])';
  const seenTarget = new Set();
  for (const el of body.querySelectorAll(selector)) {
    if (!visible(el)) continue;
    if (el.closest(".maplibregl-ctrl-attrib, .maplibregl-ctrl-logo")) continue; // legal attribution of the map library
    const cs = getComputedStyle(el);
    if (el.tagName === "A" && cs.display === "inline") continue; // a link inside a sentence
    let box = el;
    if (el.tagName === "INPUT" && /^(checkbox|radio)$/.test(el.type) && el.labels && el.labels[0]) box = el.labels[0];
    const r = box.getBoundingClientRect();
    if (r.width <= 2 || r.height <= 2) continue; // visually hidden
    if (r.right <= 0 || r.bottom <= 0) continue; // parked off screen (skip link)
    if (el !== box && !visible(box)) continue;
    if (Math.min(r.width, r.height) < cfg.minTargetPx - 0.5) {
      const key = describe(el);
      if (!seenTarget.has(key)) {
        seenTarget.add(key);
        out.smallTargets.push(`${describe(el)} is ${Math.round(r.width)}x${Math.round(r.height)}px`);
      }
    }
  }

  // 4. fonts: the page's own self-hosted faces are loaded for the scripts on screen
  const text = body.innerText || "";
  const faces = [...document.fonts];
  const need = [];
  const latin = text.match(/[A-Za-z0-9]/);
  if (latin) need.push(["latin", cfg.families.latin, latin[0]]);
  const deva = text.match(/[\u0900-\u097F]/);
  if (deva) need.push(["devanagari", cfg.families.devanagari, deva[0]]);
  const arab = text.match(/[\u0600-\u06FF]/);
  if (arab) need.push(["arabic", cfg.families.arabic, arab[0]]);
  for (const [script, family, sample] of need) {
    const mine = faces.filter((f) => f.family.replace(/["']/g, "") === family);
    if (!mine.length) out.fonts.push(`${family} has no @font-face (${script} text on screen)`);
    else if (!mine.some((f) => f.status === "loaded")) out.fonts.push(`${family} not loaded (${script} text on screen)`);
    else if (!document.fonts.check(`16px "${family}"`, sample)) out.fonts.push(`${family} cannot draw "${sample}" (${script})`);
  }

  // 5. visible text
  for (const word of cfg.neverWords) {
    const re = new RegExp(`(^|[^\\p{L}])${word.replace(/\s+/g, "\\s+")}($|[^\\p{L}])`, "iu");
    const m = text.match(re);
    if (m) {
      const at = m.index;
      out.text.push(`never-on-screen word "${word}": ...${text.slice(Math.max(0, at - 25), at + word.length + 25).replace(/\s+/g, " ")}...`);
    }
  }
  const broken = text.match(/\[missing: [^\]]*\]|\[\?\w+\]|\[object \w+\]|\bundefined\b|\bNaN\b/);
  if (broken) out.text.push(`unresolved text "${broken[0]}"`);

  // 6. a notched-share percentage never stands without its interval and the could-not-see count, in the same
  // element (the share component, an area row, a look-here card, the digest sheet) or the same table row.
  const PERCENT = /\d\s*%/;
  const squash = (el) => (el.textContent || "").replace(/\s+/g, " ").trim();
  const wordsOf = (el) => cfg.shareWords[(el.closest("[lang]") || de).getAttribute("lang")] ?? cfg.shareWords.en;
  const needs = (el, text, what) => {
    const w = wordsOf(el);
    const missing = [];
    if (!text.includes(w.interval)) missing.push(`"${w.interval}"`);
    if (!text.includes(w.couldNotSee)) missing.push(`"${w.couldNotSee}"`);
    if (missing.length) out.shares.push(`${what} has a percentage without ${missing.join(" and ")}: ${describe(el)}`);
  };
  let checked = 0;
  for (const el of body.querySelectorAll('.share[data-status="ok"], .share[data-status="bracketed"]')) {
    needs(el, squash(el), "share component");
    checked += 1;
  }
  for (const el of body.querySelectorAll(".area-row, .look-card")) {
    const text = squash(el);
    if (!PERCENT.test(text)) continue; // a too_few row shows n and the number still needed
    needs(el, text, "area row");
    checked += 1;
  }
  for (const el of body.querySelectorAll(".sheet__sec")) {
    const pct = el.querySelector(".sheet__share");
    if (!pct || !PERCENT.test(pct.textContent || "")) continue;
    needs(el, squash(el), "digest sheet");
    checked += 1;
  }
  for (const table of body.querySelectorAll(".sheet table")) {
    const head = squash(table.tHead || table);
    const w = wordsOf(table);
    for (const row of table.tBodies[0]?.rows ?? []) {
      const cells = [...row.cells];
      const pct = cells.find((c) => PERCENT.test(c.textContent || ""));
      if (!pct) continue;
      checked += 1;
      if (!squash(pct).includes(w.interval)) out.shares.push(`digest row has a percentage without "${w.interval}": ${describe(row)}`);
      if (!head.includes(w.couldNotSee)) out.shares.push(`digest table has no "${w.couldNotSee}" column`);
      else if (!squash(cells[cells.indexOf(pct) + 1] ?? row).replace(w.couldNotSee, "").match(/\d/)) {
        out.shares.push(`digest row has a percentage and no could-not-see count after it: ${describe(row)}`);
      }
    }
  }
  out.sharesChecked = checked;
  return out;
}
/* eslint-enable no-undef */

// ------------------------------------------------------------------------------------------ one page visit
async function visit(page, base, job, urlPath) {
  const problems = [];
  const origin = new URL(base).origin;
  const fail = (check, detail) => problems.push({ check, detail });

  const onConsole = (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    if (/^Failed to load resource/.test(text)) return; // reported with its URL by the response handler
    fail("console", text.slice(0, 220));
  };
  const onPageError = (err) => fail("console", `uncaught: ${String(err.message ?? err).slice(0, 220)}`);
  const onResponse = (res) => {
    const u = new URL(res.url());
    if (res.status() < 400) return;
    if (job.role === "anon" && u.pathname === "/api/me" && res.status() === 401) return; // the frame asks who is here
    if (res.request().isNavigationRequest() && res.request().frame() === page.mainFrame() && job.route === "/404" && res.status() === 404) return;
    fail("request", `HTTP ${res.status()} ${res.request().method()} ${u.pathname}`);
  };
  const onFailed = (req) => {
    const why = req.failure()?.errorText ?? "failed";
    if (/ERR_ABORTED/.test(why)) return; // a stream or fetch cancelled by navigation
    fail("request", `${why} ${req.method()} ${req.url().slice(0, 120)}`);
  };
  const onRequest = (req) => {
    const u = new URL(req.url());
    if (/^(data|blob|about):/.test(u.protocol)) return;
    if (u.origin === origin) return; // the CSP allows this server only; MapLibre is vendored under /vendor
    fail("external", `request to ${u.origin}${u.pathname}`.slice(0, 160));
  };
  page.on("console", onConsole);
  page.on("pageerror", onPageError);
  page.on("response", onResponse);
  page.on("requestfailed", onFailed);
  page.on("request", onRequest);

  try {
    const sep = urlPath.includes("?") ? "&" : "?";
    const res = await page.goto(`${base}${urlPath}${sep}lang=${job.lang}`, { waitUntil: "domcontentloaded", timeout: 30000 });
    const expect = job.route === "/404" ? 404 : 200;
    if (!res || res.status() !== expect) fail("request", `page answered ${res ? res.status() : "nothing"}, expected ${expect}`);
    // polled with evaluate(): page.waitForFunction() builds code in the page, which the strict CSP refuses
    const until = Date.now() + PAGE_READY_MS;
    while (await page.evaluate(() => !!document.querySelector('[aria-busy="true"]'))) {
      if (Date.now() > until) {
        fail("loading", `still aria-busy after ${PAGE_READY_MS / 1000} s`);
        break;
      }
      await sleep(100);
    }
    await page.evaluate(() => document.fonts.ready);
    await sleep(SETTLE_MS);

    const landed = new URL(page.url()).pathname;
    const want = urlPath.split("?")[0];
    if (landed !== want) fail("redirect", `asked for ${want}, ended on ${landed}`);
    const htmlLang = await page.evaluate(() => document.documentElement.lang);
    if (htmlLang !== job.lang) fail("language", `asked for ${job.lang}, <html lang> is "${htmlLang}"`);

    const a = await page.evaluate(auditInPage, {
      minDevanagariPx: MIN_DEVANAGARI_PX,
      minTargetPx: MIN_TARGET_PX,
      families: FONT_FAMILIES,
      neverWords: NEVER_WORDS,
      shareWords: SHARE_WORDS,
    });
    sharesChecked += a.sharesChecked;
    if (a.overflow) fail("overflow", `scrollWidth ${a.overflow.scrollWidth} > ${a.overflow.clientWidth}: ${a.overflow.culprits.join("; ")}`);
    for (const d of a.smallDevanagari) fail("devanagari-size", d);
    for (const d of a.smallTargets) fail("target-size", d);
    for (const d of a.fonts) fail("fonts", d);
    for (const d of a.text) fail("text", d);
    for (const d of a.shares) fail("share", d);

    const shot = join(OUT, slug(job.route), `${job.label}-${job.width}-${job.scheme}-${job.lang}.png`);
    mkdirSync(dirname(shot), { recursive: true });
    await page.screenshot({ path: shot, fullPage: true });
  } catch (e) {
    fail("script", String(e.message ?? e).split("\n")[0].slice(0, 220));
  } finally {
    page.off("console", onConsole);
    page.off("pageerror", onPageError);
    page.off("response", onResponse);
    page.off("requestfailed", onFailed);
    page.off("request", onRequest);
  }
  return problems;
}

// ------------------------------------------------------------------------------------------ main
async function main() {
  const t0 = Date.now();
  const pages = pageRoutes();
  const present = [];
  const skipped = [];
  for (const p of pages) (existsSync(join(ROOT, "web", p.file)) ? present : skipped).push(p);
  for (const p of skipped) say(`skip  ${p.route}  (web/${p.file} is not built yet)`);
  const wanted = only.length ? present.filter((p) => only.some((o) => p.route.includes(o))) : present;
  if (!wanted.length) throw new Error("no page to check: no HTML file exists for any route (or --pages matched none)");

  SHARE_WORDS = shareWords();
  const chromium = await loadPlaywright();
  const executablePath = chromePath();

  assertSafeToClear(OUT);
  rmSync(OUT, { recursive: true, force: true });
  mkdirSync(OUT, { recursive: true });

  const port = await freePort();
  say(`ui_check: starting a demo server on 127.0.0.1:${port} (temporary database, CT_ASR=off)`);
  const base = await startServer(port);
  const logins = readLogins();

  const browser = await chromium.launch({ executablePath, headless: true });
  const results = []; // {route, variant, problems}
  try {
    // sign in once per role; the cookie is reused by every context
    const sessions = { anon: undefined };
    const signer = await browser.newContext();
    for (const role of ["clinic", "abc", "vet", "oversight", "reviewer", "admin"]) {
      const r = await signer.request.post(`${base}/api/auth/signin`, { data: { username: role, password: logins[role] } });
      if (!r.ok()) throw new Error(`sign-in as ${role} failed: HTTP ${r.status()} ${await r.text()}`);
      sessions[role] = await signer.storageState();
      await signer.clearCookies();
    }
    for (const [role, as] of Object.entries(VISITOR_AS)) {
      const r = await signer.request.post(`${base}/api/auth/visitor`, { data: { as } });
      if (!r.ok()) throw new Error(`visitor as ${as} failed: HTTP ${r.status()} ${await r.text()}`);
      sessions[`visitor-${role}`] = await signer.storageState();
      await signer.clearCookies();
    }

    // real ids for the two routes that carry one
    const ids = {};
    {
      const c = await browser.newContext({ storageState: sessions.clinic });
      const today = await (await c.request.get(`${base}/api/clinic/today`)).json();
      const row = [...(today.due_today ?? []), ...(today.missed ?? []), ...(today.awaiting_call ?? [])][0];
      ids.course_id = row?.course_id;
      await c.close();
      const t = await browser.newContext({ storageState: sessions.abc });
      const cov = await (await t.request.get(`${base}/api/coverage`)).json();
      ids.cluster_id = cov.clusters?.[0]?.id;
      await t.close();
    }
    const resolveRoute = (route) =>
      route.replace(/\{(\w+)\}/g, (_, name) => {
        if (!ids[name]) throw new Error(`no ${name} in the seeded demo data to open ${route}`);
        return ids[name];
      });

    // the jobs
    const widths = quick ? WIDTHS_QUICK : WIDTHS_FULL;
    const schemes = quick ? ["light"] : SCHEMES_FULL;
    const jobs = [];
    for (const p of wanted) {
      const role = roleOf(p.route);
      const langs = !quick && PATIENT_ROUTES.has(p.route) ? PATIENT_LANGS : STAFF_LANGS;
      for (const width of widths) for (const scheme of schemes) for (const lang of langs) jobs.push({ ...p, role, label: role, width, scheme, lang, session: role });
      if (!quick && role !== "anon") {
        for (const width of EXTRA_WIDTHS) jobs.push({ ...p, role, label: `visitor-${role}`, width, scheme: "light", lang: "en", session: `visitor-${role}` });
        if (p.route === "/team/alerts") for (const width of EXTRA_WIDTHS) jobs.push({ ...p, role: "vet", label: "vet", width, scheme: "light", lang: "en", session: "vet" });
      }
    }
    const groups = new Map();
    for (const j of jobs) {
      const k = `${j.session}|${j.width}|${j.scheme}`;
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(j);
    }

    say(`ui_check: ${wanted.length} pages, ${jobs.length} visits (${quick ? "quick" : "full"})`);
    let done = 0;
    for (const [key, list] of groups) {
      const [session, width, scheme] = key.split("|");
      const context = await browser.newContext({
        viewport: { width: Number(width), height: HEIGHT[width] },
        colorScheme: scheme,
        reducedMotion: "reduce",
        storageState: sessions[session],
      });
      const page = await context.newPage();
      try {
        for (const job of list) {
          const problems = await visit(page, base, job, resolveRoute(job.route));
          results.push({ route: job.route, variant: `${job.label} ${job.width} ${job.scheme} ${job.lang}`, problems });
          done += 1;
          process.stdout.write(problems.length ? "x" : ".");
        }
      } finally {
        await context.close();
      }
      if (done % 60 < list.length) process.stdout.write(` ${done}/${jobs.length}\n`);
    }
    process.stdout.write("\n");
  } finally {
    await browser.close();
  }

  // report
  const byRoute = new Map();
  for (const r of results) {
    if (!byRoute.has(r.route)) byRoute.set(r.route, { runs: 0, bad: 0, issues: new Map() });
    const entry = byRoute.get(r.route);
    entry.runs += 1;
    if (r.problems.length) entry.bad += 1;
    for (const p of r.problems) {
      const k = `${p.check}: ${p.detail}`;
      if (!entry.issues.has(k)) entry.issues.set(k, []);
      entry.issues.get(k).push(r.variant);
    }
  }
  let failures = 0;
  for (const [route, e] of byRoute) {
    say(`${e.bad ? "FAIL" : "ok  "}  ${route.padEnd(28)} ${e.runs - e.bad}/${e.runs} visits clean`);
    for (const [issue, variants] of e.issues) {
      failures += 1;
      const shown = variants.slice(0, 3).join(" | ");
      say(`        ${issue}\n          in ${variants.length} visit(s): ${shown}${variants.length > 3 ? " ..." : ""}`);
    }
  }
  writeFileSync(
    join(OUT, "report.json"),
    JSON.stringify(
      { quick, skipped: skipped.map((p) => p.route), results: results.filter((r) => r.problems.length) },
      null,
      2,
    ),
  );
  if (!sharesChecked && wanted.some((p) => ["/", "/team", "/oversight"].includes(p.route))) {
    say("FAIL  no notched-share percentage was found on any page to check; the share check would pass on nothing");
    failures += 1;
  }
  say(`ui_check: ${sharesChecked} notched-share percentages checked for interval and could-not-see`);
  const secs = Math.round((Date.now() - t0) / 1000);
  say(`ui_check: ${failures ? `${failures} distinct problem(s)` : "no problems"} over ${results.length} visits in ${secs} s; screenshots in ${OUT}`);
  return failures ? 1 : 0;
}

let code = 1;
try {
  code = await main();
} catch (e) {
  console.error(`ui_check: ${e.message ?? e}`);
  if (server) console.error(`server log tail:\n${tail(server.logPath)}`);
} finally {
  await stopServer();
  if (tmp) {
    try {
      readdirSync(tmp);
      rmSync(tmp, { recursive: true, force: true });
    } catch {
      /* already gone */
    }
  }
}
process.exit(code);
