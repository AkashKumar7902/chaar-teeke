// DOM helpers. Text always goes in as text nodes, never as HTML: transcripts, place names and
// patient-entered strings are untrusted (strict CSP, no innerHTML anywhere in the app).

const SVG_NS = "http://www.w3.org/2000/svg";
const FORBIDDEN_ATTRS = new Set(["innerHTML", "outerHTML", "srcdoc", "insertAdjacentHTML"]);

function appendChildren(parent, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) appendChildren(parent, child);
    else if (typeof child === "string" || typeof child === "number") {
      parent.appendChild(document.createTextNode(String(child)));
    } else if (child && typeof child.nodeType === "number") parent.appendChild(child);
    else throw new TypeError(`h(): cannot append ${Object.prototype.toString.call(child)}`);
  }
}

function applyAttrs(el, attrs) {
  if (!attrs) return;
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (FORBIDDEN_ATTRS.has(key)) throw new Error(`h(): "${key}" is not allowed (use children)`);
    if (key === "class" || key === "className") {
      el.setAttribute("class", Array.isArray(value) ? value.filter(Boolean).join(" ") : String(value));
    } else if (key === "dataset") {
      for (const [k, v] of Object.entries(value)) if (v !== null && v !== undefined) el.dataset[k] = String(v);
    } else if (key === "style") {
      // CSP forbids a style attribute; CSSOM is allowed. Only custom properties and plain properties as an object.
      if (typeof value === "string") throw new Error('h(): style must be an object, not a string (strict CSP)');
      for (const [k, v] of Object.entries(value)) {
        if (k.startsWith("--")) el.style.setProperty(k, String(v));
        else el.style[k] = String(v);
      }
    } else if (key.startsWith("on")) {
      if (typeof value !== "function") throw new Error(`h(): "${key}" must be a function (inline handlers are blocked by the CSP)`);
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "text") {
      el.textContent = String(value);
    } else if (value === true) {
      el.setAttribute(key, "");
    } else {
      el.setAttribute(key, String(value));
    }
  }
}

/** h("button", {class: "btn", onClick: fn}, "Save", icon) -> HTMLElement. */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  applyAttrs(el, attrs);
  appendChildren(el, children);
  return el;
}

/** Same as h() for SVG elements. */
export function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  applyAttrs(el, attrs);
  appendChildren(el, children);
  return el;
}

/** Untrusted text from data: isolated so right-to-left or mixed script cannot reorder the line around it. */
export function bdi(text) {
  const el = document.createElement("bdi");
  el.setAttribute("dir", "auto");
  el.textContent = text === null || text === undefined ? "" : String(text);
  return el;
}

/** Latin text or numbers inside right-to-left text ("DEMO DATA:", a clock): isolated and set left to right. */
export function ltr(text) {
  const el = document.createElement("bdi");
  el.setAttribute("dir", "ltr");
  el.textContent = text === null || text === undefined ? "" : String(text);
  return el;
}

export function frag(...children) {
  const f = document.createDocumentFragment();
  appendChildren(f, children);
  return f;
}

/** Append children with the same rules as h(): null and false are skipped, strings become text. */
export function append(el, ...children) {
  appendChildren(el, children);
  return el;
}

/** Replace all children of `el` with `children`. */
export function replace(el, ...children) {
  el.replaceChildren();
  appendChildren(el, children);
  return el;
}

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

let idCounter = 0;
/** A unique id for label/aria links, stable within one page load. */
export function uid(prefix = "ct") {
  idCounter += 1;
  return `${prefix}-${idCounter}`;
}

/** Run `fn` on the next frame (used to move focus after render). */
export function nextFrame(fn) {
  return requestAnimationFrame(() => fn());
}
