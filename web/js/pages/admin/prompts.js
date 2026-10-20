// M3 Prompts. One job: audit the voice. Every prompt by language: its words, whether its audio file is
// there, play it, and upload a recording that replaces the machine voice draft. A language is ready when
// no file is missing. GET /api/admin/prompts, POST /api/admin/prompts/{lang}/{name} (the WAV is the body).
// Big grid, so: one language at a time, rows built once, search and filters only show or hide them.

import { ApiError, getMe } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { has, t } from "/js/core/i18n.js";
import { emptyState, field, icon, infoButton, pill, selectInput, textInput, toast } from "/js/ui/index.js";
import { api, guarded, region, writeAttrs } from "./common.js";
import { segmentsOf } from "./promptFiles.js";
import { inspectFile } from "./wav.js";

const LANGS = ["hi", "doi", "ur"];
const UPLOAD_TIMEOUT_MS = 60000;

// What the person last chose here. It outlives a rebuild of the page (a language switch).
const ui = { lang: null, q: "", missingOnly: false, call: "" };
const versions = new Map(); // file -> time of the last upload, so the player never plays the old recording
let repaint = null;

/** Open the grid on one language with only the missing files (the System tab links here). */
export function showMissing(lang) {
  ui.lang = lang;
  ui.missingOnly = true;
  ui.q = "";
  ui.call = "";
  repaint?.();
}

/** A call's name in the page language; the server's own words when this page has none for it. */
const callName = (call) => {
  const key = `admin.prompts.calls.${String(call).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "")}`;
  return has(key) ? t(key) : String(call);
};

const reviewLabel = (status) => (has(`admin.prompts.review.${status}`) ? t(`admin.prompts.review.${status}`) : String(status));

