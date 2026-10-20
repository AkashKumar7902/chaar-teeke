# MODULES.md: the contract

Every module codes against this file. Ownership says who may edit what. Contract files
(`chaarteeke/{config,clock,db,ids,crypto,audit,events,policy,models,context}.py`,
`chaarteeke/migrations/*`, `data/policy.json`, `data/regimens.json`, this file) change only when the
task is to change the contract, and then this file changes in the same commit.

## Ownership

| Lane | Owns | Depends on |
|---|---|---|
| architect | contract files above, `pyproject.toml`, `CLAUDE.md`, `MODULES.md` | |
| core | `chaarteeke/{regimens,doses,enrol,scheduler,privacy}.py`, `tests/core/` | contract |
| ivr | `chaarteeke/ivr/*`, `chaarteeke/telephony/{sim,mux}.py`, `chaarteeke/api/routes_phone.py`, `tests/ivr/` | contract, core (enrol API) |
| speech | `chaarteeke/speech/*`, `chaarteeke/places/*`, `scripts/eval_matcher.py`, `tests/places/` | contract |
| stats | `chaarteeke/stats/*`, `chaarteeke/alerts.py`, `chaarteeke/feed.py`, `chaarteeke/demo/*`, `scripts/build_clusters.py`, `data/clusters.json`, `tests/stats/` | contract, core |
| api | `chaarteeke/api/{app,auth,sse,deps}.py`, `chaarteeke/api/routes_{auth,clinic,team,oversight,review,admin,demo,feed,system,events}.py`, `chaarteeke/main.py`, `tests/api/` | all services |
| voice | `data/prompts/**`, `scripts/make_prompt_audio.py` | VOICE.md |
| web-core | `web/css/{tokens,base,components}.css`, `web/js/core/*`, `web/js/ui/*`, `web/fonts/*`, `web/i18n/*`, `web/img/*`, `docs/DESIGN.md` | models |
| web-pages | `web/*.html`, `web/css/pages/*`, `web/js/pages/*` (split per page between agents) | web-core, api |
| ops | `scripts/{gate.sh,smoke.sh,ui_check.mjs}`, `.github/`, `tests/test_repo_rules.py`, `docs/*` except DESIGN | everything |

## Conventions

- Python 3.12, type hints, pydantic v2, small functions, no dead code, no TODO stubs that pretend to
  work. Docstrings only for a non-obvious why.
