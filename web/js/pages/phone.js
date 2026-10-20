// The browser phone (J2): a web page that plays the patient's phone call. It says SIMULATOR on a permanent
// bar. A missed call, a call back, an incoming-call screen, then the call: the words being said (from the
// captions route), the same audio files as the real IVR played in order, a keypad, and "Hold to speak" for
// the question that records a place. Protocol: chaarteeke/telephony/sim.py.

import { api, ApiError, getMe } from "/js/core/api.js";
import { h } from "/js/core/dom.js";
import { mountFrame } from "/js/core/frame.js";
import { currentLang, direction, initI18n, t } from "/js/core/i18n.js";
import { errorState, icon, keypad, pill } from "/js/ui/index.js";
import { PhoneAudio, ClipError } from "./phone/audio.js";
import { addWords, englishForUrls, loadCaptions, parseClipUrl } from "./phone/captions.js";
import { LinkError, PhoneLink } from "./phone/link.js";
import { MicError, Recorder } from "./phone/recorder.js";

const WAIT_LONG_MS = 45000;
const HOLD_MIN_MS = 450; // a press shorter than this is a tap: it starts recording and a second tap ends it

const main = document.getElementById("main");
const audio = new PhoneAudio();

/** Everything the screens are drawn from. */
const S = {
  view: "connecting", // connecting | error | ready | waiting | ringing | call | ended | dropped
  link: null,
  error: null, // {message, retry}
  note: null, // {text, lang?} one amber line above the screen
  waitedLong: false,
  call: null, // see newCall()
};
let recorder = null;
let timers = { wait: null, clock: null };

function newCall() {
  return {
    startedAt: performance.now(),
    lines: [], // the words on screen: [{lang, text}]
    captionsOff: false,
    englishOff: false, // the English words did not load
    playing: null, // {stop}
    record: null, // {name, maxSeconds}
    mic: null, // "pending" | "ready" | "denied" | "insecure" | "none" | "busy" | "failed"
    capture: { phase: "idle", startedAt: 0, timer: null, raf: null },
    sent: false,
    soundPaused: false,
  };
}

// ------------------------------------------------------------------- views
const stage = h("div", { class: "phone__stage" });
const noteHost = h("div", { "aria-live": "assertive" });
let ui = {}; // handles into the call screen, so a message updates a part instead of rebuilding it

function setView(view, { note = null } = {}) {
  S.view = view;
  S.note = note;
  render();
}

function render() {
  document.title = t("phone.page");
  ui = {};
  stage.replaceChildren(
    ...{
      connecting: viewConnecting,
      error: viewError,
      ready: viewReady,
      waiting: viewWaiting,
      ringing: viewRinging,
      call: viewCall,
      ended: viewEnded,
      dropped: viewDropped,
    }[S.view](),
  );
  renderNote();
  main.removeAttribute("aria-busy");
  if (S.view === "call") {
    renderCaption();
    renderRecord();
    renderSound();
    updateClock();
  }
}

function renderNote() {
  if (!S.note) {
    noteHost.replaceChildren();
    return;
  }
  noteHost.replaceChildren(h("p", { class: "phone__note", role: "alert" }, icon("alert"), h("span", { lang: S.note.lang }, S.note.text)));
}

function viewConnecting() {
  return [h("div", { class: "phone__panel", role: "status" }, h("p", { class: "phone__big" }, t("phone.connecting")), h("div", { class: "phone__spinner", "aria-hidden": "true" }))];
}

function viewError() {
  return [errorState({ message: S.error.message, onRetry: S.error.retry })];
}

function viewReady() {
  return [
    h(
      "div",
      { class: "phone__panel" },
      h("h1", null, t("phone.ready.title")),
      h("p", null, t("phone.ready.body")),
      h("button", { type: "button", class: "btn btn--primary btn--lg btn--block", onClick: giveMissedCall }, icon("phone"), t("phone.ready.give")),
    ),
    h("p", { class: "phone__promise small muted" }, t("phone.ready.promise"), " ", h("a", { href: `/privacy?lang=${document.documentElement.lang}` }, t("phone.ready.privacy"))),
  ];
}

function viewWaiting() {
  return [
    h(
      "div",
      { class: "phone__panel", role: "status" },
      h("h1", null, t("phone.waiting.title")),
      h("p", null, t("phone.waiting.body")),
      h("div", { class: "phone__spinner", "aria-hidden": "true" }),
      S.waitedLong ? h("button", { type: "button", class: "btn btn--block", onClick: giveMissedCall }, t("phone.waiting.again")) : null,
    ),
  ];
}

