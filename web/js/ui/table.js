// Table helper. Columns say how to read a row; cells carry data-label so the table stacks on a phone.
// Cell content goes in as text or Nodes, never HTML.

import { h } from "../core/dom.js";

/**
 * dataTable({caption, columns: [{key, label, render?, numeric?}], rows, rowKey?, onRow?})
 * `render(row)` may return a string or Node. `empty` is a Node shown instead when there are no rows.
 */
export function dataTable({ caption, columns, rows, empty = null, rowClass = null, stack = true }) {
  if (rows.length === 0 && empty) return empty;
  const head = h(
    "thead",
    null,
    h(
      "tr",
      null,
      columns.map((c) => h("th", { scope: "col", class: c.numeric ? "cell-num" : null }, c.label)),
    ),
  );
  const body = h(
    "tbody",
    null,
    rows.map((row) =>
      h(
        "tr",
        { class: rowClass ? rowClass(row) : null },
        columns.map((c) =>
          h(
            "td",
            { "data-label": c.label, class: c.numeric ? "cell-num" : null },
            c.render ? c.render(row) : (row[c.key] ?? ""),
          ),
        ),
      ),
    ),
  );
  const table = h("table", { class: ["table", stack ? "table--stack" : ""] }, caption ? h("caption", null, caption) : null, head, body);
  return h("div", { class: "table-wrap" }, table);
}