- Every datetime is aware UTC from `chaarteeke.clock.now()`; serialise with `clock.iso()`; local dates
  with `clock.local_today()` / `clock.at_local()`. Never `datetime.now()` (except `telephony/fake_ari.py`,
  which imitates the PBX's own clock).
- DB access through `Database.transaction()` for writes (pass the connection down) and
  `Database.query/one` for reads. Every state change writes `audit.record(...)` in the same transaction
  and publishes one bus event after commit.
- Ids: `ids.new_id("crs")` courses, `"mc"` missed calls, `"call"`, `"rep"` bite reports, `"alr"` alerts,
  `"cb"` callbacks, `"stf"` staff, `"cln"` clinics.
- Actor strings: `staff:<username>`, `visitor:<n>`, `patient:<course_id>`, `system:scheduler`, `system:demo`.
- No fallbacks. Raise a typed error; the API maps it to `{"error", "message"}` with plain words.
- Thresholds only from `ctx.policy`. Times of day, retries, minimums: never literals in code.

## Service signatures (every function takes `ctx: AppContext` first unless noted)

### core: `chaarteeke/regimens.py`
- `load_regimens(path=DATA_DIR/"regimens.json") -> dict[str, Regimen]` where
  `Regimen(id, name, route, days: list[int], enabled: bool)`.
- `schedule(regimen: Regimen, day0: date) -> list[DoseSpec]` (`DoseSpec(dose_no, offset_days, due_date)`).

### core: `chaarteeke/doses.py`
- `refresh_states(ctx, course_id: str | None = None, today: date | None = None) -> int` recomputes
  `late_minor` / `late_review` per DOMAIN R5 and policy `doses`; returns rows changed.
- `mark_given(ctx, course_id, dose_no, by: Literal["clinic","patient"], on: date, actor) -> DoseOut`.
  Refuses (ValueError subclass `DoseError`) a dose in the future or a course that is not active.
- `next_due(ctx, course_id) -> DoseOut | None`.

### core: `chaarteeke/enrol.py`
Errors: `EnrolError(code, message)` with codes `unknown_clinic`, `unknown_regimen`, `regimen_disabled`,
`bad_date` (day0 must be exactly YYYY-MM-DD), `not_awaiting`, `already_linked`, `course_closed`, `not_found`,
`missed_call_closed`, `phone_purged`, `wrong_clinic`. `DoseError(code, message)` (a ValueError) with codes
`not_found`, `course_not_active`, `dose_in_future`, `date_in_future`, `bad_date`, `dose_not_needed`.
Extras: `dial_number(ctx, call_id) -> str` (the number to dial; refuses `phone_purged`),
`normalise_number(raw) -> str | None`. Dose 1 is recorded as given by the clinic at enrolment.
`MissedCallResult.status` is one of linked, unlinked, duplicate, withdrawn, withheld. Withdrawal also deletes
the bite report's clip and transcript (an undecided place becomes cant_tell).
- `create_course(ctx, data: EnrolIn, actor, source="clinic") -> CourseOut` creates `courses` +
  `doses` rows, status `awaiting_call`.
- `record_missed_call(ctx, number: str | None, transport, at, clinic_hint=None) -> MissedCallResult`
  (`MissedCallResult(missed_call_id, status, course_id | None)`): withheld number -> `withheld`;
  enrolled active number -> `duplicate` (schedules a `duplicate_notice` call); a number whose course
  was withdrawn and whose withdrawal notice was played -> `withdrawn`; else `unlinked`. In demo mode,
  a `sim:` number is linked at once to a new demo-visitor course (clinic `cln_demo_visitor`, regimen
  `id_trc`, eligibility `adult`, source `simulator`).
- `link(ctx, missed_call_id, course_id, actor) -> CourseOut`: moves the encrypted number into
  `contacts` (with `purge_after` = last dose + policy days), course -> `active`, and schedules the first
  call `first_call_delay_s` later (job key `first:<course_id>`).
- `withdraw(ctx, course_id, by_actor, reason) -> None`: stops the course, cancels calls, deletes the
  contact row, writes `purge_log(what='phone', reason='withdrawn')`.
- `stop(ctx, course_id, reason, actor)`, `set_vaccine_out(ctx, clinic_id, out: bool, actor)`.
- `reveal_phone(ctx, course_id, actor) -> str` (audited; refused after purge).
- `clinic_today(ctx, clinic_id) -> ClinicToday`, `get_course(ctx, course_id) -> CourseOut`.

### core: `chaarteeke/scheduler.py`
- `class Scheduler(ctx)`: `async run_forever()`, `async tick() -> TickReport`. One writer: takes the
  `locks` row `scheduler` with a heartbeat; a second instance waits. `tick()` (1) refreshes dose states,
  (2) plans call rows idempotently by job key `kind:<course>:<dose>:<local_date>` (reminders 18:30 the
  day before each dose after dose 1; morning-after 09:30 the day after an unmarked due dose), (3)
  hands due `scheduled` calls inside the calling window to `ctx.runner.place(call_id)`, (4) schedules
  retries per policy for `no_answer`/`failed`, (5) runs privacy jobs. `next_call_at(ctx) -> datetime |
  None` for the demo clock; `next_call(ctx)` (kind, course, dose, at) and `in_window(ctx, at)`.
  Demo-seeded courses (source = demo) are never planned, dialled or retried. Real-line (ari) calls go one at a
  time (policy calls.max_parallel_ari); browser-phone calls run in parallel. Loop, lock and purge pacing come
  from policy `scheduler`.

### core: `chaarteeke/privacy.py`
- `run_purges(ctx) -> PurgeReport` (phones past `purge_after`, clips past `expires_at` or decided,
  unlinked missed calls past 48 h). Writes `purge_log`, updates `kv['last_purge_run']`.
- `retention_status(ctx) -> dict` (last run, pending counts, deleted counts by kind).

### ivr: `chaarteeke/ivr/prompts.py`
- `class PromptCatalogue(prompts_json, audio_dir)`: `ids() -> list[str]`, `text(id, lang) -> str`,
  `has_audio(lang, id) -> bool`, `missing(lang) -> list[str]`, `media_name(lang, id) -> str`
  (`chaar/<lang>/<id>` for ARI), `url(lang, id) -> str` (`/media/prompts/<lang>/<id>.wav`),
  `say_date(lang, day: date) -> list[str]` and `say_number(lang, n) -> list[str]` (media names under
  `audio/<lang>/slot/`), `render(lang, id, slots) -> list[str]`, `manifest()`, and
  `missing(lang, **required_extent(ctx))` for the full list including clinic, ward and time clips. A clinic's
  `visit_time` and `phone` (migration 0002) feed the {time} slot and the vaccine-out line; a missing one fails
  the call with a plain error.

### ivr: `chaarteeke/ivr/flow.py` (pure; no I/O)
Input events `Start`, `Key(digit)`, `NoInput`, `PlayDone`, `Recorded(name, ok, talking_s)`, `Hangup`.
Output actions `Play(prompt_ids)`, `Collect(valid_keys, timeout_s)`, `Record(max_s, silence_s)`,
`Save(question, value)`, `Withdraw`, `MarkTaken(dose_no)`, `End(reason)`.
Flows: `FirstCallFlow(course, clinic, regimen, next_dose, ask_questions: bool, policy)`,
`ReminderFlow(...)`, `MorningAfterFlow(...)`, `NoticeFlow(kind)`. Each exposes `step(event) -> list[Action]`.
`Play(prompts, listen=False, keep="")`: `keep` names keys besides 9 that are held when pressed over a prompt that asks
nothing, so the next question sees them (the short notice keeps `0`). The first call's notice is layered: `FC_NOTICE`
(short, names every item), then at the consent question key `0` plays `FC_NOTICE_FULL` and asks again, at most
`policy calls.full_notice_max_plays` times; after that `0` is a wrong key.

### ivr: `chaarteeke/ivr/runner.py`
- `class CallRunner(ctx)`: `async start()` consumes `ctx.telephony.events()`; `async place(call_id)`
  originates and runs the flow; persists `answers`, creates/updates `bite_reports` + `place_captures`
  (audio saved under `var/clips/<report_id>.wav`), hands the clip to `places.review.process_capture`,
  creates alerts via `alerts.from_report` when signs = yes, marks doses via `doses.mark_given`, and
  publishes `call` / `report` events. Missed calls go to `enrol.record_missed_call`.

### ivr: `chaarteeke/telephony/sim.py`, `mux.py`, `api/routes_phone.py`
- `SimPort` implements `TelephonyPort`; `SimPort.attach(session, websocket)`; messages per SPEC s8.
- `MuxPort(ari: TelephonyPort, sim: SimPort)` routes by number prefix `sim:`.
- `POST /api/phone/sessions`, `WS /api/phone/ws` (caps from policy `visitor`).

### speech: `chaarteeke/speech/asr.py`
- `class Transcriber(Protocol): model_id: str; def transcribe(wav: bytes, lang: str) -> Transcript`.
- `load_transcriber(settings) -> Transcriber | None` (None only when `CT_ASR=off`; a load failure raises).

### speech: `chaarteeke/places/*`
- `normalise(text) -> NormText` (latin, devanagari, phonetic key, tokens).
- `class Gazetteer(gazetteer_json, wards_geojson, db)`: `ward_name(no)`, `wards()`, `aliases()`.
- `match(gaz, text, policy) -> MatchResult(confidence, candidates: list[Candidate])`.
- `review.process_capture(ctx, report_id) -> None` (ASR -> match -> `matched` or `needs_review`).
- `review.next_item(ctx) -> ReviewItem | None`, `review.decide(ctx, report_id, DecideIn, actor)` (deletes
  the clip, optional alias), `review.audio(ctx, report_id) -> bytes`.

### stats: `chaarteeke/stats/*`, `alerts.py`, `demo/*`
- Counts in outward views may be an int, `"<5"` (suppressed) or `"hidden"` (a complement that would reveal a
  suppressed cell). Shares and bounds are fractions 0..1; `Share.needed` is answers still needed.
- `wilson(x, n, z) -> (low, high)`, `manski(x, n, u) -> (low, high)`, `share(x, y, u, policy) -> Share`.
- `class Clusters(clusters_json)`: `for_ward(ward_no) -> str`, `all()`.
- `coverage(ctx, window_end: date | None = None) -> Coverage`, `ward_counts(ctx) -> list[WardCounts]`,
  `cluster_detail(ctx, cluster_id) -> dict`, `oversight_summary(ctx) -> OversightSummary`,
  `weekly_csv(ctx) -> str`, `register_csv(ctx) -> str`.
- `alerts.update_location(ctx, conn, report_id)` (called by review when a ward is decided), `alerts.unseen(ctx)`,
  `alerts.callbacks_for_clinic(ctx, clinic_id)`. Claim and call-back are vet-only.
- `alerts.from_report(ctx, conn, report_id) -> str | None`, `alerts.list(ctx, status, viewer=None) -> list[AlertOut]`
  (`alerts.get(ctx, alert_id, viewer=None)` likewise; `viewer` is the actor string of the person asking and only
  decides `AlertOut.claimed_by_me`; `claim/act/not_act` answer for their own actor),
  `alerts.claim/act/not_act(ctx, alert_id, actor, note)`, `alerts.request_callback(ctx, alert_id, actor)`,
  `alerts.callback_done(ctx, cb_id, actor, note, outcome="done")` where outcome is `done`, `declined` or
  `closed_no_contact` (the clinic could not reach the patient), `alerts.mark_seen(ctx, actor)`. A call-back being
  requested and being closed each publish one `callback` event.
- `intervals.hide_cells(cells, policy)`: cells that add up to a total shown beside them (place types, weekly
  bites). Under the threshold reads "<5"; a single hidden non-zero cell also hides the smallest shown cell
  ("hidden"), so it cannot be subtracted back.
- `demo.seed.seed(ctx, seed=20261019) -> SeedReport` (idempotent: refuses on a non-empty DB unless
  `reset=True`), `demo.controls.state(ctx) -> DemoState`, `demo.controls.jump(ctx, to) -> DemoState`.

## Configuration added to the contract (19 Oct)

- `CT_NUMBER` (`Settings.ct_number`): the Chaar Teeke missed-call number clinics tell patients to ring. One
  number for the deployment; unset by default; always reported as null in demo mode.
- Demo mode keeps its generated session secret in `var/demo_secret.key` (mode 0600, like `demo_phone.key`), so
  sessions survive a restart. `CT_SECRET_KEY` still wins; pilot mode still refuses to start without one.
- `data/policy.json` version `2026.10.3`: `auth.visitor_sessions_per_min` is 30 (was 6; a room of visitors
  shares one wifi address). Pinned in `tests/test_policy.py`.
- Content security policy: `script-src 'self'; style-src 'self'`, no outside host anywhere
  (`tests/api/test_contract_extras.py` fails if one is added). MapLibre GL JS 4.7.1 is vendored at
  `web/vendor/maplibre-gl-4.7.1/` (`maplibre-gl.js`, `.css`, `LICENSE.txt`, `SHA256SUMS`, checked by a test) and
  served from `/vendor`; `web/js/core/map.js` loads it from there.
- A page route whose HTML shell is not built yet serves the 404 page (status 404), never a 500.

## HTTP and SSE

Shapes are the pydantic models in `chaarteeke/models.py`; routes are SPEC section 11. Every write
needs the session cookie and `X-CT-CSRF`. Visitors get 403 `{"error":"read_only","message":"Your role can't do this"}`.
SSE `GET /api/events`: `event: <type>\nid: <n>\ndata: {"id","type","at","data"}\n\n`, `retry: 5000`,
a `: ping` comment every 15 s. Event payloads carry ids and small facts only, never a phone number:
`call {call_id, course_id, kind, status, simulated}`, `missed_call {id, status, phone_last4}`,
`report {report_id, cluster_id, animal, location_status, simulated}`, `alert {alert_id, status,
cluster_id, ward_no}`, `dose {course_id, dose_no, status}`, `review {remaining}`, `clock {now,
local_now}`, `system {check, ok}`, `course {course_id, status}`.

### Additions of 19 Oct (all backwards compatible)

- `GET /api/mode` (public, never a 401): `{mode, basemap, public_url, signed_in, view}`. `public_url` is
  `CT_PUBLIC_URL` or null; `view` is the role whose pages this browser sees (a visitor's chosen view), null when
  signed out. Read it before `/api/me`.
- `GET /api/clinic/config?clinic_id=` (clinic, a visitor viewing as clinic, admin; same ownership rule as
  `/api/clinic/today`): `{clinic_id, clinic_name, default_regimen, default_lang, regimens: [{id, name, route, days,
  enabled}] (enabled only), langs: ["hi","doi","ur"] (only languages whose prompt audio is complete for this
  clinic: its own clip and visit time, the check the call runner makes), ct_number (CT_NUMBER, null in demo or
  when unset), sim_phone (the browser phone is on), clinics: [{id, name}] (demo clinics a visitor or the
  administrator may switch to; empty for clinic staff)}`.
- `GET /api/admin/regimens` (admin) -> `[{id, name, route, days, enabled}]`, every schedule, for the clinic form.
- `POST /api/callbacks/{id}/done {outcome: "done" | "declined" | "closed_no_contact", note}`.
- `Coverage.claim_label_key`: stable key in the common bundle (`share.claim`, takes `{lo}` and `{hi}`); the Hindi
  text lives in `common.hi.json`. `claim_label` stays the English sentence.
- `ClusterCoverage.look_here_reason_data`: `{kind: "strong", n, ...}` (null when not flagged), next to the English
  `look_here_reason`. Bundle key `lookHere.strong`. (The `runs` kind of the first release is gone, see 19 Oct below.)
- `AlertOut.claimed_by_me` (bool): the person asking took it. `GET /api/alerts` and the claim, acted and not-acted
  replies are answered for the asker.
- `GET /api/clusters/{id}` adds `weekly_bites: [{week_start, bites, partial}]` (one entry per week of the coverage
  window, counts only; "<5" / "hidden" by `hide_cells`; `partial` marks the newest week when it is not over) and
  `place_types.unknown` (place not said), so the six place types add up to `bites`.
- `GET /api/worked/affected?ward_nos=1,2` (team records readers) -> `{courses_with_doses_ahead}`: active courses
  placed in those wards with a dose due after today (its reminder is the evening before) that have not heard the
  worked-area line yet (it is said once): the patients who would hear it. Demo-seeded courses (never called) are
  left out. An int or "<5"; names no course. 400 for a missing, empty,
  malformed or over-long list; 404 `unknown_ward`.
- SSE `callback {callback_id, alert_id, status}` (status `open` when requested, else the closing outcome). Sent to
  the clinic view (its own clinic's call-backs only; a visitor viewing as clinic sees all demo ones), to abc and
  vet. It never names a course.
- SSE for abc and vet also carries **simulated** `call` and `missed_call` events only (the visitor's own phone),
  trimmed to `call {call_id, kind, status, simulated}` and `missed_call {id, status}`; a real call or missed call
  never reaches them. Oversight, reviewer and admin get none of these.
- Event types are now `call, missed_call, report, alert, dose, review, clock, system, course, callback`.

### Additions of 20 Oct (api lane; all backwards compatible unless said)

- `data/policy.json` version `2026.10.4`, new keys only, pinned in `tests/test_policy.py`:
  `visitor.clock_jumps_per_session` (10), `visitor.clock_jumps_per_hour` (60), `auth.max_failed_signins_per_address` (30),
  `auth.signin_delay_base_s` (30), `auth.signin_delay_max_s` (600), `clinic.phone_reveals_per_hour` (30),
  `clinic.undo_dose_minutes` (15), `clinic.undo_callback_minutes` (15), `telephony.reconnect_min_s` (5),
  `telephony.reconnect_max_s` (60), `limits.json_body_max_bytes` (65536), `limits.upload_body_max_bytes` (5 MB),
  `limits.event_streams_per_principal` (5).
- Migration `0003_audit_append_only.sql`: `BEFORE UPDATE` and `BEFORE DELETE` triggers on `audit` that `RAISE(ABORT)`.
  Nothing in the app updates or deletes an audit row, and the demo reset does not touch the table.
- **Role matrix.** `auth.READ["clinic"]` is `{clinic}` only: `GET /api/clinic/today`, `/api/courses/{id}` and
  `/api/clinic/callbacks` are the clinic's own people (a visitor viewing as clinic, in the demo). The new area
  `clinic_config` is `{clinic, admin}` and serves `GET /api/clinic/config`. The administrator reads no patient.
- `GET /api/admin/audit?action=&object_type=&object_id=&actor=&phone_first=&limit=&offset=` (admin) ->
  `{rows: [{seq, at, actor, action, object_type, object_id, detail}], total, limit, offset, actions: [{action, count}]}`,
  newest first; `limit` 1..200 (default 50); `action` ending in `.` is a prefix; `phone_first` puts `phone.reveal`,
  `phone.reveal_refused` and `phone.reveal_limited` first; `actions` starts with those three.
- `GET /api/admin/signin-locks` (signed-in administrator only; a visitor gets 403) -> `[{username | null, address,
  failures, wait_s}]`; `POST /api/admin/signin-locks/clear {username?, address?}` -> `{cleared}` (audited
  `signin.lock_clear`). Sign-in counts wrong passwords per username *and* address; after `auth.max_failed_signins`
  the pair waits `signin_delay_base_s`, doubling per further wrong password up to `signin_delay_max_s` (429, `Retry-After`).
  A separate count per address (`max_failed_signins_per_address`) refuses an address that guesses many names.
- **Demo clock.** `GET /api/demo/state` and the jump reply are `DemoState` plus `clock_locked: bool` and
  `jumps_left: int | null` (a visitor's jumps still allowed; null for staff); `jump_targets` is empty when the asker may
  not jump now. `POST /api/demo/jump` audits `demo.jump` under the real actor (`visitor:<n>` / `staff:<user>`) and
  refuses a visitor with 403 `clock_locked` or 429 `jump_limit`. `POST /api/demo/clock-lock {locked}` (admin) and
  `POST /api/demo/clock-now` (signed-in staff; a visitor gets 403 `read_only`) -> `DemoState`; audited `demo.clock_lock`,
  `demo.clock_now`. The lock is `kv['demo_clock_locked']` and survives a reset.
- **Undo.** `POST /api/courses/{id}/doses/{n}/unmark` -> `DoseOut` (the clinic's own, a dose the clinic marked, within
  `clinic.undo_dose_minutes`); `POST /api/missed-calls/{id}/unlink` -> `CourseOut` (until the first call is placed);
  `POST /api/callbacks/{id}/reopen` -> `CallbackRequestOut` (within `clinic.undo_callback_minutes`). Audited
  `dose.unmarked`, `missed_call.unlink`, `callback.reopen`. Refusals: 409 `nothing_to_undo`, `undo_window_over`,
  `already_placed`, `already_open`.
- **Phone reveal limit.** `POST /api/courses/{id}/reveal-phone` is refused with 429 `rate_limited` after
  `clinic.phone_reveals_per_hour` reveals by one login in an hour; the refusal is audited `phone.reveal_limited` (once a
  minute at most) and `GET /api/system/status` shows an alarm check `phone_reveals` to the administrator.
- **System status.** Only a signed-in administrator (not a visitor viewing as admin) gets the phone line's host and port,
  the speech model's exception text and the `phone_reveals` check; everyone else gets the failure class in
  `params.failure` and a plain `detail`. New check code `scheduler.foreign` (red) when the scheduler lock is fresh and
  held by another process. A telephony check that is down carries `params.failure`.
- **Telephony reconnect.** While the phone line is down the server retries by itself (`api/telephony_watch.py`), waiting
  `telephony.reconnect_min_s` and doubling up to `reconnect_max_s`; a recovery publishes `system {check: telephony, ok: true}`.
  Calls that failed only because the line was down (`calls.error` starting "The telephone link is down" or "The phone
  system could not place the call") go back to `scheduled` with their attempt unchanged (audit `call.replanned`); the
  scheduler's retry rows for them are deleted.
- **Limits.** A write whose body is over `limits.json_body_max_bytes` (the WAV upload: `upload_body_max_bytes`) gets 413
  `too_large`, by its Content-Length or by the bytes that arrive. `GET /api/events` is refused with 429
  `too_many_streams` (`Retry-After`) when the login already holds `limits.event_streams_per_principal` streams.
- **Transport.** `connect-src` is `'self'` plus the exact `wss://` (`ws://` for http) origin of `CT_PUBLIC_URL`; the
  QR allow-list is `CT_PUBLIC_URL`'s host and the configured `CT_HOST`/`CT_PORT` (every name of this computer for a
  loopback or all-addresses host), never the Host header; `Strict-Transport-Security` is sent on https. Pages, scripts,
  styles and data are gzip-compressed (not `/api/`, `/media/`, `/fonts/`); `/fonts` and `/vendor` are
  `public, max-age=31536000, immutable`, everything else static is `no-cache` (ETag, 304).
- **Start-up.** Pilot mode refuses to start with a `CT_SECRET_KEY` under 32 characters or without `CT_PUBLIC_URL`. `serve`
  in demo mode exits with 2 and a plain line when another process holds a fresh scheduler lock. `reset-demo` and
  `seed-demo` exit with 2 and "A server is running on this database: use the admin Reset demo data button, or stop the
  server first." in the same case (the lock is read in the server's own demo time).
- **CLI.** `chaar-teeke create-admin` asks for a username and a password twice (not shown, never printed or logged),
  makes the first administrator, audits `staff.create` as `system:cli`, and refuses once any administrator exists.

### Additions of 19 Oct, demo walk-through fixes (api and stats lanes)

- `data/policy.json` version `2026.10.6`: new `feed` section (`max_items` 6, `max_real_alerts` 3), no existing threshold
  changed. Pinned in `tests/test_policy.py`.
- `GET /api/feed` (any signed-in view, a visitor included) -> `[FeedItem]`, newest first, at most `feed.max_items`:
  `{kind: call | missed_call | report | alert, id, at, day, cluster_id, ward_no, ward_name, animal, location_status,
  simulated}`. It holds the visitor's own phone (simulated calls that have started, missed calls and bite reports, with the
  facts the live stream carries) and alert summaries: simulated ones, and real ones (at most `max_real_alerts`, `at`
  null, the day only) only for a view that may read alerts. Never a real call, missed call or bite report, a number, a
  course or a clip. The landing page reads it after a reload; live `call`, `missed_call` and simulated `report` events
  add rows, and an `alert` event makes it read the list again.
- `AlertOut.made_at`: the instant, for a simulated alert only (a real alert is still served with its day only, in
  `created_at` and `local_day`). `AlertOut.location_status`: the report's place state, so a ward shows as an estimate
  until a reviewer decides it. A page never shows a bare day as a time of day (`format.js` refuses it).
- `GET /api/phone/captions?lang=hi|doi|ur&with=en` adds `en: {texts, slots}`: the English of each clip, keyed like
  `texts`. `slots` is the English of each slot clip (`slot/weekday_3` -> "Thursday"). `texts` holds the whole English line
  on the key of the line's first file (`FC_CLOSE.0`, `SYS_BYE`), with `{run.place}` where a slot goes: `run` is the
  position of the group of slot clips in the call language's own line, `place` the position of the value inside it (a
  date is one value). English orders the words differently, so the page shows the line when its first file starts and
  fills the places from the slot clips of that play. `with` other than `en` is 400.
- `POST /api/phone/sessions` decides the phone's desk from the login: a clinic login's missed call waits in that
  clinic's Today (`clinic_id` on the session); a visitor's or any other login's links at once to the demo visitor desk. A
  clinic login with no clinic is 409 `no_clinic`. Caps from policy `visitor`: phones per visitor, per network address
  (`phone_sessions_per_address`, 429 `phone_sessions_address`), in all (`phone_sessions_total`, of which
  `phone_sessions_reserved` are kept free for staff), and the socket's idle and longest time.
- Look here has one rule: the notched share over the latest `lookhere_window_weeks` full weeks is below the rest of the
  city's by more than chance explains (Bonferroni-corrected interval, `lookhere_z`), at least `min_n_for_share` answers
  in the area and in the rest. The earlier "below the rest in two consecutive windows" rule flagged someone in about
  93% of checks with no real difference and is removed. `lookhere.evaluate(policy, city, clusters)` takes the latest
  window's `Counts` only.

### Additions of 19 Oct, the layered notice (ivr, voice and ops lanes)

- `data/policy.json` version `2026.10.7`: `calls.full_notice_max_plays` (2), no existing threshold changed. Pinned in
  `tests/test_policy.py`.
- Prompts `data/prompts/prompts.json` version `0.4 draft`: `FC_NOTICE` is now the short notice (about 52 English words,
  23 to 24 s spoken) and the new `FC_NOTICE_FULL` holds the former full text (47 prompts in all). `FC_PLACE` and
  `ENR_DUP` are trimmed to about 10 to 12 s. Every changed Hindi, Dogri and Urdu line is `machine_draft_needs_native_review`.
  `flow.REQUIRED_PROMPTS` gains `FC_NOTICE_FULL`; key map: `0` is "hear the full notice" at the consent question.
- `courses.notice_version` is `<prompts version>:short` or `<prompts version>:short+full`. The runner reads which from
  the prompts played to the end in the call (`_Live.played`), not from the flow; the `consent_reminders` answer is `yes`
  either way.
- `scripts/make_prompt_audio.py check` lists any prompt over 25 s as long and exits 1 unless it is `FC_NOTICE_FULL`
  (heard only on key 0).

### Additions of 20 Oct, speech decided from the audio (ivr lane; two contract files)

- `data/policy.json` version `2026.10.9`: `calls.speech_frame_rms_min` (0.02) and `calls.speech_min_voiced_s` (0.25), no
  existing threshold changed. Pinned in `tests/test_policy.py`.
- `chaarteeke/migrations/0004_place_voiced_seconds.sql`: `place_captures.voiced_s REAL`, the voiced seconds the app measured.
- `chaarteeke/ivr/voiced.py`: `voiced_seconds(wav_bytes, frame_rms_min) -> float` (20 ms frames, voiced when RMS on samples
  scaled to -1..1 reaches the minimum; seconds = voiced frames x 0.02). `UnreadableRecording` for bytes that are not a
  16-bit WAV.
- `flow.Recorded(name, ok, talking_s, voiced_s=None)`: the runner fetches each recording that finished ok, measures it
  (`CallRunner._measure`) and the flow keeps the place when `voiced_s >= calls.speech_min_voiced_s`. `talking_s` (the phone
  system's whole seconds) is information only. A recording that cannot be read ends the call with "The place recording could not
  be read", and nothing stands in for it. The same rule serves the sim and ARI transports. The runner keeps the fetched audio in
  `_Live.heard` and saves that copy, so a kept recording is fetched once.

### Additions of 19 Oct, final web notes (web lanes; no contract change)

- New page route `/clinic/sticker` (`PAGES` in `chaarteeke/api/app.py`, shell `web/sticker.html`): the bite-card sticker, 8 to an A4
  sheet in print. It reads `GET /api/clinic/config` only (clinic and admin views, a visitor viewing as clinic; the administrator
  names the clinic with `?clinic_id=`), so it adds no API. Demo mode shows the browser phone (a QR code to `/phone` and the word
  SIMULATOR); pilot mode shows `ct_number`, and when that is null it says so and prints nothing.
- `web/js/core/map.js`: MapLibre is built with `trackResize: false`; the container's ResizeObserver and the window's "resize" event
  both go through `createResizeGuard`, which now takes `changed()` and resizes only when `canvasMatches` says the canvas is no
  longer the container's size.
- Shared strings: `common.demoData.*` (the (i) "About this demo data" on the landing and team coverage pages, demo mode only) and the
  `/method#demo-data` section carry the same words. The `/privacy` bundles gain a `register` section and carry the Data Protection
  Board, the register link and the sick-dog call of `FC_NOTICE_FULL` in all four languages (`tests/test_privacy_page.py`).

## Web core contract (`web/js/core`, `web/js/ui`)

- `getMode()` (api.js): `/api/mode` once per page; `resetMe()` forgets it with `/api/me` (call it after sign-in,
  visitor entry and sign-out, which `api.post` already does). Sign-in and visitor entry ask `/api/mode`, not
  `/api/me`, to learn there is no CSRF token yet, so an anonymous page shows no 401.
- `api.<method>(path, ..., {quiet: true})`: the caller shows this refusal itself, so the frame does not also toast
  a read-only 403. The call still throws; the error carries `error.quiet`.
- `mountFrame({mode})`: `"staff"` (needs a login: reads `/api/mode`, then `/api/me` only when signed in; a browser
  with no login goes to `/signin?next=` and the returned promise never settles), `"public"` (no login needed:
  brand, the staff language toggle, the DEMO strip in demo mode) and `"patient"` (as public with the patient
  language toggle). The DEMO strip comes from `/api/mode`, so anonymous visitors see it; the demo clock is added
  when somebody is signed in. "DEMO DATA:" and the clock's date are set in `<bdi dir="ltr">` so they read
  correctly in Urdu. On staff pages a stream that fails twice or more while `/api/mode` says nobody is signed in
  goes to `/signin` instead of retrying (`leaveWhenSignedOut` in sse.js).
- `format.js`: `claimLabel(coverage)` (the band label from `claim_label_key`), `lookHereReason(cluster)` (from
  `look_here_reason_data`).
- `shareDisplay(share, {claim: {band, labelKey}})`: the band text is `labelKey` from the common bundle (`label`, a
  ready sentence, is still accepted). `alertCard(alert)` is titled by where it happened (no repeated "Alert"
  word), and shows "You are on it" when `alert.claimed_by_me`, else "Taken by <claimed_by>".
- `dom.js` `ltr(text)`: a `<bdi dir="ltr">` island for Latin text or numbers inside right-to-left text.
- `html[lang=ur] body` takes the Urdu line height (base.css).