function viewRinging() {
  return [
    h(
      "div",
      { class: "phone__panel phone__panel--ring", role: "alert" },
      h("span", { class: "phone__avatar", "aria-hidden": "true" }, h("span", { class: "mark" })),
      h("h1", null, t("phone.ring.title")),
      h("p", { class: "phone__big" }, t("frame.brand")),
      h(
        "div",
        { class: "phone__ring-actions" },
        h("button", { type: "button", class: "btn btn--primary btn--lg", onClick: answer }, icon("phone"), t("phone.ring.answer")),
        h("button", { type: "button", class: "btn btn--lg", onClick: decline }, icon("x"), t("phone.ring.decline")),
      ),
    ),
  ];
}

function viewCall() {
  const keys = keypad({
    onKey: pressKey,
    hints: { "*": t("phone.call.hintAgain"), 9: t("phone.call.hintStop") },
    label: t("phone.call.keypad"),
  });
  ui.keypad = keys;
  ui.caption = h("div", { class: "phone__caption", "aria-live": "polite", "aria-label": t("phone.call.words") });
  ui.record = h("div");
  ui.clock = h("span", { class: "num" });
  ui.sound = h("div");
  return [
    h(
      "div",
      { class: "phone__bar" },
      h("span", { class: "phone__status" }, icon("phone"), t("phone.call.in"), " ", ui.clock),
      pill({ tone: "estimate", iconName: "info", text: t("phone.call.machineVoice") }),
    ),
    ui.caption,
    ui.sound,
    ui.record,
    h("div", { class: "phone__keys" }, keys.el),
    h("button", { type: "button", class: "btn btn--lg btn--block phone__end", onClick: () => endCall({ hangUp: true }) }, icon("x"), t("phone.call.end")),
  ];
}

function viewEnded() {
  return [
    h(
      "div",
      { class: "phone__panel", role: "status" },
      h("h1", null, t("phone.ended.title")),
      h("p", null, t("phone.ended.body")),
      h("button", { type: "button", class: "btn btn--primary btn--lg btn--block", onClick: callAgain }, icon("phone"), t("phone.ended.again")),
    ),
  ];
}

function viewDropped() {
  return [
    h(
      "div",
      { class: "phone__panel", role: "alert" },
      h("h1", null, t("phone.dropped.title")),
      h("p", null, t("phone.dropped.body")),
      h("button", { type: "button", class: "btn btn--primary btn--lg btn--block", onClick: connect }, icon("refresh"), t("phone.dropped.rejoin")),
    ),
  ];
}

// ----------------------------------------------------------------- captions
function renderCaption() {
  if (!ui.caption) return;
  // On the English page each line in the call's language is followed by its English words, so a visitor who
  // does not read Hindi can follow the call.
  const lines = S.call.lines.flatMap((line) => [
    h("p", { class: "phone__line", lang: line.lang, dir: direction(line.lang) }, line.text),
    ...(line.en && currentLang() === "en" ? [h("p", { class: "phone__line phone__line--en", lang: "en", dir: "ltr" }, line.en)] : []),
  ]);
  const extra = [
    ...(S.call.captionsOff ? [h("p", { class: "small muted" }, t("phone.captions.off"))] : []),
    ...(S.call.englishOff && currentLang() === "en" ? [h("p", { class: "small muted" }, t("phone.captions.englishOff"))] : []),
  ];
  ui.caption.replaceChildren(...(lines.length ? lines : [h("p", { class: "phone__line phone__line--wait" }, t("phone.call.starting"))]), ...extra);
}

