# CLAUDE.md

Guidance for Claude (or any agent) working in this repo.

## What this is

A scheduled "morning check" that watches Gmail for the **real** Rivian R2 order
invite, sends a phone notification via [ntfy](https://ntfy.sh) when it arrives,
and then **disarms itself** so it stops consuming tokens. See `README.md` for the
full description and `RUN.md` for the scheduled-session prompt.

## Architecture — two halves, kept separate on purpose

- **`RUN.md`** is the prompt the scheduled **Claude session** runs. It does only
  the judgement-heavy work: a two-pass Gmail search via the Gmail **MCP** server
  (`from:rivian.com newer_than:2d` + `"R2" newer_than:2d`, full bodies via
  `FULL_CONTENT`, de-duped by message ID) and **LLM classification** of each
  candidate as `ACTIONABLE_INVITE` vs `MARKETING/NOISE`. It writes `results.json`.
- **`r2_monitor.py`** is plain Python (stdlib only, no deps) that owns everything
  deterministic and irreversible: the `DONE` sentinel gate, de-dup state, ntfy
  notifications, the hard backstop, `--reset`, and `--dry-run`.

Keep this split. The LLM should not make the invite-vs-marketing judgement's
counterpart — touching ntfy/state/sentinel — and the script should not make the
classification judgement.

## Two notification channels: ntfy + Slack

- **ntfy** is owned by `r2_monitor.py` (plain HTTPS POST). Primary channel; its
  send success is what gates de-dup state and self-termination.
- **Slack** is the second channel. The script can't reach the Slack MCP, so on
  each real run it writes the run's NEW alerts to `state/last_run.json`
  (`high`, `maybe`, `notice`, `slack_user_id`), and the scheduled session
  (`RUN.md`, Step 4) mirrors them via the Slack MCP. Slack rides along; it does
  not gate disarm. `last_run.json` only ever lists NEW alerts, so Slack inherits
  the same de-dup. Slack is recorded even when ntfy fails, so a blocked ntfy
  (egress 403) still reaches you on Slack. `--dry-run` writes nothing.
- Config (`.env`, git-ignored): `NTFY_TOPIC`, `SLACK_USER_ID` (empty = ntfy only).

## Invariants — do not break these

1. **Sender is untrusted signal.** Never add a hard `from:` filter that could
   drop an invite arriving from a transactional/third-party domain. Classify by
   content and intent only.
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
   fires even on zero candidates.

## Tiers

- HIGH: `ACTIONABLE_INVITE` and confidence ≥ `R2_HIGH_CONF` (0.7) → HIGH ntfy + disarm.
- MAYBE: `ACTIONABLE_INVITE` and `R2_MAYBE_LOW` (0.4) ≤ confidence < 0.7 → LOW ntfy, keep watching.
- NONE: everything else → silent.

A MAYBE can be **upgraded** to a HIGH on a later run if reclassified ≥ 0.7.

## Conventions

- **No third-party dependencies.** Standard library only (`urllib`, `zoneinfo`,
  `json`, `argparse`). Keep it that way so it runs anywhere.
- **Config via env vars or git-ignored `.env`** (auto-loaded). Real env vars win
  over `.env`. Never hard-code the ntfy topic — this repo is meant to be open
  sourced.
- **Never commit** `.env`, `state/`, or `results.json` (all git-ignored).

## Testing

- Offline plumbing: `python3 r2_monitor.py process --input fixtures/sample_results.json --dry-run`
- End-to-end: paste `RUN.md` into a session with `DRY-RUN` at the top (searches
  the last 7 days, classifies, runs `process --dry-run`).
- Exit codes from `process`: `0` normal, `10` already disarmed, `20` hit + disarmed.

## Deployment notes

- Runs as a scheduled task in Claude Code on the web: **3×/day at 07:00 / 13:00
  / 19:00 America/Chicago**, prompt = `RUN.md`. Requires the Gmail MCP server
  in-session. De-dup + the `DONE` sentinel make the extra runs safe and cheap.
- **ntfy egress:** the sandbox uses a network egress allowlist; `ntfy.sh` (or
  your `NTFY_SERVER`) must be on it or sends fail with `HTTP 403`.
