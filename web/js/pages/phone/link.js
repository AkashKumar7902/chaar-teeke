// The phone's connection: POST /api/phone/sessions, then the WebSocket it names. The messages in both
// directions are listed in chaarteeke/telephony/sim.py.

import { api } from "/js/core/api.js";

const READY_TIMEOUT_MS = 10000;

/** The socket would not open, was refused, or never said ready. */
export class LinkError extends Error {
  constructor(kind, detail = "") {
    super(`${kind}${detail ? `: ${detail}` : ""}`);
    this.name = "LinkError";
    this.kind = kind; // "refused" | "timeout" | "closed"
  }
}

export class PhoneLink {
  constructor(ws) {
    this.ws = ws;
    this.closing = false;
    this.onMessage = () => {};
    this.onClose = () => {};
  }

  /** Open a session and attach to it. Rejects with an ApiError (the server said no) or a LinkError. */
  static async connect() {
    const { ws_url: url } = await api.post("/api/phone/sessions");
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    const link = new PhoneLink(ws);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        link.closing = true;
        ws.close();
        reject(new LinkError("timeout"));
      }, READY_TIMEOUT_MS);
      ws.onmessage = (event) => {
        let message;
        try {
          message = JSON.parse(event.data);
        } catch {
          return;
        }
        if (message.type === "ready") {
          clearTimeout(timer);
          ws.onmessage = (e) => link.#message(e);
          resolve();
        }
      };
      ws.onclose = (event) => {
        clearTimeout(timer);
        reject(new LinkError(event.code === 4401 ? "refused" : "closed", String(event.code)));
      };
      ws.onerror = () => {}; // the close event that follows carries the news
    });
    ws.onclose = (event) => {
      if (!link.closing) link.onClose({ code: event.code });
    };
    return link;
  }

  #message(event) {
    if (typeof event.data !== "string") return;
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      console.error("phone: a message from the server was not JSON");
      return;
    }
    this.onMessage(message);
  }

  get open() {
    return this.ws.readyState === WebSocket.OPEN;
  }

  send(message) {
    if (!this.open) return false;
    this.ws.send(JSON.stringify(message));
    return true;
  }

  /** One binary frame: the recording as a WAV, or an empty frame for "nothing". */
  sendBinary(buffer) {
    if (!this.open) return false;
    this.ws.send(buffer);
    return true;
  }

  close() {
    this.closing = true;
    this.ws.close();
  }
}
