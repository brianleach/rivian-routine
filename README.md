# Rivian R2 invite monitor

A scheduled check — it runs a few times a day — that watches Gmail for the
**real** Rivian R2 order invite, pushes a phone notification when it arrives, and
then **disarms itself** so it stops consuming tokens once the job is done.

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

### The pipeline on each run

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
   - **Two channels.** Alerts go to **ntfy and Slack**. The script owns ntfy and
     records each run's new alerts to `state/last_run.json`; the scheduled
     session mirrors them to Slack via the Slack MCP (Slack needs no egress
     allowlist). Slack is optional — unset `SLACK_USER_ID` to use ntfy only.
5. **De-duplication.** Notified message IDs are persisted in `state/state.json`,
   tracking high-confidence and maybe notifications **separately** so a maybe can
   be *upgraded* to a real hit if a later run reclassifies it ≥ 0.7.
6. **Self-termination.** On a confirmed, successfully-sent high-confidence hit,
   `DONE` is written and the success notification confirms it disarmed:
   *"✅ R2 invite detected — scheduled check disabled. Run --reset to re-arm."*
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
`HTTP 403: Host not in allowlist` and the monitor cannot alert you. The allowlist
is set **per cloud environment in the web UI** (not in this repo, not in the
routine config), so add your ntfy host before going live.

Exact click-path (Claude Code on the web, as of this writing):

1. Go to [claude.ai/code](https://claude.ai/code).
2. At the bottom of the screen, next to the "Describe a task…" box, click the
   **☁️ environment chip** (e.g. **Default** — whichever environment your routine
   uses).
3. In the popover, under **Cloud**, click the **⚙️ gear icon** on your
   environment's row. The **"Update cloud environment"** dialog opens.
4. Find the **Network access** dropdown (it defaults to **Trusted**) and change it
   to **Custom**. An **Allowed domains** field appears.
5. In **Allowed domains**, add one host per line — domains, *not* URLs:
   ```
   ntfy.sh
   ```
   (or your own `NTFY_SERVER` host; `*` wildcards are supported).
6. Leave **"Also include default list of common package managers"** *checked* —
   the routine needs GitHub to clone this repo, plus pip/npm.
7. **Save.** The change applies only to **newly provisioned sessions**, so it
   takes effect on the routine's *next* scheduled run (an already-running session
   won't be retrofitted).

> ⚠️ Do **not** put `NTFY_TOPIC` / `SLACK_USER_ID` in the dialog's **Environment
> variables** box — it warns that those values are visible to anyone using the
> environment. The routine writes its own git-ignored `.env` at run time instead.

Verify it took: trigger a manual run of the routine (or run
`python3 r2_monitor.py process --input fixtures/sample_results.json` in a fresh
cloud session) and confirm a push lands on your phone instead of a 403. A quick
local sanity check of the topic+app wiring (separate from the sandbox allowlist)
is `curl -d "test" https://ntfy.sh/<your-topic>`.

You only need to add `ntfy.sh` — MCP connector traffic (Gmail, Slack) is routed
through Anthropic's servers and works without being in the allowlist. See the
[Claude Code on the web docs](https://code.claude.com/docs/en/claude-code-on-the-web#network-access).

A send failure is non-destructive: the monitor will **not** disarm and will retry
on the next run, so an allowlist mistake delays alerts but never drops the hit.

### 1c. Slack as a second channel (optional)

Alerts are delivered to **both ntfy and Slack**. Slack is sent by the scheduled
session through the Slack MCP, so it needs no egress allowlist change — handy as a
backstop while you sort out ntfy. To enable, set your Slack target in `.env`:

```bash
echo 'SLACK_USER_ID=U0XXXXXXX' >> .env   # your user id (DM) or a channel id (C...)
```

The Slack MCP connector must be enabled on the session/routine. Leave
`SLACK_USER_ID` empty to use ntfy only. (Find your user id in Slack: profile →
*More* → *Copy member ID*.)

### 2. Schedule it (3×/day: 07:00, 13:00, 19:00 America/Chicago)

This runs as a **cloud routine** in Claude Code (it needs the Gmail MCP server,
which lives in the Claude session). The easiest way to create it is the
**`/schedule`** skill — just describe the routine and it provisions the cloud
cron for you. In a Claude Code session, run `/schedule` and ask for:

- **Name:** `rivian-r2-monitor`
- **Cadence:** three times a day at **07:00, 13:00, 19:00 America/Chicago**
  (one routine, cron `0 0,12,18 * * *` in UTC during CDT — `/schedule` does the
  timezone conversion for you).
- **Prompt:** the contents of [`RUN.md`](./RUN.md) (the block between the `---`
  lines). **Important:** the cloud agent gets a fresh git clone where `.env` is
  absent (it's git-ignored), so prepend a step that writes `.env` with your
  `NTFY_TOPIC` and `SLACK_USER_ID` before the pipeline runs — or set them another
  way the script can read. (Don't use the environment's *Environment variables*
  box for these — it's visible to anyone using the environment.)
- **Connectors:** attach the **Gmail** and **Slack** MCP connectors.
- **Model:** any; a stronger model classifies marketing-vs-invite more reliably.

Prefer the web UI? You can instead create the routine manually at
[claude.ai/code](https://claude.ai/code) → **Routines** → **New routine**, with
the same cadence, prompt, and connectors. Either way, complete the egress
allowlist in **§1b** first, or the cloud sends will 403.

Why 3×/day: an order invite isn't minute-critical, so this caps worst-case
detection latency at ~6–8h without continuous polling. Extra runs are safe and
cheap — de-dup means you're never pinged twice for the same email, and once a
high-confidence hit fires, the `DONE` sentinel makes every later run exit at the
guard (near-zero tokens). Keep the query at `newer_than:2d` so a skipped run is
covered by the next one. Bump to 4×/day (add 22:00) or every 3h if you want
tighter latency; drop to 1×/day to minimize cost.

> Self-hosting instead? A cron entry that drives a headless Claude run works too —
> the only hard requirement is that the Gmail MCP server is available to the
> session. Example (conceptual):
> ```cron
> # set CRON_TZ or use a system tz of America/Chicago
> CRON_TZ=America/Chicago
> 0 7,13,19 * * *  cd /path/to/rivian-routine && claude -p "$(cat RUN.md)"
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
python3 r2_monitor.py --reset     # clear the DONE sentinel; the scheduled check runs again
```

Use this if a "maybe" turned out to be wrong, after a real hit if you want to
keep watching, or to reuse the monitor next time.

## Files

```
r2_monitor.py                 # deterministic CLI: guard / process / reset / dry-run
RUN.md                        # scheduled-session prompt (Gmail search + classify + Slack mirror)
CLAUDE.md                     # guidance for Claude working in this repo
fixtures/sample_results.json  # offline test fixtures
.env.example                  # copy to .env (git-ignored): NTFY_TOPIC, SLACK_USER_ID
state/                        # runtime state: state.json, DONE, last_run.json (git-ignored)
```

## Privacy

This repo is safe to open source: it contains no inbox contents, no email
address, and no real ntfy topic — the topic is supplied at runtime via
`NTFY_TOPIC` or a git-ignored `.env`, and runtime state (`state/`, `results.json`,
`.env`) is git-ignored. Keep it that way: never commit `.env` or
`state/state.json` (it holds notified message IDs), and never hard-code your ntfy
topic.

## Author

Built by Brian Leach in Austin, TX — [LinkedIn](https://www.linkedin.com/in/bleach/)

## License

[MIT](LICENSE) © Brian Leach
