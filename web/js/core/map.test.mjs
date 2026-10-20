import assert from "node:assert/strict";
import test from "node:test";

import { HEALTHY_MS, MAP_CRASH, RAMP_EDGES, canvasMatches, clusterBin, createResizeGuard, isCanvasDead, rampBin, wardProperties } from "./map.js";

const big = { clientWidth: 800, clientHeight: 500 };
const hidden = { clientWidth: 0, clientHeight: 0 };

test("isCanvasDead: a healthy canvas is alive", () => {
  assert.equal(isCanvasDead({ canvas: { isConnected: true, width: 800, height: 500 }, container: big }), false);
});

test("isCanvasDead: missing, detached, lost context and zero size in a sized container are dead", () => {
  assert.equal(isCanvasDead({ canvas: null, container: big }), true);
  assert.equal(isCanvasDead({ canvas: { isConnected: false, width: 800, height: 500 }, container: big }), true);
  assert.equal(isCanvasDead({ canvas: { isConnected: true, width: 800, height: 500 }, container: big, contextLost: true }), true);
  assert.equal(isCanvasDead({ canvas: { isConnected: true, width: 0, height: 0 }, container: big }), true);
  assert.equal(isCanvasDead({ canvas: { isConnected: true, width: 800, height: 0 }, container: big }), true);
});

test("isCanvasDead: a zero-size canvas in a hidden container is not dead (a hidden tab is fine)", () => {
  assert.equal(isCanvasDead({ canvas: { isConnected: true, width: 0, height: 0 }, container: hidden }), false);
});

test("canvasMatches: the canvas is the container's size times the pixel ratio, give or take a rounded pixel", () => {
  assert.equal(canvasMatches({ canvas: { width: 800, height: 500 }, container: big }), true);
  assert.equal(canvasMatches({ canvas: { width: 1600, height: 1000 }, container: big, pixelRatio: 2 }), true);
  assert.equal(canvasMatches({ canvas: { width: 1599, height: 1000 }, container: { clientWidth: 799.6, clientHeight: 500 }, pixelRatio: 2 }), true);
});

test("canvasMatches: a canvas left at an old size, or an old pixel ratio, does not match", () => {
  assert.equal(canvasMatches({ canvas: { width: 800, height: 300 }, container: big }), false, "the upper part of a taller box");
  assert.equal(canvasMatches({ canvas: { width: 500, height: 500 }, container: big }), false);
  assert.equal(canvasMatches({ canvas: { width: 800, height: 500 }, container: big, pixelRatio: 2 }), false, "the window moved to a sharper screen");
  assert.equal(canvasMatches({ canvas: null, container: big }), false);
});

test("canvasMatches: a hidden container has nothing to match", () => {
  assert.equal(canvasMatches({ canvas: { width: 0, height: 0 }, container: hidden }), true);
  assert.equal(canvasMatches({ canvas: { width: 800, height: 500 }, container: hidden }), true);
});

function guardHarness({ moving = false, deadSequence = [false], changed = true } = {}) {
  const calls = [];
  const timers = [];
  let dead = [...deadSequence];
  const guard = createResizeGuard({
    isMoving: () => moving,
    stop: () => calls.push("stop"),
    resize: () => calls.push("resize"),
    changed: () => changed,
    isDead: () => dead.shift() ?? false,
    rebuild: () => calls.push("rebuild"),
    onFailed: () => calls.push("failed"),
    timers: {
      setTimeout: (fn, ms) => {
        timers.push({ fn, ms, cleared: false });
        return timers.length - 1;
      },
      clearTimeout: (id) => {
        timers[id].cleared = true;
      },
    },
  });
  return { guard, calls, timers };
}

test("resizes are debounced by 120 ms: only the last timer runs", () => {
  const { guard, calls, timers } = guardHarness();
  guard.notify();
  guard.notify();
  guard.notify();
  assert.equal(timers.length, 3);
  assert.ok(timers.every((t) => t.ms === 120));
  assert.deepEqual(timers.map((t) => t.cleared), [true, true, false]);
  timers[2].fn();
  assert.deepEqual(calls, ["resize"]);
});

test("a running camera move is stopped before the resize", () => {
  const { guard, calls, timers } = guardHarness({ moving: true });
  guard.notify();
  timers[0].fn();
  assert.deepEqual(calls, ["stop", "resize"]);
});

test("a notify that changes nothing (a window resize event on its own) neither stops a move nor resizes", () => {
  const { guard, calls, timers } = guardHarness({ moving: true, changed: false });
  guard.notify();
  timers[0].fn();
  assert.deepEqual(calls, []);
});

test("a notify that changes nothing still rebuilds a canvas that died", () => {
  const { guard, calls, timers } = guardHarness({ changed: false, deadSequence: [true] });
  guard.notify();
  timers[0].fn();
  assert.deepEqual(calls, ["rebuild"]);
});

const resizeTimers = (timers) => timers.filter((t) => t.ms !== HEALTHY_MS && !t.cleared);
const healthyTimers = (timers) => timers.filter((t) => t.ms === HEALTHY_MS && !t.cleared);

