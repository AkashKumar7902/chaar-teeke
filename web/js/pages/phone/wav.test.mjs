import assert from "node:assert/strict";
import { test } from "node:test";
import { encodeWav, joinChunks, resample, toPcm16, wavFrom } from "./wav.js";

test("resample 48 kHz to 16 kHz keeps a third of the samples and the average", () => {
  const input = Float32Array.from({ length: 48 }, (_, i) => (i % 3 === 0 ? 0.3 : 0.0));
  const out = resample(input, 48000, 16000);
  assert.equal(out.length, 16);
  for (const v of out) assert.ok(Math.abs(v - 0.1) < 1e-6);
});

test("resample handles a rate that does not divide evenly (44.1 kHz)", () => {
  const input = new Float32Array(44100).fill(0.5);
  const out = resample(input, 44100, 16000);
  assert.equal(out.length, 16000);
  assert.ok(out.every((v) => Math.abs(v - 0.5) < 1e-5));
});

test("resample refuses to invent samples", () => {
  assert.throws(() => resample(new Float32Array(8), 8000, 16000), RangeError);
});

test("toPcm16 clips and scales", () => {
  assert.deepEqual([...toPcm16(Float32Array.of(0, 1, -1, 2, -2, 0.5))], [0, 32767, -32768, 32767, -32768, 16384]);
});

test("encodeWav writes a 16 kHz mono 16-bit header", () => {
  const wav = encodeWav(Int16Array.of(1, -2, 3));
  const v = new DataView(wav);
  assert.equal(wav.byteLength, 44 + 6);
  assert.equal(String.fromCharCode(...new Uint8Array(wav, 0, 4)), "RIFF");
  assert.equal(String.fromCharCode(...new Uint8Array(wav, 8, 4)), "WAVE");
  assert.equal(v.getUint32(4, true), 36 + 6);
  assert.equal(v.getUint16(20, true), 1);
  assert.equal(v.getUint16(22, true), 1);
  assert.equal(v.getUint32(24, true), 16000);
  assert.equal(v.getUint16(34, true), 16);
  assert.equal(v.getUint32(40, true), 6);
  assert.equal(v.getInt16(48, true), 3);
});

test("wavFrom gives null for nothing and a file for something", () => {
  assert.equal(wavFrom(new Float32Array(0), 48000), null);
  const wav = wavFrom(new Float32Array(48000).fill(0.2), 48000);
  assert.equal(wav.byteLength, 44 + 16000 * 2);
});

test("joinChunks concatenates in order", () => {
  assert.deepEqual([...joinChunks([Float32Array.of(1, 2), Float32Array.of(3)])], [1, 2, 3]);
});
