# Profile: codex

- tier: worker
- channel: local
- responsibilities: independent code and implementation review, architecture and documentation consistency, verification planning, overbuild detection, second executor for coding and local file work
- reviewed by: the Orchestrator
- handoff: handoffs/<card-id>.md

## Notes

Reviews claude-exec output independently. When Codex executes a card, Claude reviews its output. Reached headlessly by `vurctos dispatch --agent codex` on the user's own subscription login, or inside a live Claude Code session through the codex delegation subagent. Dispatch runs card instructions with edits accepted, so only dispatch boards whose cards the operator authored or reviewed, never third-party boards.
