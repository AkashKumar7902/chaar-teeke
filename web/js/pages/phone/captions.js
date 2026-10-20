// The words behind each clip, from GET /api/phone/captions?lang=. A played URL such as
// /media/prompts/hi/FC_CLOSE.2.wav or /media/prompts/hi/slot/day_22.wav maps to a key under its language.

const CLIP = /^\/media\/prompts\/(hi|doi|ur)\/(.+)\.wav$/;
/** A segment that starts with punctuation (", Jammu ...") hangs on the word before it. */
const PUNCT_FIRST = /^[,;:.!?\u0964\u060c\u061b\u06d4\u061f]/u;
const loaded = new Map(); // "hi" or "hi+en" -> Promise<{texts, review_status, en?}>

/** {lang, key} for a played clip URL, or null when it is not a prompt clip. */
export function parseClipUrl(url) {
  const path = url.startsWith("http") ? new URL(url).pathname : url;
  const match = CLIP.exec(path);
  return match ? { lang: match[1], key: match[2] } : null;
}

/**
 * The captions of one call language, fetched once. `english`: also fetch the English words (`en`), which the
 * page shows under each line when it is read in English. A failed fetch is forgotten so the next play can retry.
 */
export function loadCaptions(lang, { english = false } = {}) {
  const id = english ? `${lang}+en` : lang;
  if (!loaded.has(id)) {
    const query = `lang=${encodeURIComponent(lang)}${english ? "&with=en" : ""}`;
    loaded.set(
      id,
      fetch(`/api/phone/captions?${query}`, { credentials: "same-origin" })
        .then((response) => {
          if (!response.ok) throw new Error(`captions ${id}: ${response.status}`);
          return response.json();
        })
        .catch((err) => {
          loaded.delete(id);
          throw err;
        }),
    );
  }
  return loaded.get(id);
}

const SLOT = "slot/";
const MISSING = "\u0000"; // marks a slot the English words could not fill
const WORD_KIND = [
  [/^slot\/(weekday|day|month)_/, "date"],
  [/^slot\/num_/, "number"],
  [/^slot\/time_/, "time"],
  [/^slot\/clinic_/, "clinic"],
  [/^slot\/ward_/, "ward"],
];
const kindOf = (key) => WORD_KIND.find(([pattern]) => pattern.test(key))?.[1] ?? null;

/**
 * The slot clips of one run (clips that play one after another with no word clip between them) as the values
 * they speak, in order: a date is one value (weekday, day, month), the digits of a number are one value. Each
 * value is its English words, or null when a clip has none.
 */
export function slotValues(keys, slots) {
  const values = [];
  let last = null;
  for (const key of keys) {
    const kind = kindOf(key);
    const words = slots[key] ?? null;
    const joins = last === kind && (kind === "number" || (kind === "date" && !key.startsWith(`${SLOT}weekday_`)));
    if (joins) {
      const before = values[values.length - 1];
      values[values.length - 1] = before === null || words === null ? null : `${before}${kind === "number" ? "" : " "}${words}`;
    } else values.push(words);
    last = kind;
  }
  return values;
}

/**
 * The English line for each clip of one play, from `en` (the reply's English words): the whole English line
 * of a spoken line sits on the first clip of that line, every other clip gets null. English words order
 * differently from the call's language, so a line is shown whole when it starts, not piece by piece. A line
 * whose English, or whose slot words, the data does not have gets null: nothing is made up.
 * @param {string[]} keys the clip keys of one play, in order (`FC_CLOSE.0`, `slot/num_4`, ...)
 * @param {{texts: Record<string,string>, slots: Record<string,string>}} en
 */
export function englishByClip(keys, en) {
  const out = keys.map(() => null);
  const runs = []; // the slot clips met since the last word clip, for the line being read
  let start = -1; // index of the first clip of the line being read
  const finish = () => {
    if (start < 0) return;
    const template = en.texts[keys[start]];
    if (template === undefined) return;
    const values = runs.map((run) => slotValues(run, en.slots));
    const filled = template.replace(/\{(\d+)\.(\d+)\}/g, (_all, r, i) => values[Number(r)]?.[Number(i)] ?? MISSING);
    if (!filled.includes(MISSING)) out[start] = filled;
  };
  let previous = null; // [id, k] of the last word clip
  let inRun = false;
  keys.forEach((key, index) => {
    if (key.startsWith(SLOT)) {
      if (!inRun) runs.push([]);
      inRun = true;
      runs[runs.length - 1].push(key);
      return;
    }
    inRun = false;
    const dot = key.lastIndexOf(".");
    const id = dot < 0 ? key : key.slice(0, dot);
    const k = dot < 0 ? null : Number(key.slice(dot + 1));
    const sameLine = previous !== null && k !== null && previous[0] === id && previous[1] !== null && k > previous[1];
    if (!sameLine) {
      finish();
      runs.length = 0;
      start = index;
    }
    previous = [id, k];
  });
  finish();
  return out;
}

/**
 * The English line for each URL of one play. A play can hold lines in more than one language (the language
 * menu), so the clips are read a language at a time, each with its own language's English words.
 * @param {string[]} urls played clip URLs
 * @param {Map<string, {texts: Record<string,string>, slots: Record<string,string>}>} enByLang
 */
export function englishForUrls(urls, enByLang) {
  const out = urls.map(() => null);
  const clips = urls.map(parseClipUrl);
  let from = 0;
  while (from < clips.length) {
    let to = from + 1;
    while (to < clips.length && clips[to]?.lang === clips[from]?.lang) to += 1;
    const en = clips[from] ? enByLang.get(clips[from].lang) : null;
    if (en) englishByClip(clips.slice(from, to).map((c) => c.key), en).forEach((line, i) => (out[from + i] = line));
    from = to;
  }
  // The language menu plays one line per language, and its English is the same sentence each time: say it once.
  let last = null;
  return out.map((line) => {
    const same = line !== null && line === last;
    last = line ?? last;
    return same ? null : line;
  });
}

const join = (before, words) => `${before}${PUNCT_FIRST.test(words) ? "" : " "}${words}`;

/**
 * Add one clip's words to the lines on screen: the same language continues a line, a new one starts a line.
 * `english` is the same clip in English, for a visitor reading the page in English; it follows the line in
 * the same way, and a clip with no English words adds none (nothing is made up).
 */
export function addWords(lines, lang, words, english = null) {
  const last = lines[lines.length - 1];
  if (last && last.lang === lang) {
    last.text = join(last.text, words);
    if (english) last.en = last.en ? join(last.en, english) : english;
  } else lines.push(english ? { lang, text: words, en: english } : { lang, text: words });
  return lines;
}
