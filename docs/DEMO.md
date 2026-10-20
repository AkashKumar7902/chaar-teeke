# Demo walk-through

Everything on screen is synthetic and labelled DEMO DATA in the page frame. The browser phone is labelled SIMULATOR. No real phone line is involved. See [what is not verified](../README.md#honest-status).

## Set up (once, before visitors arrive)

There are two ways to run the demo. Use the first for the full demo and the second for a run without the speech model.

**The full demo: the speech model on.** A place a visitor says is transcribed and matched, so it becomes a ward on stage (shown with the word "estimate" until a reviewer decides it).

```bash
uv sync --extra asr
uv run chaar-teeke seed-demo     # prints the staff logins once
export CT_ASR=indicconformer CT_ASR_MODEL_DIR=/path/to/the/model/folder
scripts/smoke.sh --with-asr      # = chaar-teeke preflight --with-asr: loads the model once and transcribes a bundled clip
uv run chaar-teeke serve         # http://127.0.0.1:8080/
```

The model loads in the background when the server starts. Measured once inside the app on the 8 GB Apple M1 laptop (19 October 2026): about 30 seconds to load and a peak of about 2.2 GB of memory ([EVALS.md](EVALS.md#speech-model-load-time-and-memory); an earlier run of the evaluation script gave about 7 seconds and 2.4 GB, so plan for the slower figure). Wait until the "Speech model" line on the administrator's System tab says "The speech model is loaded" before the first visitor. A recording that arrives while the model is still loading goes to the review queue with the reason "The speech model was still loading when this recording came in." Do the `--with-asr` preflight once, not between visitors, and close other programs first. The model folder is not in the repository; the model must be downloaded first ([README](../README.md#run-it-locally)). Without the folder or the libraries, the preflight fails on the speech line and tells you so.

**The rehearsal without the model: `CT_ASR=off`.** An explicit, labelled mode, not a fallback.

```bash
uv sync
uv run chaar-teeke seed-demo
CT_ASR=off uv run chaar-teeke serve   # http://127.0.0.1:8080/
CT_ASR=off scripts/smoke.sh           # the preflight, below
```

A spoken place then goes to the review queue and the feed says "Place waiting for a reviewer" until a reviewer decides it (see "If the microphone is blocked"). Every timing below was measured in this mode.

**Phones on the same wifi.** The server listens on this computer only by default. To let visitors scan the code with their own phones, start it with `CT_HOST=0.0.0.0` and set `CT_PUBLIC_URL=http://<this computer's address>:8080`. That opens the demo to everyone on that network, so it is the owner's call. Without `CT_PUBLIC_URL`, the code points at the computer itself, and the page says it works only on this computer.

## Preflight

`scripts/smoke.sh` checks the database, the prompt audio for each language, the telephony transport, the scheduler lock, and one complete simulated call on a throwaway database. It never touches the demo data and dials nothing. Each check prints one line, and the script exits non-zero if any fails. Without `--with-asr` it does not load the speech model; it checks that the model folder and libraries are there, or that `CT_ASR=off` was chosen. `scripts/smoke.sh --with-asr` also loads the model once and transcribes a bundled clip. That is slow and uses a lot of memory on an 8 GB laptop (figures above), so do it once.

Also check by eye: the DEMO DATA strip is at the top, the landing page shows a share and a map, and the phone page opens.

## Reset

**Between visitors, press "Reset demo data"** on the administrator's System tab (sign in as `admin`). It wipes the demo, writes the same seeded history again, puts the demo clock back to real time and makes new staff logins. Visitors stay signed in. The audit log is kept. To undo only a clock jump and keep everything else, a signed-in staff member presses "Back to now" in the frame strip.

`uv run chaar-teeke reset-demo` does the same from the command line, but **only with the server stopped**. A running server holds its jumped clock in memory and would not see the reset, so with a server running the command exits with code 2 and says: "A server is running on this database: use the admin Reset demo data button, or stop the server first." (`seed-demo` refuses the same way.) `serve` also refuses to start while another server holds the same database, because the second server would take the missed calls and never place the call back.

### Who moves the clock

The clock is shared by everyone in the room, so it has limits (thresholds in `data/policy.json`).

- Every jump is audited under the person who pressed it (`visitor:<n>` or `staff:<user>`). The administrator reads them in the Audit tab.
- A visitor may jump 10 times in a session, and all visitors together 60 times an hour. A refused jump says so in plain words.
- The administrator's "Lock the clock" switch (System tab) turns visitor jumps off, and the "Move clock" menu's jump choices disappear for them. Signed-in staff keep theirs, so the presenter signs in as `clinic` or `admin` on the laptop, locks the clock, and runs the script. The lock survives a reset.

## The script

Each step names what to say and what appears. Timings are only those measured in the scripted rehearsal of 19 October 2026 (a Playwright script, `rehearsal.mjs`, kept outside this repository; the final re-run, with the short notice). Scope of every figure: the 8 GB Apple M1 laptop, one headless Chrome, the browser phone (SIMULATOR, no real line), `CT_ASR=off`, a script that presses each key about one second after the question is asked. A person talking, pointing and answering questions takes longer, and a speech-model run has not been timed (see "What was not timed").

**1. Landing page.** Open `/`. Say: "About 76,824 dog bites were recorded in Jammu district in two years. The city says about 80% of its street dogs carry the right-ear notch. This page shows, area by area, where bitten people saw that notch. It helps the sterilisation team show its coverage and choose where to work next." The page shows the notched share among street-dog bites with its interval and the number of answers, a map of 8 areas, and a feed of the latest calls. The band "78-80% claimed: expected only if sterilised and unsterilised dogs bite equally" sits on the bar: point at it and say that bite reports are not a count of dogs, so the page reads the share beside the city's figure and does not replace it. Point at the strip: "All demo data." If a visitor asks how big the demo is, open the (i) "About this demo data": the history is synthetic and sized like several clinics (the share rests on 767 answers, each area on 85 to 107), while a one-clinic pilot gives about 175 answers in 12 weeks, enough for a city-wide share and not for area flags.

**2. Scan the code.** The landing page opens as the sterilisation team's view, read-only. In the "Try the call" card, scan the QR code with your phone. Or press "Open the phone here" on the laptop. The phone page says SIMULATOR.

**3. Give the missed call.** On the phone page press "Give a missed call". Say: "This is what a patient does at the clinic. On a real line a missed call is meant to cost nothing. Here it is the simulator." The landing feed shows "Missed call". In demo mode the call is linked at once to a "visitor desk" patient, so no clinic step is needed.

**4. The phone rings.** The phone shows "Incoming call". Press "Answer". Measured: 12.3 seconds from the missed call to the ring.

**5. Answer the call.** The voice speaks. A machine voice draft is labelled on the screen. The greeting and the short notice cannot be skipped: measured from the language key to the consent question, about 35 seconds (read off the captions' times). Every question can be cut short by pressing the key. On the English page each line in Hindi, Dogri or Urdu has its English words under it, with the clinic, dates and numbers filled in. Keys are on the on-screen keypad:
- Language, if asked: press 1 for Hindi.
- The short notice names every item, then press 1 to agree to reminders. Pressing 0 instead plays the full notice and the question is asked again: show it only if a visitor asks about privacy. Measured earlier on 19 October with the script pressing 0 once, and not repeated in the final re-run: 162 seconds for the call instead of about 92.
- Press 1 to answer the four questions.
- Where did the dog bite? Hold the button and say a place. A good one is "Gujjar Nagar". Let go to send.
- Kind of place: 1. Animal: 1 for a street dog.
- Did it have the cut: press 2 (no cut). Say: "No notch seen. The team uses answers like this, area by area."
- Did it look sick: press 1 (yes).
- The voice reads the next dose date and ends.

Measured: the whole first call, from the ring being answered to "The call ended", took 91.5 seconds with every key pressed about a second after its question.

**6. See it arrive.** On the laptop, the landing feed shows a bite report and an alert. The report says "No notch seen". The feed marks it as simulated, and the map marks any area that includes simulated reports with a dashed outline. With the speech model on, the place is transcribed and matched, and the feed shows the ward with the word "estimate". With `CT_ASR=off` it shows "Place waiting for a reviewer" (see "If the microphone is blocked"). Do the reviewer step before the next step if you want it: a simulated clip is deleted 8 demo hours after the call, and the clock jump moves past that.

**7. Jump the clock.** Keep the phone page open. In the frame strip press "Move clock", then "Evening before the next dose". Say: "Now it is 6:30 the evening before the second injection." The phone rings with the reminder: clinic, dose number and date. Answer and listen. (The other jump choices are "Next call" and "Morning after the next dose".) Measured: the reminder rang 5.5 seconds after the jump, and the reminder call took 37.9 seconds.

**8. The clinic and the team.** On the landing page under "See it as", choose the clinic. The clinic page lists today's due and missed patients. It opens on GMC Jammu; the visitor's own patient belongs to "Demo visitor desk" in the Clinic list, and is not due until Thursday. On the Today page "Print bite-card stickers" opens the sheet of 8 stickers that the clinic sticks on bite cards. Go back and choose the sterilisation team, then Alerts. The alert shows ward, day and kind of place. Say: "No phone number. The vet never sees one."

**9. Close.** "The number is deleted 7 days after the course. The city keeps counts by area, never a point. The pilot ask is a 12-week trial at the GMC anti-rabies clinic." Details: [PILOT.md](PILOT.md).

If time is short, skip the clinic and team step. The reminder call and the alert are the two moments that land.

### Timings, as measured

All from one run of `rehearsal.mjs` on 19 October 2026 (the final re-run). Scope as above; the full per-step log is the script's output.

| What | Seconds | Which steps |
|---|--:|---|
| The first call | 91.5 | Answer to "The call ended" |
| The reminder call | 37.9 | Answer to "The call ended" |
| Landing page to the end of the reminder call (the scripted path) | 167.1 (2 min 47 s) | Rehearsal steps 0 to 10: the landing page loads, the phone opens, missed call, ring, first call, the report on the landing page, the team alerts and coverage pages (as a visitor), the clock jump, the reminder ring and call |
| The whole rehearsal script | 283.1 (4 min 43 s) | The scripted path plus a second missed call and call (91.2 s), the vet's alerts page and the clinic view: two calls and the page visits |

An earlier run on 19 October gave a first call of 91.5 seconds, about 2 min 47 s for the scripted path, and about 4 min 40 s for a visitor's own full walk-through, including two calls and the page visits.

### What was not timed

- Anything with the speech model on: a full rehearsal in the full-demo configuration has not been run. Only the model's own figures exist: about 30 seconds to load, and in the 19 October run in the app 8.8 seconds for the first clip and 0.1 to 0.6 seconds for each later one ([EVALS.md](EVALS.md)). The demo's own example clip "mera ghar Gujjar Nagar mein hai" was placed in the right ward (6) in that run. A visitor's live voice on a laptop microphone has not been tried.
- A person who listens to every question instead of pressing early.
- Other machines than the 8 GB M1 laptop.

## If the microphone is blocked

Browsers allow the microphone only on https pages and on the computer's own address (`localhost` or `127.0.0.1`). On a visitor's phone using the network address, the page says: "This page needs a secure address (https) to use the microphone. Skip this question."

- **Easiest:** run the phone on the laptop, on the page opened with "Open the phone here". The laptop's own address is allowed.
- **Skip the question.** The call goes on. The place is saved as not said, and the alert still appears without a ward.
- **Use https for phones.** That needs a tunnel to a trusted https address, then `CT_PUBLIC_URL` set to it. A tunnel exposes the demo to the internet, so whether to run one, and which, is the owner's call. We have not set one up.

Without the speech model (`CT_ASR=off`, the rehearsal configuration), a spoken place goes to the review queue and reads "No transcript: speech model off". The landing feed shows "Place waiting for a reviewer" until a reviewer decides. That is the honest outcome, and the full demo does not use it. To show the full path anyway, choose "reviewer" under "See it as", open the review queue, listen to the clip and pick the ward. Nothing is guessed in the meantime.

## Demo logins

Visitors need no login. Use "See it as" on the landing page for the clinic, the sterilisation team, oversight and the reviewer. Visitors can read and cannot write, except to open a phone and move the demo clock (within the limits above, and not at all while the clock is locked).

Staff logins (clinic, abc, vet, oversight, reviewer, admin) are printed once by `seed-demo` and written to `var/demo_logins.txt`, readable only by you. Every seed makes new ones. You need them to claim an alert (the `vet` login), to use the administrator's tabs and to press Reset. Keep the file out of screenshots.

## What the demo data is

Twelve weeks of synthetic history from a seeded script (the same every time): a confirmed clinic named for GMC Jammu, a second one labelled "Demo clinic (unverified)", about 25 enrolments a day (made up for the screens; the pilot plans for about 12 a day, see [PILOT.md](PILOT.md#what-the-volume-allows)), a few alerts in each state, a review queue of example place clips (machine voices), worked wards and sight counts. Every row is flagged as demo. Demo phone numbers use the reserved `+91 99999 0xxxx` pattern and are never dialled. Demo courses are never called.

This is far more than a one-clinic pilot would produce: the share on the landing page rests on 767 answers, and each of the 8 areas on 85 to 107, with one area flagged "Look here". A one-clinic 12-week pilot gives about 175 answers in all, which is enough for a city-wide share and not for area flags ([PILOT.md](PILOT.md#what-the-volume-allows)). The pages say so once, in the (i) "About this demo data" on the landing and team coverage pages and in the Method page under "About this demo data".
