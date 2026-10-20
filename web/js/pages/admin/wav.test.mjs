import assert from "node:assert/strict";
import { test } from "node:test";
import { inspectWav } from "./wav.js";

function wav({ tag = 1, channels = 1, bits = 16, rate = 16000, samples = 160, extra = false } = {}) {
  const dataBytes = samples * (bits / 8) * channels;
  const parts = [];
  const u32 = (n) => Uint8Array.of(n & 255, (n >> 8) & 255, (n >> 16) & 255, (n >>> 24) & 255);
  const u16 = (n) => Uint8Array.of(n & 255, (n >> 8) & 255);
  const text = (s) => Uint8Array.from(s, (c) => c.charCodeAt(0));
  const fmt = [...u16(tag), ...u16(channels), ...u32(rate), ...u32((rate * channels * bits) / 8), ...u16((channels * bits) / 8), ...u16(bits)];
  parts.push(text("RIFF"), u32(0), text("WAVE"), text("fmt "), u32(16), Uint8Array.from(fmt));
  if (extra) parts.push(text("LIST"), u32(5), new Uint8Array(6)); // odd size: padded to even
  parts.push(text("data"), u32(dataBytes), new Uint8Array(dataBytes));
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out.buffer;
}

test("a mono 16-bit PCM file passes and reports its length", () => {
  const r = inspectWav(wav({ samples: 16000 }));
  assert.equal(r.ok, true);
  assert.equal(r.info.rate, 16000);
  assert.equal(r.info.seconds, 1);
});

test("a chunk before the data (with an odd size) is skipped", () => {
  assert.equal(inspectWav(wav({ extra: true })).ok, true);
});

test("each wrong format names its problem", () => {
  assert.equal(inspectWav(new TextEncoder().encode("not a wav file at all").buffer).problem, "not_wav");
  assert.equal(inspectWav(wav({ tag: 3 })).problem, "not_pcm");
  assert.equal(inspectWav(wav({ channels: 2 })).problem, "not_mono");
  assert.equal(inspectWav(wav({ bits: 8 })).problem, "not_16bit");
  assert.equal(inspectWav(wav({ samples: 0 })).problem, "empty");
});

test("a header with no data chunk is unreadable, not accepted", () => {
  const full = new Uint8Array(wav());
  assert.equal(inspectWav(full.slice(0, 36).buffer).problem, "unreadable");
});

test("a file cut short counts the sound that is there", () => {
  const full = wav({ samples: 1000 });
  const r = inspectWav(full.slice(0, 44 + 20), full.byteLength);
  assert.equal(r.ok, true);
});
