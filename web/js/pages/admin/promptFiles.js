// Which audio files a prompt needs. The server splits a prompt's text at its {slots}; each stretch of
// words becomes one file: <id>.wav when there are no slots, else <id>.0.wav, <id>.1.wav ... This mirrors
// chaarteeke/ivr/prompts.py (_parse). GET /api/admin/prompts says only whether the whole prompt has its
// audio, so the per-file list is rebuilt here and checked against the server's own "missing" list.

const SLOT = /\{(\w+)\}/g;
const WORD = /[\p{L}\p{N}_]/u;

/** @returns {{name: string, text: string, part: number|null}[]} `name` is the file name without language folder or .wav */
export function segmentsOf(id, text) {
  const slots = [...text.matchAll(SLOT)];
  if (slots.length === 0) return WORD.test(text) ? [{ name: id, text, part: null }] : [];
  const out = [];
  let k = 0;
  let pos = 0;
  const literal = (chunk) => {
    if (WORD.test(chunk)) {
      out.push({ name: `${id}.${k}`, text: chunk.trim(), part: k });
      k += 1;
    }
  };
  for (const m of slots) {
    literal(text.slice(pos, m.index));
    pos = m.index + m[0].length;
  }
  literal(text.slice(pos));
  return out;
}
