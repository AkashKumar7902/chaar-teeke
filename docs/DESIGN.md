# Chaar Teeke: design

The patient never sees a screen. Every web screen serves someone who acts on what a call collected, so the
design is quiet, plain and fast to read. Tokens are in `web/css/tokens.css`, components in
`web/css/components.css` and `web/js/ui/`. The living reference is `web/dev/gallery.html` (every component,
every state, both themes, both languages).

## Identity

Civic, calm, Jammu-specific without kitsch. No illustration of dogs, no saffron-and-green flag colours, no
mascot.

- **The mark** is a right ear with a V cut in its tip: the notch that marks a sterilised dog. One shape,
  `web/img/mark.svg`, drawn in `currentColor` through a CSS mask, so it follows the theme.
- **Colour:** cool paper and ink, with one action colour, Tawi blue (`--primary`). Amber is attention. Red is
  for real system faults. No cream and terracotta, no purple gradients, no emoji.
- **Type:** Atkinson Hyperlegible Next for Latin and digits, Noto Sans Devanagari for Hindi and Dogri, Noto
  Nastaliq Urdu for Urdu. Self-hosted, SIL OFL (`web/fonts/FONTS.md`). Devanagari is a first-class script:
  every screen is checked in Hindi.
- **Shape:** 8 px corners, 2 px strong borders, flat surfaces, one soft shadow only on popovers and dialogs.

## Rules

1. One job per screen and one filled button.
2. Say each fact once. DEMO DATA lives in the frame strip and nowhere else. The one other thing the demo says about itself is a named (i) "About this demo data" on the landing and team coverage pages (and under `/method#demo-data`): the demo history is sized like several clinics, far more than a one-clinic pilot would give.
3. About 40 words of fixed text per screen. An explanation goes behind a named (i) button ("About the notched share").
4. Absence is never zero or safety: "No calls yet", never "0 bites".
5. Red only for a real system fault. A late patient or an unanswered call is amber.
6. Solid means real. Dashed means estimate or simulated, always with its word.
7. Colour never stands alone: every state has an icon or a word as well.
8. Untrusted text (transcripts, place names) enters the DOM with `textContent`, inside `<bdi dir="auto">`.

## Tokens

| Group | Tokens |
|---|---|
| Surface | `--paper` page, `--surface` cards, `--surface-2` sunken, `--line`, `--line-strong` (input borders, 4:1) |
| Text | `--ink`, `--ink-2` secondary, `--ink-3` tertiary (all at least 5.4:1 on paper, both themes) |
| Action | `--primary`, `--primary-hover`, `--primary-ink`, `--primary-tint`, `--primary-text` |
| Attention | `--attention` text, `--attention-tint`, `--attention-line`, `--attention-solid` |
| Alarm | `--alarm`, `--alarm-tint`, `--alarm-line` (system faults only) |
| Estimate | `--estimate-line`, `--estimate-tint` (dashed outline, always with the word) |
| Frame | `--demo-bg`, `--demo-ink` (heavy and neutral, never amber or red) |
| Map | `--map-bg`, `--map-nodata`, `--map-ward-line`, `--map-hatch`, `--ramp-1` to `--ramp-5` |
| Type | `--text-xs` 14 px to `--text-4xl` 56 px, line height 1.5 Latin, 1.6 Devanagari, 2 Nastaliq |
| Space | `--s-1` 4 px to `--s-8` 64 px, `--gutter` 16 px on a phone |
| Targets | `--target` 44 px, `--target-lg` 56 px (stepper, keypad) |
| Focus | 3 px `--focus` outline, 2 px offset, on every control |

Themes: light by default, dark through `prefers-color-scheme`, and either forced with
`<html data-theme="light|dark">`. A `forced-colors` block keeps borders and focus visible. Reduced motion
removes all movement; the skeleton hides itself for the first 300 ms with CSS alone.

The map ramp is one blue hue stepped in lightness (darker means a higher notched share; in dark mode lighter
means higher), so it survives colour blindness. "Too few answers" is hatched, "no calls yet" is plain,
"includes simulated" is a dashed outline, "look here" an amber outline. Each has a legend entry.

