# Rivian R2 invite monitor

A scheduled "morning check" that watches Gmail for the **real** Rivian R2 order
invite, pushes a phone notification when it arrives, and then **disarms itself**
so it stops consuming tokens once the job is done.

It is built for two hard parts of this specific problem:

1. **False positives.** Rivian sends frequent marketing blasts with invite-ish
   subject lines ("Important update on R2 orders", "R2 arrives June 9"). A keyword
   match cries wolf. So candidates are run through an **LLM classifier** that
   judges *intent* — a personalized, actionable call to place/configure your order
   vs. a newsletter.
2. **Unknown sender.** Marketing comes from `hello@em.rivian.com`, but the
   transactional invite may come from a different domain/ESP. So the sender is
   **never used as a hard filter** — it's explicitly untrusted as a signal.

## How it works

The monitor is split into a thinking half and a plumbing half:

| Part | Who runs it | What it does |
|------|-------------|--------------|
| `RUN.md` | the scheduled **Claude session** | pulls candidates from Gmail (MCP), classifies each as `ACTIONABLE_INVITE` vs `MARKETING/NOISE`, writes `results.json` |
| `r2_monitor.py` | plain Python (no deps) | de-dup, ntfy notifications, the DONE sentinel, the backstop, `--reset`, `--dry-run` |

Keeping the irreversible/stateful work (notifying, disarming) in plain, tested
code — and letting the LLM do only the judgement call — is deliberate.

### The pipeline each morning

1. **Sentinel gate.** `r2_monitor.py guard` runs first. If a `DONE` sentinel
   exists, the session stops immediately (near-zero work / near-zero tokens).
2. **Two-pass Gmail search** (full message bodies, `FULL_CONTENT`):
   - Primary: `from:rivian.com newer_than:2d` — Gmail's domain match catches any
     Rivian subdomain, so we don't depend on one address.
   - Secondary: `"R2" newer_than:2d` — catches an invite from a non-obvious /
     third-party vendor domain. Pulls in noise (e.g. TLDR/Wired); the classifier
     handles it. Candidates are de-duped across both passes by message ID.
3. **Classification.** Each candidate → strict JSON with `classification`,
   `confidence`, `reason`, `sender`, `subject`, `received` (+ `message_id`).
4. **Decide & notify** (`r2_monitor.py process`):
   - **High-confidence hit** (`ACTIONABLE_INVITE` and confidence ≥ 0.7) →
     **HIGH** priority ntfy with subject/sender/time/reason and a direct Gmail
     link, then the monitor **disarms** (writes `DONE`).
   - **Maybe** (`ACTIONABLE_INVITE` and 0.4–0.7) → **LOW** priority ntfy flagged
     "POSSIBLE R2 invite — check manually." Keeps you in the loop without false
     alarms. Does **not** disarm — monitoring continues.
   - **No hit** → silent.
5. **De-duplication.** Notified message IDs are persisted in `state/state.json`,
   tracking high-confidence and maybe notifications **separately** so a maybe can
   be *upgraded* to a real hit if a later run reclassifies it ≥ 0.7.
6. **Self-termination.** On a confirmed, successfully-sent high-confidence hit,
   `DONE` is written and the success notification confirms it disarmed:
   *"✅ R2 invite detected — morning check disabled. Run --reset to re-arm."*
7. **Hard backstop.** If the date passes **2026-07-15** with no hit, one final
   LOW "window elapsed — disabling check" notice is sent and `DONE` is written —
   so a silently-missed invite can never leave the job running for months.

## Setup

### 1. Set your ntfy topic

