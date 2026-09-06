# Handoffs

Each worker returns a structured handoff file, not free-form chat, so the Orchestrator can validate and route the result without information loss. One file per board card: `handoffs/<card-id>.md`.

## Handoff Template

```text
---
card: card-001
from: claude-exec
to: claude
status: done
inputs:
  - src/config.py
outputs:
  - src/config.py
  - tests/test_config.py
---

## Summary
One paragraph on what was produced.

## Result
The concrete output, or a pointer to the output files.

## Notes For Review
Anything the Orchestrator should check, known limitations, and any unknowns the card did not answer (record them here instead of guessing silently).
```

The Orchestrator reads the handoff during result review, checks it against intent, project constraints, and style memory, then accepts the card or reopens it with feedback.
