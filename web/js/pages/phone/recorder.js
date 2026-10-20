// The microphone for "Hold to speak": opens it once per call, captures while the person speaks, and gives
// back a 16 kHz mono 16-bit WAV. Why it cannot work is said in words: denied, no secure page, no microphone,
// in use elsewhere.

import { joinChunks, wavFrom } from "./wav.js";

export class MicError extends Error {
  /** @param {"denied"|"insecure"|"none"|"busy"|"failed"} kind */
  constructor(kind, detail = "") {
    super(`${kind}${detail ? `: ${detail}` : ""}`);
    this.name = "MicError";
    this.kind = kind;
  }
}

export class Recorder {
  constructor(ctx) {
    this.ctx = ctx;
    this.stream = null;
    this.node = null;
    this.analyser = null;
    this.chunks = [];
    this.capturing = false;
    this.samples = null;
  }

  get isOpen() {
    return this.stream !== null;
  }

  async open() {
    if (this.stream) return;
    if (!globalThis.isSecureContext || !navigator.mediaDevices?.getUserMedia) throw new MicError("insecure");
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    } catch (err) {
      const kind = { NotAllowedError: "denied", SecurityError: "denied", NotFoundError: "none", OverconstrainedError: "none", NotReadableError: "busy", AbortError: "busy" }[err?.name] ?? "failed";
      throw new MicError(kind, err?.message ?? "");
    }
    try {
      await this.ctx.audioWorklet.addModule("/js/pages/phone/recorder-worklet.js");
      const source = this.ctx.createMediaStreamSource(stream);
      this.node = new AudioWorkletNode(this.ctx, "ct-capture", { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 });
      this.node.port.onmessage = (event) => {
        if (this.capturing) this.chunks.push(event.data);
      };
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 512;
      const silent = this.ctx.createGain();
      silent.gain.value = 0; // the node must reach the destination to run, and must not echo the voice
      source.connect(this.analyser);
      source.connect(this.node);
      this.node.connect(silent).connect(this.ctx.destination);
    } catch (err) {
      stream.getTracks().forEach((track) => track.stop());
      throw new MicError("failed", err?.message ?? "");
    }
    this.stream = stream;
    this.#enable(false);
  }

  #enable(on) {
    for (const track of this.stream?.getAudioTracks() ?? []) track.enabled = on;
  }

  start() {
    if (!this.stream) throw new MicError("failed", "the microphone is not open");
    this.chunks = [];
    this.capturing = true;
    this.#enable(true);
  }

  /** Stop capturing. Returns the WAV as an ArrayBuffer, or null when nothing was captured. */
  stop() {
    this.capturing = false;
    this.#enable(false);
    const wav = wavFrom(joinChunks(this.chunks), this.ctx.sampleRate);
    this.chunks = [];
    return wav;
  }

  /** The current loudness, 0 to 1 (the root mean square of the last few milliseconds). */
  level() {
    if (!this.analyser || !this.capturing) return 0;
    const data = new Float32Array(this.analyser.fftSize);
    this.analyser.getFloatTimeDomainData(data);
    let sum = 0;
    for (const v of data) sum += v * v;
    return Math.min(1, Math.sqrt(sum / data.length) * 4);
  }

  close() {
    this.capturing = false;
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    this.node?.disconnect();
    this.node = null;
    this.analyser = null;
  }
}
