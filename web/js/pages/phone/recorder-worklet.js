// Runs on the audio thread: hands the microphone's samples to the page in chunks of 2048.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunk = new Float32Array(2048);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    let read = 0;
    while (read < channel.length) {
      const n = Math.min(channel.length - read, this.chunk.length - this.filled);
      this.chunk.set(channel.subarray(read, read + n), this.filled);
      this.filled += n;
      read += n;
      if (this.filled === this.chunk.length) {
        this.port.postMessage(this.chunk.slice());
        this.filled = 0;
      }
    }
    return true;
  }
}
registerProcessor("ct-capture", CaptureProcessor);