export function promptsTab({ me, signal }) {
  const host = h("div", { class: "stack" });
  host.append(h("h2", { class: "visually-hidden" }, t("admin.prompts.title")));
  const body = h("div", { class: "stack" });
  host.append(body);

  // ---- player: one audio at a time
  const player = { audio: null, rel: null, button: null };
  function stopPlayer() {
    if (player.audio) {
      player.audio.pause();
      player.audio.removeAttribute("src");
    }
    if (player.button?.isConnected) {
      player.button.setAttribute("aria-pressed", "false");
      player.button.textContent = t("admin.prompts.play");
    }
    player.audio = player.rel = player.button = null;
  }
  const urlOf = (rel) => `/media/prompts/${rel.split("/").map(encodeURIComponent).join("/")}${versions.has(rel) ? `?v=${versions.get(rel)}` : ""}`;
  function togglePlay(rel, button, statusEl) {
    const same = player.rel === rel;
    stopPlayer();
    if (same) return;
    statusEl.textContent = "";
    const audio = new Audio(urlOf(rel));
    Object.assign(player, { audio, rel, button });
    button.setAttribute("aria-pressed", "true");
    button.textContent = t("admin.prompts.stop");
    audio.addEventListener("ended", stopPlayer);
    audio.addEventListener("error", () => {
      stopPlayer();
      setStatus(statusEl, t("admin.prompts.playFailed"), "bad");
    });
    audio.play().catch((err) => {
      if (err?.name === "AbortError") return;
      stopPlayer();
      setStatus(statusEl, t("admin.prompts.playFailed"), "bad");
    });
  }

  // ---- upload: a file chosen, checked here, then sent as the request body
  const chooser = h("input", { type: "file", accept: ".wav,audio/wav,audio/x-wav", class: "visually-hidden", tabindex: "-1", "aria-hidden": "true" });
  let target = null;
  chooser.addEventListener("change", async () => {
    const file = chooser.files?.[0];
    const t0 = target;
    chooser.value = "";
    if (file && t0) await sendRecording(file, t0);
  });

  async function sendRecording(file, { lang, rel, button, statusEl }) {
    setStatus(statusEl, t("admin.prompts.checking"), "");
    let verdict;
    try {
      verdict = await inspectFile(file);
    } catch {
      verdict = { ok: false, problem: "unreadable" };
    }
    if (!verdict.ok) return setStatus(statusEl, t(`admin.prompts.wav.${verdict.problem}`), "bad");
    setStatus(statusEl, t("admin.prompts.uploading"), "");
    button.setAttribute("aria-busy", "true");
    try {
      const name = rel.slice(lang.length + 1, -".wav".length);
      await postWav(`/api/admin/prompts/${lang}/${name.split("/").map(encodeURIComponent).join("/")}`, file);
      versions.set(rel, Date.now());
      toast(t("admin.prompts.uploaded", { name }));
      await view.reload({ quiet: true });
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      setStatus(statusEl, err.message, "bad");
      if (err.isReadOnly) toast(err.message, { tone: "attention" });
    } finally {
      button.removeAttribute("aria-busy");
    }
    return undefined;
  }

  const view = region(body, { load: () => api.get("/api/admin/prompts"), render, signal, lines: 8 });
  repaint = () => view.paint();
  signal?.addEventListener("abort", () => {
    stopPlayer();
    repaint = null;
  });

  // ---- the grid
  function render(data) {
    stopPlayer();
    const langs = data.languages;
    if (!langs.some((l) => l.lang === ui.lang)) ui.lang = (langs.find((l) => !l.ready) ?? langs[0]).lang;
    const L = langs.find((l) => l.lang === ui.lang);
    const missing = new Set(L.missing);
    const promptRels = new Set();

    const items = L.prompts.map((p) => {
      const files = segmentsOf(p.id, p.text).map((s) => ({ ...s, rel: `${L.lang}/${s.name}.wav` }));
      files.forEach((f) => {
        promptRels.add(f.rel);
        f.present = !missing.has(f.rel);
      });
      if (p.has_audio !== files.every((f) => f.present)) console.error(`prompts: ${p.id} (${L.lang}) has_audio disagrees with its files`);
      return { p, files, node: null, hay: `${p.id} ${p.call ?? ""} ${p.call ? callName(p.call) : ""} ${p.text}`.toLowerCase() };
    });
    const clips = L.missing.filter((m) => !promptRels.has(m)).map((m) => {
      const isFile = /^[a-z]+\/.+\.wav$/.test(m);
      return { raw: m, rel: isFile ? m : null, label: isFile ? m.slice(L.lang.length + 1, -4) : m.replace(/ \(not defined in prompts\.json\)$/, ""), hay: m.toLowerCase() };
    });

    const calls = [...new Set(L.prompts.map((p) => p.call).filter(Boolean))];
    if (ui.call && !calls.includes(ui.call)) ui.call = "";

    // summary: also the language picker
    const summary = h(
      "div",
      { class: "adm-langs", role: "group", "aria-label": t("admin.prompts.langsLabel") },
      langs.map((l) =>
        h(
          "button",
          { type: "button", class: "adm-lang", "aria-pressed": String(l.lang === L.lang), "data-key": `lang-${l.lang}`, onClick: () => { ui.lang = l.lang; view.paint(); } },
          h("span", { class: "adm-lang__name" }, t(`admin.lang.${l.lang}`)),
          l.ready ? pill({ tone: "primary", iconName: "check", text: t("admin.prompts.ready") }) : pill({ tone: "attention", iconName: "alert", text: t("admin.prompts.missingCount", { count: l.missing.length }) }),
        ),
      ),
    );

    // toolbar
    const search = textInput({ name: "q", type: "search", inputmode: "search", autocomplete: "off", value: ui.q, placeholder: t("admin.prompts.searchPlaceholder") });
    search.dataset.key = "search";
    const searchField = field({ label: t("admin.prompts.search"), control: search });
    const callSel = selectInput({ name: "call", value: ui.call, options: [{ value: "", label: t("admin.prompts.allCalls") }, ...calls.map((c) => ({ value: c, label: callName(c) }))] });
    callSel.dataset.key = "call";
    const callField = field({ label: t("admin.prompts.call"), control: callSel });
    const chip = h("button", { type: "button", class: "chip", "aria-pressed": String(ui.missingOnly), "data-key": "missing-only" }, icon("alert"), t("admin.prompts.missingOnly"));
    const count = h("p", { class: "muted", role: "status", "aria-live": "polite" });
    const none = emptyState({
      title: t("admin.prompts.noMatch"),
      action: h("button", { type: "button", class: "btn", onClick: () => { Object.assign(ui, { q: "", call: "", missingOnly: false }); view.paint(); } }, t("admin.prompts.showAll")),
    });
    none.hidden = true;

    const statusOf = new Map();
    const grid = h("ul", { class: "pgrid" });
    for (const item of items) {
      item.node = promptItem(L, item, statusOf);
      grid.append(item.node);
    }

    const clipList = h("ul", { class: "pgrid" });
    for (const c of clips) {
      c.node = clipItem(L, c, statusOf);
      clipList.append(c.node);
    }
    const clipsBox = h(
      "details",
      { class: "adm-clips", open: ui.missingOnly || clips.length <= 5 ? true : null },
      h("summary", null, t("admin.prompts.clips", { count: clips.length })),
      h("p", { class: "muted small" }, t("admin.prompts.clipsNote")),
      clipList,
    );
    clipsBox.hidden = clips.length === 0;

    function apply() {
      const q = ui.q.trim().toLowerCase();
      let shown = 0;
      for (const item of items) {
        const keep = (!q || item.hay.includes(q)) && (!ui.call || item.p.call === ui.call) && (!ui.missingOnly || item.files.some((f) => !f.present));
        item.node.hidden = !keep;
        if (keep) shown += 1;
      }
      let clipsShown = 0;
      for (const c of clips) {
        const keep = (!q || c.hay.includes(q)) && !ui.call;
        c.node.hidden = !keep;
        if (keep) clipsShown += 1;
      }
      clipsBox.classList.toggle("adm-clips--empty", clipsShown === 0);
      count.textContent = t("admin.prompts.showing", { shown: shown + clipsShown, total: items.length + clips.length });
      none.hidden = shown + clipsShown !== 0;
      grid.hidden = shown === 0;
      clipsBox.hidden = clips.length === 0 || clipsShown === 0;
    }
    search.addEventListener("input", () => { ui.q = search.value; apply(); });
    callSel.addEventListener("change", () => { ui.call = callSel.value; apply(); });
    chip.addEventListener("click", () => {
      ui.missingOnly = !ui.missingOnly;
      chip.setAttribute("aria-pressed", String(ui.missingOnly));
      apply();
    });
    apply();

    // one Upload handler for every row
    grid.addEventListener("click", onUploadClick);
    clipList.addEventListener("click", onUploadClick);
    function onUploadClick(event) {
      const button = event.target.closest("button[data-upload]");
      if (!button) return;
      guarded(me, () => {
        target = { lang: L.lang, rel: button.dataset.upload, button, statusEl: statusOf.get(button.dataset.upload) };
        chooser.click();
      })(event);
    }
    const onPlayClick = (event) => {
      const button = event.target.closest("button[data-play]");
      if (button) togglePlay(button.dataset.play, button, statusOf.get(button.dataset.play));
    };
    grid.addEventListener("click", onPlayClick);

    return h(
      "div",
      { class: "stack" },
      summary,
      h("div", { class: "adm-toolbar" }, searchField.el, callField.el, chip),
      h("div", { class: "list-head" }, count, infoButton({ topic: t("admin.prompts.topic"), title: t("admin.prompts.topic"), content: h("p", null, t("admin.prompts.about")) })),
      none,
      grid,
      clipsBox,
      chooser,
    );
  }

  function promptItem(L, item, statusOf) {
    const { p, files } = item;
    const multi = files.length > 1;
    return h(
      "li",
      { class: "pitem" },
      h(
        "div",
        { class: "pitem__head" },
        h("strong", { class: "pitem__id" }, p.id),
        p.call ? h("span", { class: "muted small" }, callName(p.call)) : null,
        p.voice ? (p.voice === "recording uploaded" ? pill({ tone: "primary", iconName: "check", text: t("admin.prompts.voice.uploaded") }) : pill({ tone: "estimate", iconName: "info", text: t("admin.prompts.voice.machine") })) : null,
        p.review_status ? h("span", { class: "muted small" }, reviewLabel(p.review_status)) : null,
      ),
      h("ul", { class: "pitem__files" }, files.map((f) => fileRow(L, p, f, multi, statusOf))),
    );
  }

  function fileRow(L, p, f, multi, statusOf) {
    const status = h("span", { class: "pfile__status small", role: "status" });
    statusOf.set(f.rel, status);
    const what = multi ? t("admin.prompts.partNamed", { id: p.id, n: f.part + 1, total: segmentsOf(p.id, p.text).length }) : p.id;
    return h(
      "li",
      { class: "pfile" },
      h("p", { class: "pfile__text", lang: L.lang, dir: L.lang === "ur" ? "rtl" : "ltr" }, multi ? h("span", { class: "pfile__part" }, t("admin.prompts.part", { n: f.part + 1 }), " ") : null, f.text),
      h(
        "div",
        { class: "pfile__side" },
        f.present ? pill({ tone: "primary", iconName: "check", text: t("admin.prompts.present") }) : pill({ tone: "attention", iconName: "alert", text: t("admin.prompts.missing") }),
        f.present ? h("button", { type: "button", class: "btn", "data-play": f.rel, "data-key": `play-${f.rel}`, "aria-pressed": "false", "aria-label": t("admin.prompts.playNamed", { name: what }) }, t("admin.prompts.play")) : null,
        h("button", { type: "button", class: "btn", "data-upload": f.rel, "data-key": `up-${f.rel}`, "aria-label": t("admin.prompts.uploadNamed", { name: what }), ...writeAttrs(me) }, t(f.present ? "admin.prompts.replace" : "admin.prompts.upload")),
      ),
      status,
    );
  }

  function clipItem(L, c, statusOf) {
    const status = h("span", { class: "pfile__status small", role: "status" });
    if (c.rel) statusOf.set(c.rel, status);
    return h(
      "li",
      { class: "pfile pfile--clip" },
      h("p", { class: "pfile__text" }, c.rel ? [clipLabel(c.label), " ", h("code", { class: "muted small" }, c.label)] : t("admin.prompts.notDefined", { id: c.label })),
      c.rel
        ? h(
            "div",
            { class: "pfile__side" },
            pill({ tone: "attention", iconName: "alert", text: t("admin.prompts.missing") }),
            h("button", { type: "button", class: "btn", "data-upload": c.rel, "data-key": `up-${c.rel}`, "aria-label": t("admin.prompts.uploadNamed", { name: c.label }), ...writeAttrs(me) }, t("admin.prompts.upload")),
          )
        : null,
      status,
    );
  }

  return host;
}

