// What the bite-card sticker shows, as plain functions (no DOM), so the rules are tested under node.
// The sticker is for the patient: the lines are in Hindi, Dogri and Urdu whatever language the staff page is in.

/** The order of the three lines on a sticker. */
export const STICKER_LANGS = ["hi", "doi", "ur"];
/** Stickers on one A4 sheet (2 across, 4 down: 105 x 74 mm each). */
export const PER_SHEET = 8;

/**
 * Where the patient gives the missed call.
 *  mode: "demo" | "pilot" (from /api/mode); ctNumber: `ct_number` from /api/clinic/config (null when not set)
 * Demo: the browser phone, which is a simulator (the sticker says so). Pilot: the clinic's Chaar Teeke number, and
 * when it is not set nothing stands in for it: the sticker says so.
 */
export function callTarget({ mode, ctNumber }) {
  if (mode === "demo") return { kind: "simulator" };
  if (typeof ctNumber === "string" && ctNumber.trim() !== "") return { kind: "number", number: ctNumber.trim() };
  return { kind: "unset" };
}

/** Where the codes point. With no public address the page's own origin is used, and the page says it is local only. */
export function codeBase({ publicUrl, origin }) {
  const base = (publicUrl || origin || "").replace(/\/+$/, "");
  if (!/^https?:\/\//.test(base)) throw new Error(`sticker: no web address to point the codes at (got "${base}")`);
  return { base, localOnly: !publicUrl };
}

/** The two codes: the privacy notice always; the browser phone only for the simulator. */
export function codeLinks({ base, target }) {
  return { privacy: `${base}/privacy`, phone: target.kind === "simulator" ? `${base}/phone` : null };
}

/** Can the sheet be printed? A sticker with no number to ring is not printed. */
export function canPrint(target) {
  return target.kind !== "unset";
}

/** The privacy label in each language that has its own word, once (Hindi and Dogri share one). */
export function privacyLabels(labels) {
  return [...new Set(STICKER_LANGS.map((lang) => labels[lang]))];
}