function updateClock() {
  if (!ui.clock || !S.call) return;
  const s = Math.floor((performance.now() - S.call.startedAt) / 1000);
  ui.clock.textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

// -------------------------------------------------------------- record section
function renderRecord() {
  if (!ui.record) return;
  const call = S.call;
  const r = call.record;
  ui.meter = null;
  ui.count = null;
  if (!r) {
    ui.record.replaceChildren(...(call.sent ? [h("p", { class: "phone__sent small", role: "status" }, icon("check"), t("phone.record.sent"))] : []));
    return;
  }
  const phase = call.capture.phase;
  const mic = call.mic;
  const skip = h("button", { type: "button", class: "btn btn--block", onClick: skipQuestion }, t("phone.record.skip"));
  if (mic !== "ready") {
    const why = { pending: "phone.mic.pending", denied: "phone.mic.denied", insecure: "phone.mic.insecure", none: "phone.mic.none", busy: "phone.mic.busy", failed: "phone.mic.failed" }[mic ?? "pending"];
    ui.record.replaceChildren(
      h(
        "div",
        { class: "phone__record card stack", role: "group", "aria-label": t("phone.record.title") },
        h("p", { class: mic === "pending" ? "" : "phone__warn" }, mic === "pending" ? null : icon("alert"), t(why)),
        mic === "denied" || mic === "busy" || mic === "failed" ? h("button", { type: "button", class: "btn btn--block", onClick: openMic }, icon("mic"), t("phone.mic.allow")) : null,
        skip,
      ),
    );
    return;
  }
  const speak = h(
    "button",
    {
      type: "button",
      class: ["btn", "btn--lg", "btn--block", "phone__speak", phase === "idle" ? "btn--primary" : ""],
      "aria-pressed": String(phase !== "idle"),
      onPointerdown: speakDown,
      onPointerup: speakUp,
      onPointercancel: speakUp,
      onKeydown: speakKey,
      onContextmenu: (event) => event.preventDefault(),
    },
    icon("mic"),
    phase === "idle" ? t("phone.record.hold") : phase === "holding" ? t("phone.record.holding") : t("phone.record.tapStop"),
  );
  ui.meter = h("span", { class: "phone__meter-fill" });
  ui.count = h("span", { class: "num phone__count" }, phase === "idle" ? t("phone.record.upTo", { n: r.maxSeconds }) : t("phone.record.left", { n: r.maxSeconds }));
  ui.record.replaceChildren(
    h(
      "div",
      { class: ["phone__record", "card", "stack", phase === "idle" ? "" : "is-live"], role: "group", "aria-label": t("phone.record.title") },
      speak,
      h("div", { class: "phone__meter", role: "meter", "aria-label": t("phone.record.level"), "aria-valuemin": "0", "aria-valuemax": "100" }, ui.meter),
      h("div", { class: "row row--between" }, ui.count, phase === "idle" ? h("span", { class: "small muted" }, t("phone.record.tapHint")) : null),
      skip,
    ),
  );
}

async function openMic() {
  const call = S.call;
  if (!call) return;
  if (recorder?.isOpen) {
    call.mic = "ready";
    renderRecord();
    return;
  }
  call.mic = "pending";
  renderRecord();
  try {
    recorder ??= new Recorder(audio.ctx);
    await recorder.open();
    call.mic = "ready";
  } catch (err) {
    if (!(err instanceof MicError)) console.error(err);
    call.mic = err instanceof MicError ? err.kind : "failed";
  }
  if (S.call === call) renderRecord();
}

function startCapture(phase) {
  const call = S.call;
  const cap = call?.capture;
  if (!call?.record || !recorder?.isOpen || cap.phase !== "idle") return;
  audio.ctx.resume?.();
  recorder.start();
  cap.phase = phase;
  cap.startedAt = performance.now();
  cap.timer = setInterval(() => {
    const left = Math.max(0, call.record.maxSeconds - (performance.now() - cap.startedAt) / 1000);
    if (ui.count) ui.count.textContent = t("phone.record.left", { n: Math.ceil(left) });
    if (left <= 0) finishCapture();
  }, 100);
  const frame = () => {
    ui.meter?.style.setProperty("--level", `${Math.round((recorder?.level() ?? 0) * 100)}%`);
    cap.raf = requestAnimationFrame(frame);
  };
  cap.raf = requestAnimationFrame(frame);
  renderRecord();
}

function stopCaptureTimers() {
  const cap = S.call?.capture;
  if (!cap) return;
  clearInterval(cap.timer);
  cancelAnimationFrame(cap.raf);
  cap.timer = cap.raf = null;
  cap.phase = "idle";
}

function finishCapture() {
  const call = S.call;
  if (!call?.record || call.capture.phase === "idle") return;
  stopCaptureTimers();
  const wav = recorder.stop();
  sendRecording(wav);
}

function sendRecording(wav) {
  const call = S.call;
  S.link?.sendBinary(wav ?? new ArrayBuffer(0));
  call.record = null;
  call.sent = wav !== null;
  renderRecord();
}

function skipQuestion() {
  const call = S.call;
  if (!call?.record) return;
  if (call.capture.phase !== "idle") {
    stopCaptureTimers();
    recorder.stop(); // what was captured is dropped
  }
  S.link?.sendBinary(new ArrayBuffer(0));
  call.record = null;
  call.sent = false;
  renderRecord();
}

function speakDown(event) {
  event.preventDefault();
  event.currentTarget.setPointerCapture?.(event.pointerId);
  if (S.call.capture.phase === "toggled") finishCapture();
  else startCapture("holding");
}

function speakUp() {
  const cap = S.call?.capture;
  if (!cap || cap.phase !== "holding") return;
  if (performance.now() - cap.startedAt >= HOLD_MIN_MS) finishCapture();
  else {
    cap.phase = "toggled";
    renderRecord();
  }
}

function speakKey(event) {
  if (event.key !== " " && event.key !== "Enter") return;
  event.preventDefault();
  if (event.repeat) return;
  if (S.call.capture.phase === "idle") startCapture("toggled");
  else finishCapture();
}

// ----------------------------------------------------------- server messages
async function onPlay(message) {
  const call = S.call;
  if (!call) return;
  const { playback_id: id, urls } = message;
  call.sent = false;
  let buffers;
  try {
    buffers = await audio.loadClips(urls);
  } catch (err) {
    if (!(err instanceof ClipError)) console.error(err);
    const file = err instanceof ClipError ? err.url.split("/").slice(-2).join("/") : urls[0];
    S.note = { text: t("phone.audio.missing", { file }) };
    renderNote();
    S.link?.send({ type: "played", playback_id: id, ok: false });
    return;
  }
  if (S.call !== call) return;
  const langs = [...new Set(urls.map((u) => parseClipUrl(u)?.lang).filter(Boolean))];
  const captions = new Map();
  const english = new Map();
  // The call's own words and, on the English page, the English words under them. Each can fail alone.
  await Promise.all(
    langs.flatMap((lang) => [
      loadCaptions(lang).then(
        (c) => captions.set(lang, c.texts),
        (err) => {
          console.error(err);
          call.captionsOff = true;
        },
      ),
      ...(currentLang() === "en"
        ? [
            loadCaptions(lang, { english: true }).then(
              (c) => english.set(lang, c.en),
              (err) => {
                console.error(err);
                call.englishOff = true;
              },
            ),
          ]
        : []),
    ]),
  );
  const englishLines = englishForUrls(urls, english);
  if (S.call !== call) return;
  audio.stopRing();
  call.playing?.stop();
  const handle = audio.playSequence(buffers, {
    onStart: (i) => {
      if (i === 0) call.lines = [];
      const clip = parseClipUrl(urls[i]);
      const words = clip ? captions.get(clip.lang)?.[clip.key] : null;
      if (clip && words) addWords(call.lines, clip.lang, words, englishLines[i]);
      else if (clip && captions.has(clip.lang)) addWords(call.lines, clip.lang, t("phone.captions.noWords"));
      renderCaption();
    },
  });
  call.playing = handle;
  const result = await handle.done;
  if (call.playing === handle) call.playing = null;
  if (result === "ended" && S.call === call) S.link?.send({ type: "played", playback_id: id, ok: true });
}

async function onRecord(message) {
  const call = S.call;
  if (!call) return;
  call.record = { name: message.name, maxSeconds: Math.max(1, Math.round(message.max_seconds)) };
  call.sent = false;
  call.mic = recorder?.isOpen ? "ready" : "pending";
  renderRecord();
  if (call.mic === "pending") await openMic();
}

function onServerHangup(reason) {
  if (S.view === "ringing" || S.view === "waiting") {
    audio.stopRing();
    setView("ready", { note: { text: t(reason === "no_answer" ? "phone.stoppedRinging" : "phone.ended.title") } });
    return;
  }
  if (S.view === "call") endCall({ hangUp: false });
}

function onServerError(text) {
  const tooMany = /too many missed calls/i.test(text);
  if (tooMany && S.view === "waiting") {
    clearTimeout(timers.wait);
    setView("ready", { note: { text: t("phone.err.tooMany") } });
    return;
  }
  S.note = tooMany ? { text: t("phone.err.tooMany") } : { text, lang: "en" };
  renderNote();
}

function onMessage(message) {
  switch (message.type) {
    case "missed_call_received":
      S.waitedLong = false;
      clearTimeout(timers.wait);
      timers.wait = setTimeout(() => {
        S.waitedLong = true;
        if (S.view === "waiting") render();
      }, WAIT_LONG_MS);
      setView("waiting");
      break;
    case "ring":
      clearTimeout(timers.wait);
      audio.startRing();
      setView("ringing");
      break;
    case "play":
      onPlay(message);
      break;
    case "record":
      onRecord(message);
      break;
    case "hangup":
      onServerHangup(message.reason);
      break;
    case "error":
      onServerError(String(message.message ?? ""));
      break;
    default:
      break;
  }
}

// -------------------------------------------------------------------- taps
async function giveMissedCall() {
  try {
    await audio.unlock(); // this tap is what lets the page make sound
  } catch (err) {
    console.error(err);
    S.note = { text: t("phone.audio.blocked") };
    renderNote();
  }
  if (!S.link?.send({ type: "missed_call" })) setView("dropped");
}

async function answer() {
  let state = "closed";
  try {
    state = await audio.unlock();
  } catch (err) {
    console.error(err);
  }
  audio.stopRing();
  if (!S.link?.send({ type: "answer" })) {
    setView("dropped");
    return;
  }
  S.call = newCall();
  setView("call", { note: state === "running" ? null : { text: t("phone.audio.blocked") } });
  audio.onState = (st) => {
    if (!S.call) return;
    S.call.soundPaused = st !== "running";
    renderSound();
  };
  timers.clock = setInterval(updateClock, 1000);
}

function renderSound() {
  if (!ui.sound) return;
  ui.sound.replaceChildren(
    ...(S.call?.soundPaused
      ? [h("p", { class: "phone__note", role: "alert" }, icon("alert"), h("span", { class: "grow" }, t("phone.audio.paused")), h("button", { type: "button", class: "btn", onClick: () => audio.unlock().catch(console.error) }, t("phone.audio.resume")))]
      : []),
  );
}

function decline() {
  audio.stopRing();
  S.link?.send({ type: "reject" });
  setView("ready", { note: { text: t("phone.declined") } });
}

function pressKey(key) {
  audio.keyTone(key);
  if (!S.link?.send({ type: "dtmf", digit: key })) setView("dropped");
}

function cleanupCall() {
  clearInterval(timers.clock);
  S.call?.playing?.stop();
  stopCaptureTimers();
  recorder?.close();
  recorder = null;
  audio.stopRing();
  audio.onState = null;
}

function endCall({ hangUp }) {
  if (hangUp) S.link?.send({ type: "hangup" });
  cleanupCall();
  S.call = null;
  setView("ended");
}

function callAgain() {
  if (S.link?.open) setView("ready");
  else connect();
}

// -------------------------------------------------------------- connection
function describe(err) {
  if (err instanceof ApiError) {
    const key = { phone_sessions_busy: "phone.err.busy", phone_sessions_full: "phone.err.full", phone_off: "phone.err.off" }[err.code];
    if (key) return t(key);
    if (err.code === "network" || err.code === "timeout") return err.message;
    return err.message;
  }
  if (err instanceof LinkError) return t("phone.err.connect");
  console.error(err);
  return t("errors.generic");
}

async function connect() {
  S.link?.close();
  S.link = null;
  cleanupCall();
  S.call = null;
  setView("connecting");
  try {
    const link = await PhoneLink.connect();
    link.onMessage = onMessage;
    link.onClose = () => {
      if (S.link !== link) return;
      cleanupCall();
      S.call = null;
      audio.stopRing();
      clearTimeout(timers.wait);
      setView("dropped");
    };
    S.link = link;
    setView("ready");
  } catch (err) {
    S.error = { message: describe(err), retry: connect };
    setView("error");
  }
}

// -------------------------------------------------------------------- boot
async function boot() {
  const [modeResponse] = await Promise.all([fetch("/api/mode", { credentials: "same-origin" }), initI18n({ bundles: ["common", "phone"], audience: "patient" })]);
  const { mode } = await modeResponse.json();
  document.title = t("phone.page");
  try {
    await getMe();
  } catch (err) {
    if (!(err instanceof ApiError && err.isAuth)) throw err;
    if (mode !== "demo") {
      location.replace("/signin?next=/phone");
      return;
    }
    await api.post("/api/auth/visitor", { as: "abc" }); // a visitor may open one phone and nothing else
  }
  main.replaceChildren(noteHost, stage);
  await mountFrame({ mode: "patient" });
  document.addEventListener("ct:langchange", render);
  window.addEventListener("pagehide", () => {
    S.link?.close();
    cleanupCall();
    audio.close();
  });
  await connect();
}

boot().catch((err) => {
  console.error(err);
  main.removeAttribute("aria-busy");
  main.replaceChildren(h("p", { role: "alert", class: "phone__note" }, icon("alert"), err instanceof ApiError ? err.message : "Chaar Teeke could not load the phone. Reload the page."));
});
