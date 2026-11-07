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

## Deviations
One bullet per forced departure from the card: which field or item, what you did instead, and why. Write None. if you followed the card exactly.

## Notes For Review
Anything the Orchestrator should check, known limitations, and any unknowns the card did not answer (record them here instead of guessing silently).
```

Deviations is for results that contradict something the card states (name the field); Notes For Review is for what the card left silent or unclear. If you can point to the card line your result contradicts, it is a Deviation.

The Orchestrator reads the handoff during result review, checks it against intent, project constraints, and style memory, then accepts the card or reopens it with feedback.
