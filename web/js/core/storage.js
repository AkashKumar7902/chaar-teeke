// localStorage for per-viewer conveniences only (language, a remembered tab). Every access is wrapped:
// private windows and blocked site data throw. A failed read is "nothing remembered", never an error
// shown to the person; state that must persist belongs on the server.

export function readPref(key) {
  try {
    return globalThis.localStorage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

export function writePref(key, value) {
  try {
    globalThis.localStorage?.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}
