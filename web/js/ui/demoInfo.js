// The (i) "About this demo data": the demo history is far larger than a one-clinic pilot would produce. Said once
// here, behind a button, so the screen itself carries only the DEMO DATA strip. The same words are on /method.

import { h } from "../core/dom.js";
import { t } from "../core/i18n.js";
import { infoButton } from "./popover.js";

/** The button for a page in demo mode, or null in pilot mode (`mode` is the /api/mode answer). */
export function demoDataInfo(mode) {
  if (mode?.mode !== "demo") return null;
  return infoButton({ topic: t("demoData.topic"), title: t("demoData.title"), content: h("p", null, t("demoData.body")) });
}