## Scripts

- Nothing under 14 px. No letter-spacing, italics or ALL CAPS in Devanagari or Nastaliq.
- `html[lang=ur]` is set to 115% with line height 2 so Nastaliq neither looks small nor clips.
- `<html lang dir>` is set per page by `initI18n`. Western digits in every language. Layout uses logical
  properties only. Maps, charts, the keypad and the interval bar never mirror (`dir="ltr"`).
- Staff pages: English and Hindi (toggle remembered in `localStorage`, inside try/catch). Patient pages (phone,
  privacy): Hindi, Dogri (in Devanagari), Urdu, English. Dogri and Urdu strings are machine-written and
  flagged in `_meta` until a native speaker checks them.

## Components

Frame (`mountFrame`): one header with the mark, role nav, language toggle and account menu; in demo mode one
strip with "DEMO DATA: synthetic, not real patients", the demo clock and one "Move clock" menu (on a phone the strip is one
line: DEMO DATA with an (i) that opens the rest, the date, Move clock). Staff pages need a login;
`public` and `patient` modes work without one (brand and language toggle only, the strip still shows). Buttons (`.btn`, one
`.btn--primary`), segmented control, chips, pills (`statusPill`), (i) button and popover, menu button,
card (only where a thing needs a boundary), stat, list, table (stacks on a phone), empty state, error state,
banner, skeleton, toast, dialog, tabs, stepper (big - and +), keypad, share display, alert card, form field
with error summary, ward map with legend, QR image, icons. The bite-card sticker (`/clinic/sticker`, linked from Today) is always black on
white: one sticker on screen, eight on an A4 sheet in print (2 across, 4 down, 105 x 74 mm), its lines in Hindi, Dogri and Urdu whatever
the page language, and nothing printed when the pilot has no missed-call number. Pages import them from `/js/ui/index.js`.

**Share display.** A percentage never appears without its interval, n and the could-not-see count. Between 25
and 49 answers it adds "Fewer than 50 answers. Read with care." Below 25 there is no percentage: a hatched box
says "n = 11 of 25 needed". With no answers it says "No calls yet". The reference band (78 to 80 per cent) is
drawn on the city-wide bar with the server's own label. The label is the server's `claim_label_key` looked up in the common bundle, so Hindi is not rebuilt from English.

**Alert card.** Titled by where it happened, not by the word "Alert" (the page heading says it, and a screen-reader label on each card
does). Who has it reads "You are on it" for the person who took it, "Taken by <name>" for anyone else. The ward is a
machine's match until a reviewer decides it, so `placeChip` (alert card and landing feed) puts it in the dashed
"estimate" pill ("Ward 48, Bahu (West) · estimate", Hindi "वार्ड 48 · अनुमान") and shows it bare only when
`location_status` is `decided`.

**Phone line banner.** The clinic, team and admin views read `/api/system/status` and listen to the `system` event.
While the telephony check is down an amber banner reads "Calls are not going out: the phone line is down." with the
server's reason, and goes when the check is ok again. **Draft translation note.** Patient pages in Dogri and Urdu carry a
dashed "this translation is a draft" note under the header until a native speaker has read the strings.

**Toasts** show one at a time (a new one replaces the one on screen) and fade after 3 seconds; errors stay until closed.

## Copy

English dates read "Sep", never "Sept".

