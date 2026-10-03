#!/usr/bin/env bash
# Run the grok-bot-gateway test suite plus shell lint.
# Safe: every test talks to 127.0.0.1 mocks only, never a real service.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL="$(cd "$HERE/.." && pwd)"

echo "== bash -n =="
bash -n "$SKILL/scripts/grokgw" "$SKILL/scripts/start.sh" "$HERE/run.sh"
echo "bash syntax ok"

echo "== shellcheck =="
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck "$SKILL/scripts/grokgw" "$SKILL/scripts/start.sh" "$HERE/run.sh"
  echo "shellcheck clean"
else
  echo "shellcheck not installed; skipping"
fi

echo "== unittest =="
python3 -m unittest discover -s "$HERE" -v
