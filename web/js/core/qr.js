// QR code from the server (GET /api/phone/qr.svg?url=). It is an <img>, so the SVG cannot run script.
// If the image does not load, the page shows the plain link instead and says so. Nothing else stands in.

import { h } from "./dom.js";
import { t } from "./i18n.js";

export const QR_ENDPOINT = "/api/phone/qr.svg";

export function qrSrc(url) {
  return `${QR_ENDPOINT}?${new URLSearchParams({ url }).toString()}`;
}

/** qrCode(url) -> <figure> with the QR and the link under it. */
export function qrCode(url, { size = 176 } = {}) {
  const protocol = new URL(url, globalThis.location?.href).protocol;
  if (protocol !== "https:" && protocol !== "http:") throw new Error(`qrCode: only http(s) links, got "${protocol}"`);
  const link = h("a", { href: url, class: "qr__link" }, url);
  const figure = h("figure", { class: "qr" });
  const img = h("img", {
    class: "qr__img",
    src: qrSrc(url),
    alt: t("qr.alt"),
    width: size,
    height: size,
    onError: () => {
      img.replaceWith(h("p", { class: "qr__missing", role: "status" }, t("qr.missing")));
    },
  });
  figure.append(img, h("figcaption", { class: "small" }, link));
  return figure;
}