/** "slot/day_3" -> "Day 3". Names this page has no words for show as they are. */
function clipLabel(label) {
  const m = /^slot\/(clinic|time|ward|day|month|weekday|num)_(.+)$/.exec(label);
  if (!m) return "";
  const value = m[1] === "time" ? `${m[2].slice(0, 2)}:${m[2].slice(2)}` : m[1] === "clinic" ? "" : m[2];
  return t(`admin.prompts.clip.${m[1]}`, { value });
}

function setStatus(el, text, tone) {
  el.textContent = text;
  el.classList.toggle("pfile__status--bad", tone === "bad");
}

/** The WAV is the request body, so this is a raw fetch (api.js sends JSON). Same CSRF header and error shape. */
async function postWav(path, file) {
  const { csrf } = await getMe();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), UPLOAD_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(path, { method: "POST", credentials: "same-origin", headers: { "X-CT-CSRF": csrf, "Content-Type": "audio/wav", Accept: "application/json" }, body: file, signal: controller.signal });
  } catch (err) {
    throw new ApiError(0, err?.name === "AbortError" ? "timeout" : "network", t(err?.name === "AbortError" ? "errors.timeout" : "errors.network"));
  } finally {
    clearTimeout(timer);
  }
  let parsed = null;
  try {
    parsed = await response.json();
  } catch {
    parsed = null;
  }
  if (!response.ok) {
    throw new ApiError(response.status, typeof parsed?.error === "string" ? parsed.error : "bad_response", typeof parsed?.message === "string" ? parsed.message : t("errors.server"), parsed);
  }
  return parsed;
}