Notifications use [ntfy](https://ntfy.sh) — install the ntfy app, subscribe to a
topic of your choosing, then point the monitor at it. Pick a long, unguessable
topic name (ntfy topics are public to anyone who knows the name).

The topic is **not** stored in the repo (so this is safe to open source). Provide
it either as an environment variable or via a git-ignored `.env` file, which the
script loads automatically:

```bash
cp .env.example .env
echo 'NTFY_TOPIC=your-private-topic-name' > .env   # e.g. r2-watch-7f3a9c
```

Other optional overrides (env vars or `.env`): `NTFY_SERVER` (default
`https://ntfy.sh`), `R2_BACKSTOP_DATE` (default `2026-07-15`), `R2_TIMEZONE`
(default `America/Chicago`), `R2_HIGH_CONF` (0.7), `R2_MAYBE_LOW` (0.4).

### 1b. Allow outbound access to ntfy.sh ⚠️

Claude Code on the web runs in a sandbox with a **network egress allowlist**. If
`ntfy.sh` (or your `NTFY_SERVER`) is not on it, notifications fail with
`HTTP 403: Host not in allowlist` and the monitor will not be able to alert you.
Add your ntfy host to the environment's network egress settings before going
live — see the [Claude Code on the web docs](https://code.claude.com/docs/en/claude-code-on-the-web).
A send failure is non-destructive: the monitor will **not** disarm and will retry
the next morning, so an allowlist mistake delays alerts but never drops the hit.

### 2. Schedule it (every morning at 7:00 AM America/Chicago)

This runs as a **scheduled task in Claude Code on the web** (it needs the Gmail
MCP server, which lives in the Claude session):

1. Open the repo in Claude Code on the web and create a **scheduled task /
   trigger**.
2. Schedule: **daily at 07:00, timezone America/Chicago**.
3. Prompt: paste the contents of [`RUN.md`](./RUN.md) (the block between the
   `---` lines).
4. Make sure `NTFY_TOPIC` is set in the environment (env var / setup script).

> Self-hosting instead? A cron entry that drives a headless Claude run works too —
> the only hard requirement is that the Gmail MCP server is available to the
> session. Example (conceptual):
> ```cron
> # min hour dom mon dow   (set CRON_TZ or use a system tz of America/Chicago)
> CRON_TZ=America/Chicago
> 0 7 * * *  cd /path/to/rivian-routine && claude -p "$(cat RUN.md)"
> ```

## Testing — run the dry-run BEFORE the first live execution

`--dry-run` runs the **full pipeline against the last 7 days** but **suppresses
real notifications** and **never writes the sentinel or state** — so you can
confirm it tags the June 9 "Important update" blast and other existing marketing
as **NOT** an invite.

**End-to-end (real Gmail), in a Claude session:** paste `RUN.md` into a normal
session and put the word **DRY-RUN** at the top. It will search the last 7 days,
classify everything, and run `process --dry-run`, printing each email's tier and
the notifications it *would* have sent — without touching your inbox state, ntfy,
or the sentinel.

**Plumbing only (offline, no Gmail/LLM needed):** a fixture set demonstrates the
decision logic deterministically:

```bash
python3 r2_monitor.py process --input fixtures/sample_results.json --dry-run
```

Expected: the June 9 marketing blast and the TLDR newsletter are **silent**, the
ambiguous "reservation: next steps" is a **MAYBE** (low), and a personalized
"it's your turn to configure your R2 order" is a **HIGH** hit that *would* disarm
the monitor. Nothing is written to disk.

## Re-arming and resetting

```bash
python3 r2_monitor.py --reset     # clear the DONE sentinel; the morning check runs again
```

Use this if a "maybe" turned out to be wrong, after a real hit if you want to
keep watching, or to reuse the monitor next time.

## Files

```
r2_monitor.py                 # deterministic CLI: guard / process / reset / dry-run
RUN.md                        # the scheduled-session prompt (Gmail search + classify)
CLAUDE.md                     # guidance for Claude working in this repo
fixtures/sample_results.json  # offline test fixtures
.env.example                  # copy to .env (git-ignored) and set NTFY_TOPIC
state/                        # runtime state + DONE sentinel (git-ignored)
```

## Privacy

This repo is safe to open source: it contains no inbox contents, no email
address, and no real ntfy topic — the topic is supplied at runtime via
`NTFY_TOPIC` or a git-ignored `.env`, and runtime state (`state/`, `results.json`,
`.env`) is git-ignored. Keep it that way: never commit `.env` or
`state/state.json` (it holds notified message IDs), and never hard-code your ntfy
topic.
