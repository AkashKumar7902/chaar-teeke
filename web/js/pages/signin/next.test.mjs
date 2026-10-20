import assert from "node:assert/strict";
import { test } from "node:test";
import { safeNextPath } from "./next.js";

const ORIGIN = "https://demo.example";

test("a path on this site is kept with its query and fragment", () => {
  assert.equal(safeNextPath("/clinic", ORIGIN), "/clinic");
  assert.equal(safeNextPath("/team/cluster/c1?x=1#top", ORIGIN), "/team/cluster/c1?x=1#top");
});

test("an address that the browser would send to another site is refused", () => {
  assert.equal(safeNextPath("/\t/x", ORIGIN), null);
  assert.equal(safeNextPath("/\n/x", ORIGIN), null);
  assert.equal(safeNextPath("/\r/x", ORIGIN), null);
  assert.equal(safeNextPath("//x", ORIGIN), null);
  assert.equal(safeNextPath("/\\x", ORIGIN), null);
  assert.equal(safeNextPath("\\\\x", ORIGIN), null);
  assert.equal(safeNextPath("https://x.example/clinic", ORIGIN), null);
  assert.equal(safeNextPath("javascript:alert(1)", ORIGIN), null);
});

test("nothing asked for means no destination", () => {
  assert.equal(safeNextPath(null, ORIGIN), null);
  assert.equal(safeNextPath("", ORIGIN), null);
});
