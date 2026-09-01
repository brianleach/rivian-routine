# CLAUDE.md

Guidance for Claude (or any agent) working in this repo.

## What this is

A scheduled check (runs a few times a day) that watches Gmail for the Rivian R2
**VIN-assignment** email, sends a phone notification via [ntfy](https://ntfy.sh)
when it arrives, and then **disarms itself** so it stops consuming tokens. See
`README.md` for the full description and `RUN.md` for the scheduled-session
prompt.

This is **phase 2** of the repo: phase 1 caught the R2 order invite (it worked —
the order was placed 2026-08-18). Rivian's post-order emails say "When your R2
is assigned a VIN, you'll be able to complete the purchasing experience and
schedule your delivery" — catching that moment is now the job. Same
architecture, new classification target.

## Architecture — two halves, kept separate on purpose

- **`RUN.md`** is the prompt the scheduled **Claude session** runs. It does only
  the judgement-heavy work: a two-pass Gmail search via the Gmail **MCP** server
  (`from:rivian.com newer_than:2d` + `("R2" OR "VIN") newer_than:2d`, full bodies
  via `FULL_CONTENT`, de-duped by message ID) and **LLM classification** of each
  candidate as `VIN_ASSIGNED` vs `STATUS_UPDATE` vs `MARKETING/NOISE`. It writes
  `results.json`.
- **`r2_monitor.py`** is plain Python (stdlib only, no deps) that owns everything
  deterministic and irreversible: the `DONE` sentinel gate, de-dup state, ntfy
  notifications, the hard backstop, `--reset`, and `--dry-run`.

Keep this split. The LLM should not make the VIN-vs-marketing judgement's
counterpart — touching ntfy/state/sentinel — and the script should not make the
classification judgement.

## Two notification channels: ntfy + Slack

- **ntfy** is owned by `r2_monitor.py` (plain HTTPS POST). Primary channel; its
  send success is what gates de-dup state and self-termination.
- **Slack** is the second channel. The script can't reach the Slack MCP, so on
  each real run it writes the run's NEW alerts to `state/last_run.json`
  (`high`, `maybe`, `news`, `notice`, `slack_user_id`), and the scheduled session
  (`RUN.md`, Step 4) mirrors them via the Slack MCP. Slack rides along; it does
  not gate disarm. `last_run.json` only ever lists NEW alerts, so Slack inherits
  the same de-dup. Slack is recorded even when ntfy fails, so a blocked ntfy
  (egress 403) still reaches you on Slack. `--dry-run` writes nothing.
- Config (`.env`, git-ignored): `NTFY_TOPIC`, `SLACK_USER_ID` (empty = ntfy only).

## Invariants — do not break these

1. **Sender is untrusted signal.** Never add a hard `from:` filter that could
   drop a VIN/delivery email arriving from a transactional/third-party domain
   (logistics, Rivian Financial Services / Chase, registration). Classify by
   content and intent only. (Observed so far: transactional mail from
   `t.rivian.com`, marketing from `em.rivian.com` — do not rely on that split.)
2. **The sentinel is checked first.** `guard` (and `process` when not in
   dry-run) must exit near-instantly if `state/DONE` exists. This is the
   token-saver; keep it cheap and at the very top.
3. **A failed notification must NOT disarm.** The `DONE` sentinel and state
   writes happen only after a notification *successfully* sends. A send failure
   (e.g. ntfy egress 403) must leave the monitor armed to retry next run.
4. **`--dry-run` is side-effect free.** No ntfy sends, no `DONE`, no state
   writes. It must stay safe to run against the real inbox.
5. **Only a high-confidence hit self-terminates.** A "maybe" never writes `DONE`.
6. **Backstop is the hard cap.** Past `R2_BACKSTOP_DATE` with no hit → one final
   low-priority notice + `DONE`. Keep this independent of the candidate path so it
   fires even on zero candidates. It matters more in this phase: Rivian may put
   the VIN in the account (Garage) without ever emailing, so the backstop notice
   tells the owner to go check rivian.com/account manually.

## Tiers

- HIGH: `VIN_ASSIGNED` and confidence ≥ `R2_HIGH_CONF` (0.7) → HIGH ntfy + disarm.
  A VIN-assignment / complete-your-purchase / schedule-your-delivery email for
  MY specific vehicle.
- MAYBE: `VIN_ASSIGNED` and `R2_MAYBE_LOW` (0.4) ≤ confidence < 0.7 → LOW ntfy,
  keep watching.
- NEWS: `STATUS_UPDATE` → DEFAULT-priority FYI ntfy, **never disarms**. For
  substantive changes in the vehicle's production/delivery status or timeline —
  a new production stage ("your R2 is in production" / "has been built"),
  shipped/in transit, a concrete delivery-window estimate or a delay.
  Confidence-independent: the classifier's `STATUS_UPDATE` label is the gate.
  Distinct from "we're still getting your vehicle ready" re-explainers, which
  stay MARKETING/NOISE → silent.
- NONE: everything else → silent. Notably the phase-2 traps: Rivian's own
  process emails that *mention* the VIN ("When your R2 is assigned a VIN,
  you'll be able to…"), order/configuration confirmation receipts, and
  delivery-prep promos (home charger, road trips).

A MAYBE can be **upgraded** to a HIGH on a later run if reclassified ≥ 0.7. NEWS
is its own de-dup axis (`state["news"]`); it neither disarms nor blocks a later
HIGH/MAYBE for a different message.

## Conventions

- **No third-party dependencies.** Standard library only (`urllib`, `zoneinfo`,
  `json`, `argparse`). Keep it that way so it runs anywhere.
- **Config via env vars or git-ignored `.env`** (auto-loaded). Real env vars win
  over `.env`. Never hard-code the ntfy topic — this repo is meant to be open
  sourced.
- **Never commit** `.env`, `state/`, or `results.json` (all git-ignored).

## Testing

- Regression suite (stdlib only, no deps): `python3 tests/test_fixtures.py`.
  Drives `process --dry-run` over every fixture in `fixtures/` and asserts the
  exit code + HIT/MAYBE/NEWS routing, then checks `--dry-run` created no `state/`.
  Fixtures are modelled on the real post-order inbox: only a genuine
  VIN-assignment email may fire + disarm; "Your R2 is in production" fires a
  NEWS heads-up (no disarm); the VIN-mention teasers, confirmation receipts,
  and charger/road-trip promos stay silent. `status_update_results.json`
  isolates the NEWS tier.
- Offline plumbing (single fixture): `python3 r2_monitor.py process --input fixtures/sample_results.json --dry-run`
- End-to-end: paste `RUN.md` into a session with `DRY-RUN` at the top (searches
  the last 7 days, classifies, runs `process --dry-run`).
- Exit codes from `process`: `0` normal, `10` already disarmed, `20` hit + disarmed.

## Deployment notes

- Runs as a scheduled task in Claude Code on the web: **3×/day at 07:00 / 13:00
  / 19:00 America/Chicago**, prompt = `RUN.md`. Requires the Gmail MCP server
  in-session. De-dup + the `DONE` sentinel make the extra runs safe and cheap.
- **Re-arming for a new phase:** if a deployment still carries `state/` from the
  previous phase (the invite hit wrote `DONE`), run `python3 r2_monitor.py reset`
  once. Fresh clones start armed (state/ is git-ignored).
- **ntfy egress:** the sandbox uses a network egress allowlist; `ntfy.sh` (or
  your `NTFY_SERVER`) must be on it or sends fail with `HTTP 403`.
