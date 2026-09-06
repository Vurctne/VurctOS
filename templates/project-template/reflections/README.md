# Reflections

This folder holds reflection proposals produced by `vurctos reflect`. Each is a dated staging file, for example `2026-06-30.md`, where session day logs are distilled into proposed durable-memory changes before anything is committed.

## Flow

1. `vurctos reflect` gathers the session day logs not yet reflected and writes a proposal here with empty sections: additions to `USER.md`, additions to `MEMORY.md`, exact lines to prune, and skill candidates.
2. Claude as Orchestrator fills the proposal by distilling the logs (not copying them raw).
3. A human reviews and edits the proposal, then sets `status: approved`.
4. `vurctos reflect-apply` applies an approved proposal: it appends the additions under a dated `## Reflected Updates` block in `USER.md` / `MEMORY.md`, removes the pruned lines, and advances `.last-reflected` so those sessions are not reflected again.

The human approval gate is the point: distilled memory only reaches `USER.md` / `MEMORY.md` after you confirm it, so a wrong distilled fact never silently shapes later sessions.

The `.last-reflected` marker is generated machine-local instance data. Reflection proposals that contain real project detail are private instance data; keep them out of the public repository, as described in [docs/privacy-model.md](https://github.com/Vurctne/VurctOS/blob/main/docs/privacy-model.md) in the public VurctOS repository (this file is not included in the project folder).
