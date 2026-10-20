// Check a recording before it is sent: RIFF/WAVE, plain PCM, mono, 16-bit, with sound in it. The server
// checks again; this only saves a round trip and says what is wrong in the page's words.
// Pure functions over an ArrayBuffer, so they can be checked under node (wav.test.mjs).

/** How much of the file is read to find the format and the sound: a header sits in the first bytes. */
export const HEADER_BYTES = 1024 * 1024;

const ascii = (view, at) => String.fromCharCode(view.getUint8(at), view.getUint8(at + 1), view.getUint8(at + 2), view.getUint8(at + 3));

/**
 * @param {ArrayBuffer} buffer the first HEADER_BYTES (or all) of the file
 * @param {number} totalSize the size of the whole file, in bytes
 * @returns {{ok: true, info: {rate: number, seconds: number}} | {ok: false, problem: string}}
 *   problem is one of: not_wav, not_pcm, not_mono, not_16bit, empty, unreadable
 */
export function inspectWav(buffer, totalSize = buffer.byteLength) {
  const view = new DataView(buffer);
  if (buffer.byteLength < 12 || ascii(view, 0) !== "RIFF" || ascii(view, 8) !== "WAVE") return { ok: false, problem: "not_wav" };
  let pos = 12;
  let fmt = null;
  let dataBytes = null;
  while (pos + 8 <= buffer.byteLength) {
    const id = ascii(view, pos);
    const size = view.getUint32(pos + 4, true);
    const body = pos + 8;
    if (id === "fmt ") {
      if (size < 16 || body + 16 > buffer.byteLength) return { ok: false, problem: "unreadable" };
      fmt = { tag: view.getUint16(body, true), channels: view.getUint16(body + 2, true), rate: view.getUint32(body + 4, true), bits: view.getUint16(body + 14, true) };
    } else if (id === "data") {
      dataBytes = Math.max(0, Math.min(size, totalSize - body));
      break;
    }
    pos = body + size + (size % 2);
  }
  if (!fmt) return { ok: false, problem: "unreadable" };
  if (fmt.tag !== 1) return { ok: false, problem: "not_pcm" };
  if (fmt.channels !== 1) return { ok: false, problem: "not_mono" };
  if (fmt.bits !== 16) return { ok: false, problem: "not_16bit" };
  if (dataBytes === null) return { ok: false, problem: "unreadable" };
  if (dataBytes < 2) return { ok: false, problem: "empty" };
  return { ok: true, info: { rate: fmt.rate, seconds: dataBytes / 2 / fmt.rate } };
}

/** Read the start of a File and inspect it. */
export async function inspectFile(file) {
  const head = await file.slice(0, HEADER_BYTES).arrayBuffer();
  return inspectWav(head, file.size);
}
