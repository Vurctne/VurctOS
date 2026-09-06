#!/bin/sh
# SessionStart nudge: surface this project's unconsolidated memory, and any
# reflection draft waiting for review, once at session start as hook JSON.
# The numbers come from `vurctos memory-status --hook`, which also stages an
# empty draft once the backlog is large. Silent when nothing is waiting.
# `vurctos new` bakes the CLI path in below; VURCTOS_CLI overrides it.
cli="${VURCTOS_CLI:-}"
[ -n "$cli" ] || cli=__VURCTOS_CLI__
dir="${CLAUDE_PROJECT_DIR:-.}"
[ -d "$dir/sessions" ] || exit 0
if [ ! -f "$cli" ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"VurctOS nudge: the CLI path in .claude/hooks/reflect-nudge.sh does not exist; point cli= at cli/vurctos.py in your VurctOS checkout."}}\n'
  exit 0
fi
exec python3 "$cli" memory-status --project "$dir" --hook
