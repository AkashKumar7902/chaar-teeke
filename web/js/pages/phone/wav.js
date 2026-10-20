// Turning what the microphone heard into the WAV the server wants: mono, 16 kHz, 16-bit PCM, one file.
// Pure functions, so they can be checked under node (wav.test.mjs).

export const TARGET_RATE = 16000;

/** Mean-of-window downsampling: each output sample is the average of the input it covers, which also
 *  low-passes below the new Nyquist well enough for speech. `samples` is a Float32Array in [-1, 1]. */
export function resample(samples, fromRate, toRate = TARGET_RATE) {
  if (!(fromRate > 0) || !(toRate > 0)) throw new RangeError(`resample: bad rates ${fromRate} -> ${toRate}`);
  if (fromRate === toRate) return samples;
  if (fromRate < toRate) throw new RangeError(`resample: ${fromRate} Hz is below ${toRate} Hz and would have to be invented`);
  const ratio = fromRate / toRate;
  const length = Math.floor(samples.length / ratio);
  const out = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const start = i * ratio;
    const end = start + ratio;
    let sum = 0;
    let weight = 0;
    for (let j = Math.floor(start); j < Math.ceil(end) && j < samples.length; j += 1) {
      const w = Math.min(j + 1, end) - Math.max(j, start);
      sum += samples[j] * w;
      weight += w;
    }
    out[i] = weight > 0 ? sum / weight : 0;
  }
  return out;
}

/** Float [-1, 1] to 16-bit integers, clipped. */
export function toPcm16(samples) {
  const out = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i += 1) {
    const v = Math.max(-1, Math.min(1, samples[i]));
    out[i] = v < 0 ? Math.round(v * 0x8000) : Math.round(v * 0x7fff);
  }
  return out;
}

/** A complete WAV file (44-byte header and the samples, little endian) as an ArrayBuffer. */
export function encodeWav(pcm, rate = TARGET_RATE) {
  const bytes = pcm.length * 2;
  const buffer = new ArrayBuffer(44 + bytes);
  const view = new DataView(buffer);
  const text = (offset, s) => {
    for (let i = 0; i < s.length; i += 1) view.setUint8(offset + i, s.charCodeAt(i));
  };
  text(0, "RIFF");
  view.setUint32(4, 36 + bytes, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true); // fmt chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits
  text(36, "data");
  view.setUint32(40, bytes, true);
  for (let i = 0; i < pcm.length; i += 1) view.setInt16(44 + i * 2, pcm[i], true);
  return buffer;
}

/** Float32 samples at `rate` -> a 16 kHz mono 16-bit WAV, or null when nothing was captured. */
export function wavFrom(samples, rate) {
  if (samples.length === 0) return null;
  const resampled = resample(samples, rate, TARGET_RATE);
  if (resampled.length === 0) return null;
  return encodeWav(toPcm16(resampled), TARGET_RATE);
}

/** Join the chunks the worklet sent. */
export function joinChunks(chunks) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Float32Array(total);
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.length;
  }
  return out;
}
