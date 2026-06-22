#!/bin/bash
# SessionStart hook for Claude Code on the web.
#
# This project is standard-library only — there are no dependencies to install.
# So instead of an install step, we run the offline regression suite as a fast
# smoke check (it is stdlib-only and side-effect free), so the session starts
# knowing the classifier->plumbing contract is green.
set -euo pipefail

cd "${CLAUDE_PROJECT_DIR:-.}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "session-start: python3 not found; skipping regression smoke check." >&2
  exit 0
fi

python3 tests/test_fixtures.py
