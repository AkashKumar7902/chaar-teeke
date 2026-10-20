# Privacy

This is an engineering reading of public documents. It is not legal advice, and no lawyer has reviewed it. The legal sources are the [Digital Personal Data Protection Act 2023](https://www.meity.gov.in/static/uploads/2024/06/2bf1f0e9f04e6fb4f8fef35e82c42aa5.pdf) (the Act) and the [DPDP Rules 2025](https://www.meity.gov.in/static/uploads/2025/11/53450e6e5dc0bfa85ebd78686cadad39.pdf) (the Rules, notified 13 November 2025). Rules 3 and 5 to 16 apply from 13 May 2027. A pilot would probably start before that. We build to the Rules anyway.

The patient-facing notice is layered. A short notice is spoken at the start of the first call; the full notice is one key press away on the call (key 0), is written on the `/privacy` page (the sticker on the bite card carries a QR code to it) of the app, in Hindi, Dogri, Urdu and English. The layers are described under "Notice and consent" below.

## What we collect and what we do not

We collect: a phone number, the language, dose dates, what the patient presses and says on the calls, and (only if the patient agrees to the questions) the place of the bite, the kind of place, what the animal was and whether it looked sick.

We do not collect: name, date of birth, address, Aadhaar, or any health detail beyond dose dates. We hold one age fact: whether staff marked the patient as an adult or as under 18 with a guardian's consent. The clinic keeps its own register as it does today. We only hold the register number.

## Data map

"Staff" means the clinic's own staff, for their own clinic only.

| What | Where it is kept | Who can see it | How long |
|---|---|---|---|
| Phone number | `contacts` table, encrypted (Fernet), key from the environment. Last four digits stored in the clear | Staff see the last four digits. The full number needs a separate "reveal" action, which is logged and limited to 30 an hour for each login; a login that reaches the limit is refused with a plain message and the administrator's system page shows an alarm. Nobody else | Until 7 days after the last scheduled dose, 7 days after the clinic stops the course, or at once on withdrawal |
| Missed call from an unknown number | `missed_calls` table, encrypted | Staff see the last four digits | 48 hours if no staff member links it to a patient. On link, the number moves to `contacts`. The number kept to play a withdrawal notice goes as soon as that call ends |
| Consent record: reminders yes or no, questions yes or no, notice version, language, time | `courses` and `answers` | Staff, Health | With the course record (see "Not yet decided" below) |
| Register number, schedule, dose dates and whether each dose was given | `courses`, `doses` | The clinic's own staff, for their own clinic only. The administrator does not read a clinic's patients (only the clinic's settings) | With the course record |
| Call log: time, result, seconds listened | `calls` | Staff, on the patient's page | With the course record |
| Voice clip of the place (up to 8 seconds) | A file under `var/clips/` | The reviewer role only, through the review page | Deleted when a ward is set or decided, and in any case after 7 days |
| Transcript and candidate wards of the clip | `place_captures` | The reviewer role | Deleted with the clip |
| Ward, kind of place, animal, notch answer, signs answer | `bite_reports`, without the phone number but linked to the course, and so to the register number | The reviewer role, while placing the report and in the register export. The clinic's patient page does not show them. The sterilisation team and oversight see only counts and shares by area | With the course record |
| Sick-dog alert: ward, day, kind of place, answer | `alerts` | The sterilisation team, vets and the administrator. No number, no voice, no call time | No automatic deletion yet (see "Not yet decided") |
| Audit log: who did what, every number reveal, every refused reveal | `audit`. The database itself refuses to change or delete a row (triggers); the app only ever adds | Administrator, in the Audit tab, who sets the system up but does not see patients. It holds no phone number | At least one year (see below) |
| Deletion log: which kind of data was deleted, for which course, when and why | `purge_log` | Administrator, as counts on the system tab | Kept. It holds no number |
| Weekly counts export | Generated on request | The oversight role (counts and shares only) | Whatever the person who downloads it does with it |
| Register export (columns of the national animal-bite register) | Generated on request | The reviewer role only (Health's own staff). This one is row-level: one row per course, with clinic, register number, ward and doses given | Whatever the person who downloads it does with it |
| Language and theme choice | The browser's own storage | The person's own browser | Until cleared |
| Call records held by the telephony provider | Outside our control | The provider | Unknown. To be set in the contract. Not verified |

## Who is responsible (DPDP roles)

- **Data fiduciary: Health.** The anti-rabies clinic, under the Health and Medical Education department, decides why and how the data is used. The fiduciary answers for the data.
- **Data processors: the Chaar Teeke team and the telephony provider.** They act on Health's instructions under a written contract. Who signs it for Health is an open decision.
- **JSCL gets aggregates only.** If JSCL were given personal data, it would become a fiduciary itself. So the sterilisation team sees counts and shares by area and sick-dog alerts. It never sees a phone number, a voice clip, a call time or a register number.

## Notice and consent

- **Notice comes first, in two layers.** Rule 3 asks for a notice that is itemised, gives the purpose, and says how to withdraw and how to complain. A 127-word spoken notice (52 to 54 seconds with the machine voice) did that, but it was the main reason a first call took over two minutes, and a long notice is one a listener stops listening to. So the notice is layered:
  1. **The short notice** (`FC_NOTICE`, about 52 English words, 23 to 24 seconds spoken) is played before any key is asked for. It names every item in a line each: we call this number before each injection; the number is deleted 7 days after the last injection; we ask where the dog bit and record your voice for the place, and delete the recording once the place is found; your answers are kept apart from your number; press 9 at any time to stop; press 0 to hear the full notice.
  2. **The full notice** (`FC_NOTICE_FULL`, the former spoken notice word for word) adds the rest: that the number was kept from the missed call and only for this, that the recording is also deleted after 7 days, that the answers are tied to the person's entry in the clinic's register, that the clinic may call if the dog looked sick, and how to complain (tell the clinic first; if it is not solved, write to the Data Protection Board of India). The listener hears it by pressing 0 at the consent question, at most twice a call (`calls.full_notice_max_plays` in `data/policy.json`); the consent question is then asked again. A third 0 is a wrong key and uses up a try like any wrong key. A 0 pressed while the short notice is still playing is kept, so a listener who knows what they want does not have to wait.
  3. **The same full notice is on screen, and the sticker leads to it:** the `/privacy` page says everything `FC_NOTICE_FULL` says, in Hindi, Dogri, Urdu and English: what is kept (and that the number came from the missed call, only for this), why, that the answers are kept apart from the number but tied to the person's entry in the clinic's register, that the clinic may call if the dog looked sick, how long, how to stop, how to complain (the clinic first, then the Data Protection Board of India) and who is responsible. `tests/test_privacy_page.py` fails if the page loses one of these or drifts from the spoken words. The printed sticker (`/clinic/sticker`, 8 to an A4 sheet) is shorter: where to give the missed call, the 7-day deletion promise, press 9 to stop, in Hindi, Dogri and Urdu, and a QR code to `/privacy`. It does not carry the full notice itself.

  Dogri, Hindi and Urdu are all languages the Act allows for notices. All three are machine drafts that no native speaker has read (`machine_draft_needs_native_review`); the layered wording must be read by one before a real patient hears it.
- **Consent has two parts.** "Press 1 to agree" to reminders. Pressing 2 declines: the call ends, no reminder is planned and the number is deleted. If the person presses nothing, no consent is recorded, the call counts as unanswered and may be tried again within the retry limit. Then "press 1 to answer four short questions, 2 to skip". Skipping the questions never costs the patient their reminders.
- **Withdrawal is as easy as consent.** Press 9 on any call (the voice asks for 9 again to confirm), or give a missed call after the "you are already signed up" message that a repeat missed call first gets. Both stop the course, cancel scheduled calls and delete the phone number. Withdrawal also deletes the place clip and transcript.
- **Wrong number.** Every reminder call opens with "if this call is not for you, press 8". Confirming with 8 stops all calls to that number.
- **Proof.** We keep when the consent was given, the notice version, the language and the answers, so Health can show it gave notice and got consent. The notice version is the version of the prompt texts plus which layer the listener heard to the end: `<prompts version>:short` for the short notice, `<prompts version>:short+full` when the listener also pressed 0 and heard the full notice through. A notice whose playback failed does not count as heard (and that call ends). The answer to the consent question is the same either way; only the version differs, so Health can tell who was told what.
- **Open point: lawful basis.** Consent plus the "voluntarily provided" and "State service" grounds in section 7 of the Act may both apply. Counsel should confirm the wording.
- **Rights.** A patient can ask the clinic to see, correct or erase their data. Grievances must be answered within 90 days. The clinic needs a named contact. That is not yet set up.

## Children

The speech and phone steps cannot tell a patient's age. So:

- Staff must mark every patient as an adult or as under 18 with a guardian's consent when they enrol the patient. The enrolment form has no third choice. (The database also has an "unconfirmed" value, which only the demo data uses; for it the four questions never run.)
- The four data questions run only for adult or guardian consented. The guardian's consent is taken in person and ticked by staff. A key press alone does not count.
- For a child, the phone enrolled should be the parent's or guardian's.
- Reminders still run for children. Part A of the Rules' Fourth Schedule exempts a clinical establishment from the parent's verifiable consent where processing is restricted to health services for the child "to the extent necessary for the protection of her health". That probably covers reminders. It does not clearly cover bite-place and dog questions, which is why they wait for the guardian's consent. Counsel should confirm.

## Retention and the one-year log caveat

The promise we make to patients is narrow: **the phone number is removed from the reminder system 7 days after the last scheduled dose, or on withdrawal.** We do not promise that every trace is gone.

Rules 6(1)(e) and 8(3) of the Rules, read plainly, require a fiduciary to keep personal data, traffic data and processing logs for at least one year. Telephony providers also keep call records under their licences (not verified). So:

- Our audit log is kept at least a year. It holds actor, action and object ids. It never holds a full phone number.
- Provider and security logs may be kept longer as the law requires.
- Open decision for Health and counsel: accept the wording "removed from the reminder system 7 days after the course; provider and security logs are kept as the law requires", or hold numbers sealed for a year.

**Not yet decided.** The software deletes phone numbers, clips, transcripts and unlinked missed calls on time. It does not yet delete course rows, dose rows, call logs, bite reports, alerts or consent records. Health must say how long they stay. The pilot should not start without an answer.

## Suppression rules

What leaves Health is counts by area. The rules, all set in `data/policy.json`:

- A count under 5 shows as "<5", zero included.
- If exactly one cell that adds up to a shown total is hidden and is not zero, the smallest shown cell also reads "hidden", so it cannot be found by subtraction.
- A percentage shows only when the answers behind it number 25 or more (notch seen plus no notch seen, street dogs only). From 25 to 49 it is marked as rough. Below 25 the page says how many more answers are needed.
- Every share is shown with its interval, the number of answers and the number of patients who could not see the ear.
- There is no map of points. Wards show counts only, never shares. Shares are for the city and for 8 analysis clusters of neighbouring wards (not JMC zones).
- Alerts carry ward, day, kind of place and answer, and nothing else.

### Known limits

These rules are conventions, not a proven guarantee. No Indian rule sets a threshold. The numbers follow common practice in national health surveys and in NHS England's published-data rules, which we have not checked against their latest text.

- **A sum of hidden cells can be inferred.** If two or more cells are hidden, they cannot be told apart, but their sum can be worked out from the shown total and the other cells.
- **Shares narrow hidden counts.** A percentage with an interval and a visible total lets a careful reader bound a hidden count even when it cannot be recovered exactly.
- **The affected-patient count can be differenced.** The ABC team's "worked wards" screen shows how many patients with a dose ahead would hear the "your area was worked" line. Recording two sets of wards that differ by one ward gives that ward's figure by subtraction. The count shows "<5" under 5 and names no patient, but the difference can still reveal a small number. This screen is for team records readers only.
- **Counts over time can be differenced.** Two weekly totals that overlap give the difference.
- **Small places and local knowledge.** A count of 6 in a small cluster can still point to a person a neighbour already knows was bitten.
- **Row-level exports exist.** The register export is for Health's reviewer role and is not suppressed. Access is by role.

If any of these matter for the pilot, raise the threshold in the policy file (it needs a new version and a new test pin) or widen the clusters.

## If there is a breach

Our reading of the Rules (rule 7). Health, as fiduciary, is responsible for the notices. The team, as processor, must tell Health at once.

1. **Contain.** Rotate `CT_SECRET_KEY` (signs everyone out) and, if the phone table may have been read, `CT_PHONE_KEY`. Disable the accounts involved. Take the host off the network if needed.
2. **Find out.** Use the audit log: which accounts, which numbers were revealed, what time. Write down what happened.
3. **Tell Health.** The team tells the Health contact named in the pilot agreement the same day.
4. **Tell patients.** Health tells each affected patient without delay, using the registered number, with plain words on what happened, what to do and who to contact.
5. **Tell the Data Protection Board.** Health tells the Board without delay and sends full detail within 72 hours.
6. **Fix and record.** Close the hole, add a test that would have caught it, and keep the written account.

## Related

[ARCHITECTURE.md](ARCHITECTURE.md) (security controls) · [PILOT.md](PILOT.md) · [DATA-LICENCES.md](DATA-LICENCES.md)
