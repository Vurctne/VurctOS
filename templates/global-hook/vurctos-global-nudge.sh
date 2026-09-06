#!/bin/sh
# SessionStart nudge (user-level): surface the global VurctOS memory backlog
# at ~/.vurctos, and any reflection draft waiting for review, once at session
# start in every project. The numbers come from `vurctos memory-status
# --global --hook`, which also stages an empty draft once the backlog is
# large. Silent when nothing is waiting or ~/.vurctos does not exist.
# Install (bakes in the CLI path): see docs/memory-system.md.
cli="${VURCTOS_CLI:-}"
[ -n "$cli" ] || cli=__VURCTOS_CLI__
dir="${VURCTOS_HOME:-$HOME/.vurctos}"
[ -d "$dir/sessions" ] || exit 0
if [ ! -f "$cli" ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"VurctOS global nudge: the CLI path in ~/.claude/hooks/vurctos-global-nudge.sh does not exist; re-run the install line from docs/memory-system.md."}}\n'
  exit 0
fi
exec python3 "$cli" memory-status --global --hook