test("a dead canvas is rebuilt once; a second death soon after gives up and says so", () => {
  const { guard, calls, timers } = guardHarness({ deadSequence: [true, true] });
  guard.notify();
  resizeTimers(timers)[0].fn();
  assert.deepEqual(calls, ["resize", "rebuild"]);
  assert.equal(guard.rebuilt, true);
  assert.equal(healthyTimers(timers).length, 1, "the rebuilt canvas is on probation");
  guard.notify();
  resizeTimers(timers).at(-1).fn();
  assert.deepEqual(calls, ["resize", "rebuild", "resize", "failed"]);
  assert.equal(healthyTimers(timers).length, 0, "giving up ends the probation");
});

test("after 30 s of healthy drawing the canvas can be rebuilt again", () => {
  assert.equal(HEALTHY_MS, 30000);
  // dead, then alive for the probation check and the next resize, then dead again much later
  const { guard, calls, timers } = guardHarness({ deadSequence: [true, false, false, true] });
  guard.notify();
  resizeTimers(timers)[0].fn(); // first death: rebuilt
  assert.equal(guard.rebuilt, true);
  healthyTimers(timers)[0].fn(); // 30 s later it is drawing: probation over
  assert.equal(guard.rebuilt, false, "the guard is armed again");
  guard.notify();
  resizeTimers(timers).at(-1).fn(); // healthy resize
  guard.notify();
  resizeTimers(timers).at(-1).fn(); // a new death, long after the first
  assert.deepEqual(calls.filter((c) => c === "rebuild").length, 2, "rebuilt a second time");
  assert.equal(calls.includes("failed"), false, "and not given up on");
});

test("a canvas that is still dead when probation ends stays marked as rebuilt", () => {
  const { guard, timers } = guardHarness({ deadSequence: [true, true] });
  guard.notify();
  resizeTimers(timers)[0].fn();
  healthyTimers(timers)[0].fn(); // isDead() answers true
  assert.equal(guard.rebuilt, true);
});

test("an easing-frame crash rebuilds once (after stopping the move); other messages are ignored", () => {
  const { guard, calls } = guardHarness({ moving: true });
  assert.equal(guard.crash("Cannot read properties of undefined"), false);
  assert.equal(guard.crash("TypeError in _onEaseFrame"), true);
  assert.deepEqual(calls, ["stop", "rebuild"]);
  assert.equal(guard.crash("another already running move"), true);
  assert.equal(calls.at(-1), "failed");
  assert.ok(MAP_CRASH.test("_onEaseFrame"));
});

test("dispose cancels the pending resize and the probation timer", () => {
  const { guard, timers } = guardHarness({ deadSequence: [true] });
  guard.notify();
  guard.dispose();
  assert.equal(timers[0].cleared, true);
  const again = guardHarness({ deadSequence: [true] });
  again.guard.notify();
  again.timers[0].fn();
  again.guard.dispose();
  assert.equal(healthyTimers(again.timers).length, 0);
});

test("rampBin puts shares in five ordered bins and refuses non-fractions", () => {
  assert.deepEqual([0, 0.39, 0.4, 0.54, 0.55, 0.69, 0.7, 0.79, 0.8, 1].map(rampBin), ["r1", "r1", "r2", "r2", "r3", "r3", "r4", "r4", "r5", "r5"]);
  assert.equal(RAMP_EDGES.length, 4);
  assert.throws(() => rampBin(62), RangeError);
  assert.throws(() => rampBin(null), RangeError);
});

test("clusterBin: too few is hatched, no data is plain, never a ramp colour", () => {
  assert.equal(clusterBin({ status: "too_few", share: null }), "hatch");
  assert.equal(clusterBin({ status: "no_data", share: null }), "nodata");
  assert.equal(clusterBin({ status: "ok", share: 0.82 }), "r5");
  assert.equal(clusterBin({ status: "bracketed", share: 0.5 }), "r2");
  assert.throws(() => clusterBin({ status: "mystery" }));
});

test("wardProperties: wards outside every cluster are nodata; simulated and look-here marks carry through", () => {
  const clusters = [
    { id: "C1", name: "One", ward_nos: [1, 2], share: { status: "ok", share: 0.6 }, look_here: true, simulated_bites: 0 },
    { id: "C2", name: "Two", ward_nos: [3], share: { status: "too_few", share: null }, look_here: false, simulated_bites: 2 },
  ];
  const props = wardProperties([1, 2, 3, 4], clusters, { simulatedWards: [4], selected: 2 });
  const by = Object.fromEntries(props.map((p) => [p.ward_no, p]));
  assert.equal(by[1].bin, "r3");
  assert.equal(by[1].look, true);
  assert.equal(by[2].selected, true);
  assert.equal(by[3].bin, "hatch");
  assert.equal(by[3].sim, true);
  assert.equal(by[4].bin, "nodata");
  assert.equal(by[4].sim, true);
  assert.equal(by[4].cluster_id, null);
});
