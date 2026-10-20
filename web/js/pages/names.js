// R2 Known names. One job: search a name, see which ward it points to, and teach the system a new name.
// The list holds the ward names (from the ward file) and the names the reviewer taught (GET /api/aliases).
// Names are untrusted text: they enter the page as text inside <bdi dir="auto">.

import { ApiError, api } from "/js/core/api.js";
import { bdi, h } from "/js/core/dom.js";
import { formatDate } from "/js/core/format.js";
import { t } from "/js/core/i18n.js";
import { emptyState, field, icon, pill, selectInput, textInput, toast, unsentNote } from "/js/ui/index.js";
import { boot, dataView } from "./oversight/lib.js";
import { loadWardList } from "./review/wards.js";

const me = await boot(["names"], "names.title");

const PAGE = 20;
const state = { data: null, find: "", shown: PAGE };

const main = document.getElementById("main");
const heading = h("h1", null, t("names.title"));
const body = h("div", { class: "stack" });
main.append(h("div", { class: "page-title" }, heading), body);

// the persistent halves: the search and list, and the add form (typed text survives a reload)
const listHost = h("div", { class: "stack stack--s", "aria-live": "polite" });
const wrap = h("div", { class: "nm-grid" });
let searchField = null;
let form = null;
let formLang = null;

const wardLabel = (no, name) => (name ? t("alert.ward", { no, name }) : t("names.wardNo", { no }));

function rows() {
  const { aliases, wards } = state.data;
  const byNo = new Map(wards.map((w) => [w.ward_no, w.ward_name]));
  const learned = aliases.map((a) => ({ kind: "alias", text: a.text, ward_no: a.ward_no, ward_name: a.ward_name ?? byNo.get(a.ward_no) ?? null, by: a.added_by, at: a.added_at }));
  const names = wards.map((w) => ({ kind: "ward", text: w.ward_name ?? "", ward_no: w.ward_no, ward_name: w.ward_name }));
  const q = state.find.trim().toLowerCase();
  const all = [...learned, ...names];
  if (!q) return { all, hit: all, learned: learned.length };
  const asNumber = /^(?:ward\s*)?(\d{1,3})$/i.exec(q);
  const hit = all.filter((r) => (asNumber ? r.ward_no === Number(asNumber[1]) : r.text.toLowerCase().includes(q) || (r.ward_name ?? "").toLowerCase().includes(q)));
  return { all, hit, learned: learned.length };
}

function row(r) {
  const who = r.by ? String(r.by).replace(/^(staff|visitor):/, "") : null;
  return h(
    "li",
    { class: "list__row nm-row" },
    h(
      "div",
      { class: "list__main" },
      h("span", { class: "list__title" }, bdi(r.text)),
      h("span", { class: "list__meta" }, r.kind === "ward" ? t("names.wardNo", { no: r.ward_no }) : wardLabel(r.ward_no, r.ward_name)),
      r.kind === "alias" && who ? h("span", { class: "list__meta small" }, t("names.added", { who, when: formatDate(r.at) })) : null,
    ),
    h("div", { class: "list__side" }, r.kind === "alias" ? pill({ tone: "primary", iconName: "check", text: t("names.kind.alias") }) : pill({ text: t("names.kind.ward") })),
  );
}

function paintList() {
  const { hit, learned } = rows();
  const shown = hit.slice(0, state.shown);
  const parts = [];
  if (learned === 0) parts.push(h("p", { class: "nm-note muted" }, icon("info"), t("names.noLearned")));
  if (hit.length > 0) parts.push(h("p", { class: "small muted", role: "status" }, t("names.count", { shown: shown.length, total: hit.length })));
  if (hit.length === 0) parts.push(emptyState({ title: t("names.noMatch", { q: state.find.trim() }) }));
  else {
    parts.push(h("ul", { class: "list", "aria-label": t("names.list") }, shown.map(row)));
    if (hit.length > shown.length) {
      parts.push(
        h("button", { type: "button", class: "btn", onClick: () => ((state.shown += PAGE), paintList()) }, t("names.more")),
      );
    }
  }
  listHost.replaceChildren(...parts);
}

