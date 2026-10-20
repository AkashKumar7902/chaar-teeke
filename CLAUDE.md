# Chaar Teeke: rules for Claude Code sessions in this repo

Chaar Teeke is a missed-call IVR for dog-bite patients in Jammu: reminders before each rabies vaccine
dose, and on the first call four optional questions (where the bite happened, place type, whether the
dog had the right-ear V-notch that marks a sterilised dog, signs of rabies). Jammu Smart City's
sterilisation team sees, by area, the notched share among street-dog bites with its interval, and gets
same-day alerts. Built by Akash Kumar and Asish Kumar.

Read `MODULES.md` (the contract and ownership) before editing code, and `docs/DESIGN.md` before UI work.

## Hard rules

1. **Run only offline code without asking**: `uv run pytest`, `uv run ruff check .`,
   `node --check`, `scripts/gate.sh`, and the app in `CT_MODE=demo CT_TELEPHONY=sim`. Ask before
   anything that calls outside services, places a real phone call (`CT_TELEPHONY=ari`), installs
   packages, or exposes the app to the internet (a tunnel).
2. **No fallbacks.** When a step fails (a call, the speech model, a missing prompt audio, an unmatched
   place) it says so where the person acts and nothing stands in for it. `CT_TELEPHONY=sim` and
   `CT_ASR=off` are explicit, labelled modes, not fallbacks. Ask the owner before adding anything that
   substitutes data, a default, a cached copy or a second path.
3. **Clinical safety.** The IVR speaks only a greeting, the clinic, dose dates, "come to the clinic" and
   "call the clinic". No medical advice, no "restart" or "extra dose". The clinic chooses the regimen.
   Dog answers never change the schedule.
4. **Privacy.** Phone numbers live only in `contacts` (encrypted) and `missed_calls` (encrypted, 48 h),
   are never in logs, events, exports or UI beyond the last four digits, and leave the reminder system
   7 days after the last dose or on withdrawal. JSCL sees aggregates only; counts below 5 show "<5";
   shares need n >= 25.
5. **The server enforces every rule the UI shows.** Visitors can read, never write.
6. **One clock** (`chaarteeke.clock.now()`), **one scheduler writer**, **idempotent job keys**.
7. **Thresholds live in `data/policy.json`.** A change needs a new version and a new pin in
   `tests/test_policy.py`; a pin is never edited or removed.
8. **Git.** Commit as `AkashKumar7902 <91385321+AkashKumar7902@users.noreply.github.com>`. No
   `Co-Authored-By` or session trailers. Subagents and workflows never commit or push; only the main
   session does, after looking at the result. Push only when the owner asks. The commit message is the
   hand-over: what changed, why (quote the owner), what was checked, what is left.
9. **No secrets or personal contact details** in code, docs or screens. `.env.example` lists names only.

## How the UI talks (docs/DESIGN.md)

- Keep what matters, organise it (glance, one tap, details), delete what is not relevant.
- Say each fact once. DEMO DATA lives in the frame only.
- About 40 words of fixed text per screen; explanations behind a named (i) button.
- Absence is never zero or safety ("No calls yet", never "0 bites"). Never on screen: safe, clear,
  all fine, rabies detected, diagnosis, fully sterilised, a share without its interval.
- Red only for real system faults. Attention is amber. Dashed means estimate or simulated.
- Check UI work with `node scripts/ui_check.mjs` at 1440, 1280 and 390, en and hi, light and dark.

## How to run things

```bash
uv sync
uv run chaar-teeke seed-demo           # synthetic DEMO data (seeded, labelled)
uv run chaar-teeke serve               # http://127.0.0.1:8080/
uv run chaar-teeke preflight           # checks before a demo
scripts/gate.sh                        # lint, format, node --check, tests, quick UI check
```