Plain words first, the acronym in brackets after. Short, active, no jargon. Buttons are verbs ("Mark dose
given"). Errors say what failed and what to do, in amber, with one Retry. Numbers, dates and ids come from
code; IST 24-hour times ("10:40"), dates as "Mon 19 Oct", counts below 5 as "<5".

**Never on screen:** safe, clear, all fine, rabies detected, diagnosis, fully sterilised, a share without its
interval. Do not write "0" for something that was not seen. Tests read `web/i18n/*.json` for these words.

## Honesty labels (pinned by tests, never removed without the owner)

"DEMO DATA: synthetic, not real patients" in the frame; "SIMULATOR" on the phone page; "estimate" beside
every ward match ("Ward: estimate"); interval, n and could-not-see beside every share; the reference band
label; "machine voice draft" on prompt audio; "estimate" beside a ward in the alert card and the landing feed; "phone numbers leave the reminder system 7 days after the
course" on the privacy and phone pages.

## States (defined once, `ui/state.js`)

| State | Look |
|---|---|
| Loading | Skeleton after 300 ms |
| Empty | What is missing and when it was last checked ("Last checked 10:10") |
| Error | Amber, plain words, where it failed, one Retry, optional "Why?" |
| Offline | Banner "Can't reach the server. Retrying."; no stale data; typed input stays, marked "Not sent" |
| Forbidden | Neutral "Your role can't do this" (403 `read_only` also raises a toast, unless the call was made with `{quiet: true}` because a dialog shows the refusal) |
| System fault | The only red: dongle offline, alert not delivered |

Streams and maps recover: SSE reopens after 5 to 60 s with jitter and resyncs. The server sends `event: ping` every
15 s, and 40 s without any event counts as lost (close, reopen, offline banner), so a half-open connection is caught;
the stream also checks itself when the tab becomes visible and reopens when the network comes back. The map has one
resize path: MapLibre's own resize tracking is off (`trackResize: false`), so the container's ResizeObserver and the window's
"resize" event both go through the guard, which waits 120 ms, resizes only when the canvas no longer matches its
container (a resize clears the drawing buffer, and a full-page screenshot's resize event once left the map drawn in
the upper part of its box), stops any running move first and rebuilds a dead canvas; the rebuild is armed again after 30 s
of healthy drawing, and a second death inside that time says "The map stopped drawing" with Retry. A stream that
keeps failing while nobody is signed in sends the page to the sign-in page instead of retrying.

## Checking a page

`node scripts/ui_check.mjs` at 1440, 1280 and 390, en and hi, light and dark: no console errors, no horizontal
overflow, fonts loaded, nothing under 14 px, targets at least 44 px. For a component, open
`/dev/gallery.html?theme=dark&lang=hi`.

## Hindi glossary

One word for one thing in every `*.hi.json` (a test in `web/js/core/i18n.test.mjs` fails if a variant returns).
Dogri and Urdu follow the same terms where their bundles use them.

| English | Hindi | Never |
|---|---|---|
| street dog | गली के कुत्ते (the same as the call, FC_ANIMAL) | सड़क के कुत्ते |
| the notch (V-cut in the ear) | V-कट | निशान, V-निशान |
| tick a box | टिक करें | निशान लगाएँ |
| alert | अलर्ट | चेतावनी |
| analysis area; "look here" | क्षेत्र; यहाँ देखें | इलाके, यहाँ देखिए |
| machine voice draft | मशीनी आवाज़ का मसौदा | मशीन आवाज़ |
| simulated | सिम्युलेटेड (simulator: सिम्युलेटर) | नकली, सिमुलेटेड |
| vaccine; dose | टीका; खुराक | वैक्सीन |
| schedule | शेड्यूल | योजना, कार्यक्रम |
| call-back | कॉल-बैक | वापस कॉल, वापसी फ़ोन |
| withdrawing (the patient's number) | नाम वापस लेना | वापसी |
| outside the city | JMC के बाहर | |
| voice prompts | वॉइस संदेश | आवाज़ संदेश |
| dashed line | टूटी रेखा | बिंदीदार |
| the clock control | घड़ी आगे करें | आगे बढ़ें |
| wards the team covered | टीम के कवर किए वार्ड | काम किए गए वार्ड |
| dogs seen on rounds | गश्त में देखे कुत्ते | कुत्तों की गिनती |

Spellings, one each: ख़त्म, ख़राबी, तारीख़, बाक़ी, हिन्दी, पेज. Intradermal and intramuscular read "ID (त्वचा)" and
"IM (मांसपेशी)". The clinic, enrol and course Hindi is for nurses: a nurse reads all of it before any real patient uses it, and
`common.hi.json` `_meta.nurse_check` says NOT DONE until that is recorded.