function buildSearch() {
  const input = textInput({ name: "find", type: "search", inputmode: "search", autocomplete: "off", placeholder: t("names.search.placeholder"), value: state.find });
  input.addEventListener("input", () => {
    state.find = input.value;
    state.shown = PAGE;
    paintList();
  });
  searchField = field({ label: t("names.search.label"), control: input });
  return searchField.el;
}

// ------------------------------------------------------------------------------------------ the add form
function buildForm() {
  const name = textInput({ name: "alias", maxlength: 80, autocomplete: "off" });
  const options = [{ value: "", label: t("names.add.choose") }, ...state.data.wards.map((w) => ({ value: String(w.ward_no), label: `${w.ward_no} ${w.ward_name ?? ""}`.trim() }))];
  const ward = selectInput({ name: "ward", options, value: "" });
  const nameField = field({ label: t("names.add.name"), hint: t("names.add.nameHint"), control: name });
  const wardField = field({ label: t("names.add.ward"), control: ward });
  const slot = h("div", { class: "nm-slot" });
  const submit = h("button", { type: "submit", class: "btn btn--primary", "aria-disabled": me.can_write ? null : "true" }, icon("plus"), t("names.add.submit"));
  const el = h("form", { class: "card stack nm-form", novalidate: true }, h("h2", { class: "section-title" }, t("names.add.title")), nameField.el, wardField.el, slot, h("div", { class: "row" }, submit));
  el.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!me.can_write) {
      toast(t("names.readOnly"), { tone: "attention" });
      return;
    }
    nameField.setError(name.value.trim() ? null : t("names.add.nameRequired"));
    wardField.setError(ward.value ? null : t("names.add.wardRequired"));
    slot.replaceChildren();
    if (!name.value.trim() || !ward.value) {
      (name.value.trim() ? ward : name).focus();
      return;
    }
    if (submit.getAttribute("aria-busy") === "true") return;
    submit.setAttribute("aria-busy", "true");
    try {
      const before = state.data.aliases.length;
      const list = await api.post("/api/aliases", { text: name.value.trim(), ward_no: Number(ward.value) });
      state.data.aliases = list;
      if (list.length === before) {
        nameField.setError(t("names.add.exists"));
      } else {
        const newest = list.reduce((a, b) => (b.id > a.id ? b : a));
        toast(t("names.add.added", { text: newest.text }));
        name.value = "";
        nameField.setError(null);
        state.find = "";
        searchField.control.value = "";
        state.shown = PAGE;
      }
      paintList();
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      if (err.isReadOnly) return; // the frame says so
      if (err.code === "bad_request") nameField.setError(err.message);
      else if (err.code === "unknown_ward") wardField.setError(err.message);
      else slot.replaceChildren(unsentNote(), h("p", { class: "field__error" }, icon("alert"), err.message));
      if (err.isNetwork) slot.replaceChildren(unsentNote());
    } finally {
      submit.removeAttribute("aria-busy");
    }
  });
  return el;
}

function render(data) {
  state.data = data;
  if (searchField === null || formLang !== document.documentElement.lang) {
    formLang = document.documentElement.lang;
    searchField = null;
    const left = h("section", { class: "stack", "aria-label": t("names.list") }, buildSearch(), listHost);
    form = buildForm();
    wrap.replaceChildren(left, form);
  }
  paintList();
  return wrap;
}

dataView(body, {
  load: async () => {
    const [aliases, wards] = await Promise.all([api.get("/api/aliases"), loadWardList().catch((err) => {
      console.error(err);
      throw new ApiError(0, "bad_response", t("names.add.failed"));
    })]);
    return { aliases, wards };
  },
  render,
  events: ["review"],
});

document.addEventListener("ct:langchange", () => {
  heading.textContent = t("names.title");
});
