#!/usr/bin/env python3
"""
r2_monitor.py — Rivian R2 order-invite morning monitor (deterministic plumbing).

This script owns everything that does NOT require judgement:
  * the DONE-sentinel gate (self-termination),
  * de-duplication state (so you are not pinged twice for the same email),
  * ntfy notifications (high-confidence hit / maybe / backstop),
  * the hard July-15 backstop,
  * --reset (re-arm) and --dry-run (test) modes.

The two parts that DO require judgement — pulling candidate emails from Gmail and
classifying them as ACTIONABLE_INVITE vs MARKETING/NOISE — are performed by the
scheduled Claude session using the Gmail MCP server, and handed to this script as
JSON (see RUN.md). That keeps the LLM doing only what an LLM is good at and keeps
the irreversible/stateful bits in plain, testable code.

Usage
-----
  python3 r2_monitor.py guard
      Exit 0 ("ARMED") if the monitor is live; exit 10 ("DONE") if it has already
      disarmed itself. The scheduled prompt calls this FIRST and stops on exit 10.

  python3 r2_monitor.py process --input results.json [--dry-run]
      Consume the classifier's JSON, de-dupe, notify, and (on a confirmed
      high-confidence hit) write the DONE sentinel. Also enforces the backstop.
      Reads from stdin if --input is omitted.

  python3 r2_monitor.py reset            (alias: --reset)
      Clear the DONE sentinel so the morning check runs again.

Exit codes from `process`:
  0  ran normally (no high-confidence hit, or dry-run)
  10 sentinel already present (no work done)
  20 high-confidence hit found + notified -> monitor disarmed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, date

# ----------------------------------------------------------------------------
# Configuration (override via environment variables or a git-ignored .env)
# ----------------------------------------------------------------------------

# Task dir = this script's directory. State lives in ./state/ (git-ignored).
TASK_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a git-ignored .env in the task dir.

    This keeps secrets (your ntfy topic) out of the committed repo while still
    making them available to the scheduled run. Real environment variables take
    precedence, so .env is only a fallback.
    """
    path = os.path.join(TASK_DIR, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)
    except OSError:
        pass


_load_dotenv()

# ntfy topic + server. The topic is intentionally a placeholder — set NTFY_TOPIC
# (env var or .env) before going live; never hard-code it in a public repo.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "REPLACE_WITH_MY_NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

# Classification thresholds (see task spec).
HIGH_CONF_THRESHOLD = float(os.environ.get("R2_HIGH_CONF", "0.7"))
MAYBE_LOW_THRESHOLD = float(os.environ.get("R2_MAYBE_LOW", "0.4"))

# Hard backstop: once this date has PASSED with no high-confidence hit, send one
# final low-priority "window elapsed" notice and disarm. Caps the worst case.
BACKSTOP_DATE = date.fromisoformat(os.environ.get("R2_BACKSTOP_DATE", "2026-07-15"))

# Timezone for the backstop date comparison.
TIMEZONE = os.environ.get("R2_TIMEZONE", "America/Chicago")

