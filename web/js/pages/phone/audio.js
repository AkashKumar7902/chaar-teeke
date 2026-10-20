// Sound for the browser phone, all through one AudioContext that a tap unlocks: the ring, key tones, the
// prompt clips played back to back, and the clip decoding. Nothing is an audio file except the prompts.

const RING_EVERY_MS = 3000;
const DTMF = {
  rows: { 1: 697, 2: 697, 3: 697, 4: 770, 5: 770, 6: 770, 7: 852, 8: 852, 9: 852, "*": 941, 0: 941, "#": 941 },
  cols: { 1: 1209, 2: 1336, 3: 1477, 4: 1209, 5: 1336, 6: 1477, 7: 1209, 8: 1336, 9: 1477, "*": 1209, 0: 1336, "#": 1477 },
};

/** A clip that could not be fetched or decoded. `url` is what the server asked the page to play. */
export class ClipError extends Error {
  constructor(url, why) {
    super(`${url}: ${why}`);
    this.name = "ClipError";
    this.url = url;
  }
}

export class PhoneAudio {
  constructor() {
    this.ctx = null;
    this.clips = new Map(); // url -> AudioBuffer
    this.ringTimer = null;
    this.ringGain = null;
    this.onState = null; // (state) => void when the context is suspended or resumed
  }

  /** Create the context on first use and resume it. Call it inside a tap: browsers refuse sound before one. */
  async unlock() {
    if (!this.ctx) {
      const Context = globalThis.AudioContext ?? globalThis.webkitAudioContext;
      if (!Context) throw new Error("This browser has no Web Audio.");
      this.ctx = new Context();
      this.ctx.addEventListener("statechange", () => this.onState?.(this.ctx.state));
    }
    if (this.ctx.state !== "running") await this.ctx.resume();
    return this.ctx.state;
  }

  get state() {
    return this.ctx?.state ?? "closed";
  }

  // --------------------------------------------------------------------- ring
  startRing() {
    if (!this.ctx || this.ringTimer !== null) return;
    const ctx = this.ctx;
    const gain = ctx.createGain();
    gain.gain.value = 0.0001;
    gain.connect(ctx.destination);
    this.ringGain = gain;
    const burst = (at) => {
      for (const hz of [440, 480]) {
        const osc = ctx.createOscillator();
        osc.frequency.value = hz;
        osc.connect(gain);
        osc.start(at);
        osc.stop(at + 0.4);
      }
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.linearRampToValueAtTime(0.12, at + 0.03);
      gain.gain.setValueAtTime(0.12, at + 0.37);
      gain.gain.linearRampToValueAtTime(0.0001, at + 0.4);
    };
    const ring = () => {
      const now = ctx.currentTime + 0.02;
      burst(now);
      burst(now + 0.6);
    };
    ring();
    this.ringTimer = setInterval(ring, RING_EVERY_MS);
  }

  stopRing() {
    if (this.ringTimer !== null) clearInterval(this.ringTimer);
    this.ringTimer = null;
    if (this.ringGain) {
      this.ringGain.gain.cancelScheduledValues(0);
      this.ringGain.gain.value = 0;
      this.ringGain.disconnect();
      this.ringGain = null;
    }
  }

  // --------------------------------------------------------------- key tones
  keyTone(key) {
    if (!this.ctx || this.ctx.state !== "running" || !(key in DTMF.rows)) return;
    const ctx = this.ctx;
    const gain = ctx.createGain();
    const at = ctx.currentTime;
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.linearRampToValueAtTime(0.1, at + 0.01);
    gain.gain.setValueAtTime(0.1, at + 0.11);
    gain.gain.linearRampToValueAtTime(0.0001, at + 0.13);
    gain.connect(ctx.destination);
    for (const hz of [DTMF.rows[key], DTMF.cols[key]]) {
      const osc = ctx.createOscillator();
      osc.frequency.value = hz;
      osc.connect(gain);
      osc.start(at);
      osc.stop(at + 0.14);
    }
  }

  // ------------------------------------------------------------------- clips
  /** Fetch and decode every clip, in parallel; a clip already decoded is reused. Rejects with a ClipError. */
  async loadClips(urls) {
    if (!this.ctx) throw new Error("The sound was not unlocked.");
    return Promise.all(urls.map((url) => this.#decode(url)));
  }

  async #decode(url) {
    const known = this.clips.get(url);
    if (known) return known;
    let response;
    try {
      response = await fetch(url, { credentials: "same-origin" });
    } catch {
      throw new ClipError(url, "could not be fetched");
    }
    if (!response.ok) throw new ClipError(url, `answered ${response.status}`);
    try {
      const buffer = await this.ctx.decodeAudioData(await response.arrayBuffer());
      this.clips.set(url, buffer);
      return buffer;
    } catch {
      throw new ClipError(url, "is not audio the browser can play");
    }
  }

  /**
   * Play buffers back to back, with no gap. onStart(i) fires as clip i starts. Returns {done, stop}: `done`
   * resolves "ended" after the last clip, or "stopped" when stop() cut it short.
   */
  playSequence(buffers, { onStart } = {}) {
    const ctx = this.ctx;
    const sources = [];
    const timers = [];
    let settle;
    const done = new Promise((resolve) => {
      settle = resolve;
    });
    let at = ctx.currentTime + 0.05;
    buffers.forEach((buffer, i) => {
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      source.start(at);
      sources.push(source);
      timers.push(setTimeout(() => onStart?.(i), Math.max(0, (at - ctx.currentTime) * 1000)));
      at += buffer.duration;
    });
    if (sources.length === 0) settle("ended");
    else sources[sources.length - 1].onended = () => settle("ended");
    return {
      done,
      stop: () => {
        for (const t of timers) clearTimeout(t);
        for (const s of sources) {
          s.onended = null;
          try {
            s.stop();
          } catch {
            // a source that never started cannot be stopped again
          }
        }
        settle("stopped");
      },
    };
  }

  close() {
    this.stopRing();
    this.ctx?.close().catch(() => {});
    this.ctx = null;
  }
}
