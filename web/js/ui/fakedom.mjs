// A tiny stand-in for the browser's DOM, only as much as dom.js, icons.js, pill.js and alert.js use, so the
// UI components can be unit-tested under node without a browser. Import it before the component.

class FakeNode {
  constructor(tag, ns = null) {
    this.tagName = tag;
    this.namespaceURI = ns;
    this.nodeType = 1;
    this.children = [];
    this.attrs = new Map();
    this.listeners = new Map();
    this.style = { setProperty() {} };
    this.dataset = {};
  }
  setAttribute(k, v) {
    this.attrs.set(k, String(v));
  }
  getAttribute(k) {
    return this.attrs.get(k) ?? null;
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  append(...nodes) {
    for (const n of nodes) this.children.push(typeof n === "string" ? new FakeText(n) : n);
  }
  addEventListener(type, fn) {
    this.listeners.set(type, fn);
  }
  get textContent() {
    return this.children.map((c) => c.textContent).join("");
  }
  set textContent(value) {
    this.children = [new FakeText(String(value))];
  }
  /** Every descendant element (depth first) whose class list has `name`. */
  byClass(name) {
    const out = [];
    const walk = (node) => {
      for (const c of node.children) {
        if (c.nodeType !== 1) continue;
        if ((c.getAttribute("class") ?? "").split(/\s+/).includes(name)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
}

class FakeText {
  constructor(text) {
    this.nodeType = 3;
    this.textContent = text;
  }
}

globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (ns, tag) => new FakeNode(tag, ns),
  createTextNode: (text) => new FakeText(text),
};
