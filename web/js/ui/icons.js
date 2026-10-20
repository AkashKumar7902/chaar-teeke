// Inline SVG icons drawn with currentColor (24 x 24 grid, 2 px stroke). Built with createElementNS so
// no HTML string is parsed. Every icon sits beside a word; icons never carry meaning alone.

import { s } from "../core/dom.js";

const PATHS = {
  info: [["circle", { cx: 12, cy: 12, r: 9.5 }], ["path", { d: "M12 11v6" }], ["path", { d: "M12 7.2v.1" }]],
  alert: [["path", { d: "M12 3.5 2.8 19.5h18.4L12 3.5Z" }], ["path", { d: "M12 10v4.5" }], ["path", { d: "M12 17.2v.1" }]],
  check: [["path", { d: "M4.5 12.5 9.5 17.5 19.5 6.5" }]],
  x: [["path", { d: "M6 6l12 12M18 6 6 18" }]],
  plus: [["path", { d: "M12 5v14M5 12h14" }]],
  minus: [["path", { d: "M5 12h14" }]],
  clock: [["circle", { cx: 12, cy: 12, r: 9.5 }], ["path", { d: "M12 7v5.5l3.5 2" }]],
  chevronDown: [["path", { d: "M6 9.5l6 6 6-6" }]],
  refresh: [["path", { d: "M20 11a8 8 0 1 0-2.3 6.2" }], ["path", { d: "M20 4.5V11h-6.5" }]],
  offline: [["path", { d: "M3 3l18 18" }], ["path", { d: "M2 9a15 15 0 0 1 4.5-2.8M22 9a15 15 0 0 0-8-3.5M5 12.5a10 10 0 0 1 4-2.3M19 12.5a10 10 0 0 0-3-2.1M8.5 16a5 5 0 0 1 4-1.4M12 19.5h.1" }]],
  user: [["circle", { cx: 12, cy: 8, r: 3.8 }], ["path", { d: "M4.5 20c.8-3.8 3.7-5.8 7.5-5.8s6.7 2 7.5 5.8" }]],
  signOut: [["path", { d: "M10 4H5.5v16H10" }], ["path", { d: "M14 8l4 4-4 4M18 12H9" }]],
  inbox: [["path", { d: "M3.5 13.5 6 5h12l2.5 8.5V19h-17v-5.5Z" }], ["path", { d: "M3.5 13.5H9a3 3 0 0 0 6 0h5.5" }]],
  hourglass: [["path", { d: "M6.5 3.5h11M6.5 20.5h11M7.5 3.5c0 5 4.5 5.5 4.5 8.5s-4.5 3.5-4.5 8.5M16.5 3.5c0 5-4.5 5.5-4.5 8.5s4.5 3.5 4.5 8.5" }]],
  phone: [["path", { d: "M6.5 3.5h3l1.5 4-2 1.5a11 11 0 0 0 6 6l1.5-2 4 1.5v3a2 2 0 0 1-2 2A16 16 0 0 1 4.5 5.5a2 2 0 0 1 2-2Z" }]],
  mic: [["rect", { x: 9, y: 3, width: 6, height: 11, rx: 3 }], ["path", { d: "M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3" }]],
  list: [["path", { d: "M8 6h12M8 12h12M8 18h12M4 6h.1M4 12h.1M4 18h.1" }]],
  map: [["path", { d: "M9 4 3.5 6v14L9 18l6 2 5.5-2V4L15 6 9 4Z" }], ["path", { d: "M9 4v14M15 6v14" }]],
  print: [["path", { d: "M7 8V3.5h10V8" }], ["path", { d: "M7 17H4.5V9h15v8H17" }], ["rect", { x: 7, y: 14, width: 10, height: 6.5 }]],
  dots: [["path", { d: "M5 12h.1M12 12h.1M19 12h.1" }]],
};

/** icon("alert") -> <svg class="icon">. Decorative by default (aria-hidden). */
export function icon(name, { className = "", label = null } = {}) {
  const parts = PATHS[name];
  if (!parts) throw new Error(`icon: unknown icon "${name}"`);
  const svg = s(
    "svg",
    {
      class: ["icon", className].filter(Boolean).join(" "),
      viewBox: "0 0 24 24",
      focusable: "false",
      ...(label ? { role: "img", "aria-label": label } : { "aria-hidden": "true" }),
    },
    parts.map(([tag, attrs]) => s(tag, attrs)),
  );
  return svg;
}

export const ICON_NAMES = Object.keys(PATHS);
