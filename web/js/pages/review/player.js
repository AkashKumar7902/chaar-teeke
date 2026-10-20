// The recording player. The clip is fetched once, on the first Play, so that one listen is one logged request;
// nothing is loaded in advance. Space plays and pauses (the page wires the key to toggle()).

import { s, h } from "/js/core/dom.js";
import { t } from "/js/core/i18n.js";
import { icon } from "/js/ui/index.js";

const PLAY = "M8 5.5v13l11-6.5-11-6.5Z";
const PAUSE = "M7 5h3.5v14H7zM13.5 5H17v14h-3.5z";

function glyph(d) {
  return s("svg", { class: "icon icon--fill", viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false" }, s("path", { d }));
}

const clock = (seconds) => {
  const n = Number.isFinite(seconds) ? Math.max(0, Math.round(seconds)) : 0;
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
};

/**
 * player({ reportId, onMissing }) -> { el, toggle, destroy, refresh }
 * `onMissing(message)` runs when the server says there is no recording (410), so the page can lock the choices.
 */
export function player({ reportId, onMissing }) {
  let audio = null;
  let url = null;
  let phase = "idle"; // idle | loading | playing | paused | ended | failed
  let failure = null;

  const button = h("button", { type: "button", class: "btn btn--lg rv-play", "aria-pressed": "false", onClick: () => toggle() });
  const bar = h("progress", { class: "rv-progress", max: "1", value: "0", "aria-hidden": "true" });
  const time = h("span", { class: "rv-time num", "aria-hidden": "true" });
  const hint = h("span", { class: "rv-hint muted small" }, t("review.player.space"));
  const message = h("p", { class: "rv-player__msg", role: "status" });
  const el = h("div", { class: "rv-player", role: "group", "aria-label": t("review.player.label") }, button, h("div", { class: "rv-player__track" }, bar, time), hint, message);

  function paint() {
    const label =
      phase === "playing" ? t("review.player.pause") : phase === "loading" ? t("review.player.loading") : phase === "ended" ? t("review.player.again") : phase === "failed" ? t("review.player.tryAgain") : t("review.player.play");
    button.replaceChildren(phase === "playing" ? glyph(PAUSE) : phase === "failed" ? icon("refresh") : glyph(PLAY), label);
    button.setAttribute("aria-pressed", String(phase === "playing"));
    if (phase === "loading") button.setAttribute("aria-busy", "true");
    else button.removeAttribute("aria-busy");
    const total = audio && Number.isFinite(audio.duration) ? audio.duration : null;
    const now = audio ? audio.currentTime : 0;
    time.textContent = total === null ? clock(now) : t("review.player.time", { now: clock(now), total: clock(total) });
    bar.value = total ? Math.min(1, now / total) : 0;
    message.replaceChildren(...(failure ? [icon("alert"), failure] : []));
  }

  async function load() {
    phase = "loading";
    failure = null;
    paint();
    let response;
    try {
      response = await fetch(`/api/review/${encodeURIComponent(reportId)}/audio`, { credentials: "same-origin" });
    } catch {
      phase = "failed";
      failure = t("errors.network");
      paint();
      return false;
    }
    if (!response.ok) {
      let body = null;
      try {
        body = await response.json();
      } catch {
        body = null;
      }
      if (response.status === 401) {
        location.replace(`/signin?next=${encodeURIComponent(location.pathname)}`);
        return false;
      }
      if (response.status === 410) {
        phase = "idle";
        onMissing(body?.message ?? t("review.noAudio"));
        return false;
      }
      phase = "failed";
      failure = body?.message ?? t("errors.server");
      paint();
      return false;
    }
    url = URL.createObjectURL(await response.blob());
    audio = new Audio(url);
    audio.addEventListener("timeupdate", paint);
    audio.addEventListener("loadedmetadata", paint);
    audio.addEventListener("pause", () => {
      if (phase === "playing") {
        phase = "paused";
        paint();
      }
    });
    audio.addEventListener("ended", () => {
      phase = "ended";
      paint();
    });
    audio.addEventListener("error", () => {
      phase = "failed";
      failure = t("review.player.failed");
      paint();
    });
    return true;
  }

  async function toggle() {
    if (phase === "loading") return;
    if (phase === "playing") {
      audio.pause();
      return;
    }
    if (audio === null || phase === "failed") {
      if (audio !== null) {
        audio.pause();
        URL.revokeObjectURL(url);
        audio = null;
      }
      if (!(await load())) return;
    }
    if (phase === "ended") audio.currentTime = 0;
    try {
      await audio.play();
      phase = "playing";
      failure = null;
    } catch {
      phase = "failed";
      failure = t("review.player.failed");
    }
    paint();
  }

  function destroy() {
    audio?.pause();
    if (url) URL.revokeObjectURL(url);
    audio = null;
    url = null;
  }

  paint();
  return {
    el,
    toggle,
    destroy,
    get playing() {
      return phase === "playing";
    },
    /** Repaint the words after a language change. */
    refresh() {
      hint.textContent = t("review.player.space");
      el.setAttribute("aria-label", t("review.player.label"));
      paint();
    },
  };
}