STATE_DIR = os.path.join(TASK_DIR, "state")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
DONE_FILE = os.path.join(STATE_DIR, "DONE")


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def today_local() -> date:
    """Today's date in the configured timezone (falls back to UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TIMEZONE)).date()
    except Exception:
        return datetime.utcnow().date()


def gmail_link(message_id: str) -> str:
    """A deep link that opens the message in Gmail web."""
    return f"https://mail.google.com/mail/u/0/#all/{message_id}"


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    # Track high-confidence and maybe notifications SEPARATELY so a "maybe" can
    # later be upgraded to a real hit if reclassified with higher confidence.
    data.setdefault("high_confidence", {})
    data.setdefault("maybe", {})
    return data


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def write_done(reason: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(DONE_FILE, "w", encoding="utf-8") as fh:
        json.dump(
            {"disarmed_at": datetime.now().astimezone().isoformat(), "reason": reason},
            fh,
            indent=2,
        )


def send_ntfy(title: str, body: str, priority: str, tags: str,
              click: str | None = None, dry_run: bool = False) -> bool:
    """POST a notification to ntfy. Returns True on success.

    In dry-run mode nothing is sent; the would-be notification is printed instead.
    """
    if dry_run:
        print("  [dry-run] WOULD send ntfy notification:")
        print(f"    topic    : {NTFY_TOPIC}")
        print(f"    priority : {priority}")
        print(f"    title    : {title}")
        if click:
            print(f"    click    : {click}")
        print(f"    body     :\n      " + body.replace("\n", "\n      "))
        return True

    if NTFY_TOPIC == "REPLACE_WITH_MY_NTFY_TOPIC":
        print("  [error] NTFY_TOPIC is still the placeholder. Set NTFY_TOPIC env "
              "var or edit r2_monitor.py before going live.", file=sys.stderr)
        return False

    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    # Title/Priority/Tags travel as headers and must be latin-1 safe, so keep emoji
    # out of the Title — the emoji icon comes from Tags, and the body is UTF-8.
    headers = {
        "Title": title.encode("ascii", "ignore").decode("ascii"),
        "Priority": priority,
        "Tags": tags,
    }
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        url, data=body.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as exc:
        print(f"  [error] ntfy send failed: {exc}", file=sys.stderr)
        return False


# ----------------------------------------------------------------------------
# Notification bodies
# ----------------------------------------------------------------------------

def _details_block(c: dict) -> str:
    return (
        f"Subject: {c.get('subject', '(none)')}\n"
        f"From:    {c.get('sender', '(unknown)')}\n"
        f"At:      {c.get('received', '(unknown)')}\n"
        f"Why:     {c.get('reason', '')}\n"
        f"Open:    {gmail_link(c['message_id'])}"
    )


def notify_high(c: dict, dry_run: bool) -> bool:
    body = (
        "Rivian appears to have sent your R2 order invite.\n\n"
        + _details_block(c)
        + "\n\n✅ R2 invite detected — morning check disabled. "
        "Run --reset to re-arm."
    )
    return send_ntfy(
        title="R2 ORDER INVITE detected",
        body=body,
        priority="high",
        tags="rotating_light,car",
        click=gmail_link(c["message_id"]),
        dry_run=dry_run,
    )


def notify_maybe(c: dict, dry_run: bool) -> bool:
    body = (
        "POSSIBLE R2 invite — check manually.\n\n"
        + _details_block(c)
        + f"\n\n(confidence {c.get('confidence', '?')}; not disarming — still watching.)"
    )
    return send_ntfy(
        title="POSSIBLE R2 invite - check manually",
        body=body,
        priority="low",
        tags="mag,car",
        click=gmail_link(c["message_id"]),
        dry_run=dry_run,
    )


def notify_backstop(dry_run: bool) -> bool:
    body = (
        "window elapsed — R2 invite never detected, disabling check.\n\n"
        f"The monitor watched through {BACKSTOP_DATE.isoformat()} and never saw a "
        "high-confidence R2 order invite. It is disarming itself to stop running "
        "indefinitely.\n\nRun --reset to re-arm if you still expect the invite."
    )
    return send_ntfy(
        title="R2 monitor: window elapsed - disabling",
        body=body,
        priority="low",
        tags="hourglass_done",
        dry_run=dry_run,
    )


# ----------------------------------------------------------------------------
# Tier decision
# ----------------------------------------------------------------------------

def tier_of(c: dict) -> str:
    """Map a classification record to HIGH / MAYBE / NONE.

    HIGH : ACTIONABLE_INVITE and confidence >= 0.7
    MAYBE: ACTIONABLE_INVITE and 0.4 <= confidence < 0.7
           (the classifier is instructed to place genuinely ambiguous, clearly
            Rivian-sent / order-related emails in this band so they surface as
            a MAYBE rather than being dropped)
    NONE : everything else (silent)
    """
    classification = str(c.get("classification", "")).strip().upper()
    try:
        conf = float(c.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    if classification == "ACTIONABLE_INVITE":
        if conf >= HIGH_CONF_THRESHOLD:
            return "HIGH"
        if conf >= MAYBE_LOW_THRESHOLD:
            return "MAYBE"
    return "NONE"


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

def cmd_guard() -> int:
    """Sentinel gate — the scheduled prompt calls this at the very top."""
    if os.path.exists(DONE_FILE):
        print("DONE — monitor already disarmed; nothing to do. Run --reset to re-arm.")
        return 10
    print("ARMED — monitor live.")
    return 0


def cmd_reset() -> int:
    if os.path.exists(DONE_FILE):
        os.remove(DONE_FILE)
        print("Re-armed: DONE sentinel cleared. The morning check will run again.")
    else:
        print("Already armed: no DONE sentinel present.")
    return 0


def _read_input(path: str | None) -> list:
    raw = sys.stdin.read() if not path else open(path, "r", encoding="utf-8").read()
    raw = raw.strip()
    if not raw:
        return []
    data = json.loads(raw)
    # Accept either a bare list or {"candidates": [...]}.
    if isinstance(data, dict):
        data = data.get("candidates", [])
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of classification objects.")
    return data


def cmd_process(input_path: str | None, dry_run: bool) -> int:
    # 1) Sentinel gate — near-zero work if already disarmed (skipped in dry-run so
    #    the pipeline can always be tested).
    if os.path.exists(DONE_FILE) and not dry_run:
        print("DONE — monitor already disarmed; skipping. Run --reset to re-arm.")
        return 10

    # 2) Hard backstop — fire once if the window has fully elapsed.
    if today_local() > BACKSTOP_DATE:
        print(f"Backstop: today is past {BACKSTOP_DATE.isoformat()} with no hit.")
        if notify_backstop(dry_run):
            if not dry_run:
                write_done("backstop: window elapsed")
                print("Backstop notice sent; monitor disarmed.")
            else:
                print("  [dry-run] would write DONE sentinel (backstop).")
        return 0

    candidates = _read_input(input_path)
    if not candidates:
        print("No candidates to classify. Nothing to do (silent).")
        return 0

    state = load_state()
    did_high = False

    for c in candidates:
        mid = c.get("message_id")
        if not mid:
            print(f"  [skip] candidate missing message_id: {c.get('subject')!r}")
            continue
        tier = tier_of(c)
        label = f"{tier:5s} conf={c.get('confidence')} | {c.get('subject')!r}"

        if tier == "HIGH":
            if mid in state["high_confidence"]:
                print(f"  [dup ] {label} — already notified, skipping.")
                continue
            print(f"  [HIT ] {label}")
            if notify_high(c, dry_run):
                did_high = True
                if not dry_run:
                    state["high_confidence"][mid] = {
                        "subject": c.get("subject"),
                        "sender": c.get("sender"),
                        "received": c.get("received"),
                        "confidence": c.get("confidence"),
                        "notified_at": datetime.now().astimezone().isoformat(),
                    }
                    # Upgrade: drop any prior "maybe" record for this message.
                    state["maybe"].pop(mid, None)
            else:
                print("  [error] high-confidence notification failed; will retry "
                      "next run (not disarming).")

        elif tier == "MAYBE":
            if mid in state["high_confidence"]:
                print(f"  [skip] {label} — already escalated to a hit.")
                continue
            if mid in state["maybe"]:
                print(f"  [dup ] {label} — already flagged as maybe, skipping.")
                continue
            print(f"  [MAYBE] {label}")
            if notify_maybe(c, dry_run):
                if not dry_run:
                    state["maybe"][mid] = {
                        "subject": c.get("subject"),
                        "sender": c.get("sender"),
                        "received": c.get("received"),
                        "confidence": c.get("confidence"),
                        "notified_at": datetime.now().astimezone().isoformat(),
                    }
        else:
            print(f"  [    ] {label} — not an invite (silent).")

    if not dry_run:
        save_state(state)

    # 3) Self-terminate on a confirmed, successfully-notified high-confidence hit.
    if did_high:
        if not dry_run:
            write_done("high-confidence R2 invite detected and notified")
            print("\n✅ High-confidence hit notified — monitor DISARMED "
                  "(DONE sentinel written). Run --reset to re-arm.")
        else:
            print("\n[dry-run] high-confidence hit — would write DONE sentinel "
                  "(skipped in dry-run).")
        return 20

    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Rivian R2 invite morning monitor.")
    parser.add_argument("--reset", action="store_true",
                        help="Clear the DONE sentinel and re-arm the monitor.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("guard", help="Exit 10 if disarmed, else 0.")
    sub.add_parser("reset", help="Clear the DONE sentinel and re-arm.")
    p_proc = sub.add_parser("process", help="Process classifier JSON and notify.")
    p_proc.add_argument("--input", help="Path to classifier JSON (else stdin).")
    p_proc.add_argument("--dry-run", action="store_true",
                        help="Run the pipeline but suppress real notifications "
                             "and never write the DONE sentinel or state.")

    args = parser.parse_args(argv)

    if args.reset:
        return cmd_reset()
    if args.command == "guard":
        return cmd_guard()
    if args.command == "reset":
        return cmd_reset()
    if args.command == "process":
        return cmd_process(args.input, args.dry_run)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
