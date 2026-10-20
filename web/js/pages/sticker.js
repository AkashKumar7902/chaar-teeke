// C5 Bite-card sticker: a sheet of 8 stickers for the clinic desk to print and stick on the bite card. Each one
// says, in Hindi, Dogri and Urdu, where to give the missed call, that the number is deleted 7 days after the course,
// that 9 stops the calls, and carries a QR code to /privacy. In the demo the number is the browser phone (a QR code
// to /phone and the word SIMULATOR); in the pilot it is the clinic's Chaar Teeke number, and when that is not set the
// page says so and prints nothing. The screen shows one sticker; the print stylesheet lays out eight on A4.

import { ApiError, getMode } from "/js/core/api.js";
import { bdi, h } from "/js/core/dom.js";
import { mountFrame } from "/js/core/frame.js";
import { initI18n, t } from "/js/core/i18n.js";
import { qrSrc } from "/js/core/qr.js";
import { errorFor, icon, skeleton } from "/js/ui/index.js";
import { clinicParam, loadConfig, pageTitle, withClinic } from "./clinic/common.js";
import { PER_SHEET, STICKER_LANGS, callTarget, canPrint, codeBase, codeLinks, privacyLabels } from "./clinic/sticker-model.js";

await initI18n({ bundles: ["common", "sticker"], audience: "staff" });
await mountFrame({ mode: "staff" });

const main = document.getElementById("main");
const RTL = new Set(["ur"]);

/** One QR code image: an <img> from the server, so the SVG cannot run script. A code that does not load says so. */
function code(url, size) {
  const img = h("img", {
    class: "sticker__qr",
    src: qrSrc(url),
    alt: t("sticker.codeAlt"),
    width: size,
    height: size,
    onError: () => img.replaceWith(h("p", { class: "sticker__qr-missing", role: "status" }, t("qr.missing"))),
  });
  return img;
}

function line(lang, key) {
  return h("p", { class: `sticker__line sticker__line--${key}`, lang, dir: RTL.has(lang) ? "rtl" : "ltr" }, t(`sticker.lines.${lang}.${key}`));
}

/** One sticker. The patient's lines are fixed Hindi, Dogri and Urdu; only the clinic and the number change. */
function sticker({ clinicName, target, links }) {
  const where =
    target.kind === "number"
      ? h("p", { class: "sticker__number num", dir: "ltr" }, target.number)
      : target.kind === "simulator"
        ? h("p", { class: "sticker__sim", dir: "ltr" }, t("sticker.simulator"))
        : h("p", { class: "sticker__none" }, t("sticker.noNumber"));
  return h(
    "article",
    { class: "sticker" },
    h(
      "div",
      { class: "sticker__main" },
      h("p", { class: "sticker__clinic" }, bdi(clinicName)),
      where,
      STICKER_LANGS.map((lang) => h("div", { class: "sticker__lang" }, line(lang, "call"), line(lang, "keep"))),
    ),
    h(
      "div",
      { class: "sticker__codes" },
      links.phone ? h("figure", { class: "sticker__code" }, code(links.phone, 120), h("figcaption", { dir: "ltr" }, t("sticker.simulator"))) : null,
      h(
        "figure",
        { class: "sticker__code" },
        code(links.privacy, 120),
        h("figcaption", null, privacyLabels({ hi: t("sticker.privacy.hi"), doi: t("sticker.privacy.doi"), ur: t("sticker.privacy.ur") }).join(" · ")),
      ),
    ),
  );
}

async function build() {
  main.setAttribute("aria-busy", "true");
  main.replaceChildren(skeleton({ lines: 4, block: true }));
  try {
    const [mode, cfg] = await Promise.all([getMode(), loadConfig(clinicParam())]);
    const target = callTarget({ mode: mode.mode, ctNumber: cfg.ct_number });
    const { base, localOnly } = codeBase({ publicUrl: mode.public_url, origin: location.origin });
    const one = sticker({ clinicName: cfg.clinic_name, target, links: codeLinks({ base, target }) });
    const print = h("button", { type: "button", class: "btn btn--primary", disabled: canPrint(target) ? null : true, onClick: () => window.print() }, icon("print"), t("sticker.print"));
    main.replaceChildren(
      ...[
        h(
          "div",
          { class: "page-title row--between sticker-screen" },
          h("h1", null, t("sticker.title")),
          h("div", { class: "row" }, h("a", { class: "btn", href: withClinic("/clinic", clinicParam()) }, t("sticker.today")), print),
        ),
        h("p", { class: "muted sticker-screen" }, t("sticker.intro")),
        localOnly && target.kind !== "unset" ? h("p", { class: "muted small sticker-screen" }, t("sticker.localOnly")) : null,
        h("p", { class: "sticker-draft small sticker-screen", role: "note" }, t("sticker.draft")),
        // the sheet: the screen shows the first sticker, the print stylesheet all eight
        h("div", { class: target.kind === "unset" ? "sticker-sheet sticker-sheet--unset" : "sticker-sheet" }, one, ...Array.from({ length: PER_SHEET - 1 }, () => one.cloneNode(true))),
      ].filter(Boolean),
    );
  } catch (err) {
    if (!(err instanceof ApiError)) console.error(err);
    main.replaceChildren(h("h1", null, t("sticker.title")), errorFor(err, { onRetry: build }));
  } finally {
    main.removeAttribute("aria-busy");
  }
}

document.title = pageTitle("sticker.title");
document.addEventListener("ct:langchange", () => {
  document.title = pageTitle("sticker.title");
  build();
});
build();
