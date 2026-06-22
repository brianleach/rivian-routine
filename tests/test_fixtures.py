#!/usr/bin/env python3
"""Offline regression tests for the R2 monitor's plumbing + tier routing.

These tests drive `r2_monitor.py process --input <fixture> --dry-run` against the
fixtures in ../fixtures and assert the exit code and the HIT/MAYBE/silent routing
the classifier's output should produce. They are deliberately:

  * stdlib-only (no pytest, no deps) — run with `python3 tests/test_fixtures.py`;
  * side-effect-free — every case runs in --dry-run, and the suite asserts that
    no state/ directory was created (CLAUDE.md invariant 4).

The fixtures pin the behaviour proven against a real inbox that actually held
the R2 invite — including the two false-positive traps a keyword/sender filter
would fail on: a transactional order *confirmation* (invite-looking but a
receipt) and a "Keep an eye out for your invite" pre-invite teaser (contains the
literal word "invite"). Both must stay silent; only the genuine personalized
invite may fire a HIGH hit and disarm (exit 20).
"""

from __future__ import annotations

import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR = os.path.join(REPO_ROOT, "r2_monitor.py")
FIXTURES = os.path.join(REPO_ROOT, "fixtures")
STATE_DIR = os.path.join(REPO_ROOT, "state")

# fixture filename -> expected (exit_code, high_count, maybe_count)
CASES = {
    # The real-inbox regression: exactly one genuine invite fires + disarms;
    # the confirmation, the teasers, the "keep an eye out for your invite"
    # pre-invite teaser, and the third-party forums digest all stay silent.
    "real_inbox_results.json": (20, 1, 0),
    # The shipped sample: one HIGH hit (disarms) plus one ambiguous MAYBE.
    "sample_results.json": (20, 1, 1),
    # A single ambiguous, clearly-Rivian order email surfaces as a MAYBE only —
    # no hit, no disarm (exit 0).
    "maybe_only_results.json": (0, 0, 1),
    # Pure marketing/noise (incl. the two traps): completely silent (exit 0).
    "all_marketing_results.json": (0, 0, 0),
}


def run_fixture(name: str) -> tuple[int, int, int]:
    """Run one fixture through `process --dry-run`; return (exit, hits, maybes)."""
    path = os.path.join(FIXTURES, name)
    proc = subprocess.run(
        [sys.executable, MONITOR, "process", "--input", path, "--dry-run"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    out = proc.stdout
    # Tier markers printed by cmd_process: "  [HIT ] " and "  [MAYBE] ".
    hits = out.count("[HIT ]")
    maybes = out.count("[MAYBE]")
    return proc.returncode, hits, maybes, out


def main() -> int:
    if not os.path.exists(MONITOR):
        print(f"FAIL: cannot find r2_monitor.py at {MONITOR}", file=sys.stderr)
        return 1

    # Invariant 4: dry-run is side-effect free. Snapshot whether state/ exists so
    # we can prove the suite never created it.
    state_existed_before = os.path.exists(STATE_DIR)

    failures = 0
    for name, (exp_exit, exp_high, exp_maybe) in CASES.items():
        if not os.path.exists(os.path.join(FIXTURES, name)):
            print(f"FAIL  {name}: fixture missing")
            failures += 1
            continue
        code, hits, maybes, out = run_fixture(name)
        problems = []
        if code != exp_exit:
            problems.append(f"exit {code} != {exp_exit}")
        if hits != exp_high:
            problems.append(f"HIT {hits} != {exp_high}")
        if maybes != exp_maybe:
            problems.append(f"MAYBE {maybes} != {exp_maybe}")
        if problems:
            failures += 1
            print(f"FAIL  {name}: " + "; ".join(problems))
            print("---- output ----")
            print(out.rstrip())
            print("----------------")
        else:
            print(f"ok    {name}: exit={code} high={hits} maybe={maybes}")

    # Side-effect check: dry-runs must not have created state/.
    if not state_existed_before and os.path.exists(STATE_DIR):
        failures += 1
        print("FAIL  side-effects: --dry-run created state/ (violates invariant 4)")
    else:
        print("ok    side-effects: --dry-run created no state/")

    if failures:
        print(f"\n{failures} test(s) FAILED")
        return 1
    print(f"\nAll {len(CASES)} fixture(s) passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
