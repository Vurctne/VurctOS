#!/bin/sh
# One-shot smoke test for `vurctos dispatch` against a real, logged-in
# agent CLI (claude or codex). This is the ONLY check that spends
# subscription quota, so it is a manual script, deliberately not part of
# cli/test_vurctos.py (which mocks the agent subprocess).
#
# Usage:
#   sh scripts/smoke-dispatch.sh                       claude, positive
#   sh scripts/smoke-dispatch.sh --negative            claude, escaping output -> blocked
#   sh scripts/smoke-dispatch.sh --agent codex         codex, positive
#   sh scripts/smoke-dispatch.sh --agent codex --negative
#
# Run the positive case once after install (per agent), then re-run only
# after changing dispatch, _child_env, _run_*, _output_paths, or the output verification helpers.
set -eu

mode="positive"
agent="claude"
while [ $# -gt 0 ]; do
  case "$1" in
    --negative) mode="negative" ;;
    --agent)
      shift
      [ $# -gt 0 ] || { echo "--agent needs a value" >&2; exit 2; }
      agent="$1" ;;
    *) echo "unknown arg: $1"; exit 2 ;;
  esac
  shift
done

cli="$(cd "$(dirname "$0")/.." && pwd)/cli/vurctos.py"
command -v "$agent" >/dev/null 2>&1 || {
  echo "FAIL: $agent CLI not found on PATH (install it and log in)"; exit 1; }

tmp=$(mktemp -d)
python3 "$cli" new smoke --dir "$tmp" >/dev/null
proj="$tmp/smoke"

if [ "$mode" = "negative" ]; then
  # Deterministic failure: the expected output resolves OUTSIDE the
  # project, so dispatch must refuse it before running and the card must land in
  # blocked, no matter what the model does.
  expected="../escape.txt"
  notes="Write only the handoff file. Do not create any file outside the project directory."
else
  expected="final/smoke.txt"
  notes="Create the file final/smoke.txt containing exactly the single line: vurctos smoke ok (no other text, no trailing commentary). Do not modify any other file."
fi

cat >> "$proj/BOARD.md" <<EOF

- id: smoke-001
  title: Dispatch smoke test card
  assignee: $agent
  channel: local
  status: ready
  inputs:
  expected_outputs:
    - $expected
  handoff: handoffs/smoke-001.md
  notes: $notes
EOF

echo "dispatching real card in $proj (agent: $agent, mode: $mode)..."
python3 "$cli" dispatch --project "$proj" --agent "$agent" --timeout 300

status=$(awk '/^- id: smoke-001/{f=1} f && /status:/{print $2; exit}' "$proj/BOARD.md")

fail() { echo "FAIL: $1"; echo "project kept for inspection: $proj"; exit 1; }

if [ "$mode" = "negative" ]; then
  [ "$status" = "blocked" ] || fail "expected status blocked, got: $status"
  echo "PASS (negative): escaping output refused, card -> blocked"
else
  [ "$status" = "review" ] || fail "expected status review, got: $status"
  [ -f "$proj/final/smoke.txt" ] || fail "final/smoke.txt missing"
  content=$(cat "$proj/final/smoke.txt")
  [ "$content" = "vurctos smoke ok" ] || fail "wrong content: $content"
  [ -f "$proj/handoffs/smoke-001.md" ] || fail "handoff file missing"
  echo "PASS: card -> review, output content verified, handoff written"
fi
rm -rf "$tmp"
