// Where to go after signing in. The address in ?next= is resolved the way the browser resolves it, so a
// tab, a new line or a backslash that the browser would drop or turn into a slash cannot turn a path on this
// site into an address on another one ("/\t/host" is "//host" to a browser).

/** The path (with query and fragment) on `origin` that `next` points to, or null when it points anywhere else. */
export function safeNextPath(next, origin) {
  if (!next) return null;
  try {
    const url = new URL(next, origin);
    return url.origin === origin ? url.pathname + url.search + url.hash : null;
  } catch {
    return null;
  }
}
