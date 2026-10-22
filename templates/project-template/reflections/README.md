# Reflections

This folder holds reflection proposals produced by `vurctos reflect`. Each is a dated staging file, for example `2026-06-30.md`, where session day logs are distilled into proposed durable-memory changes before anything is committed.

## Flow

1. `vurctos reflect` gathers the session day logs not yet reflected and writes a proposal here: a digest the CLI computed (never applied), then empty sections for additions to `USER.md`, additions to `MEMORY.md`, exact lines to prune, skill candidates, and a rationale. A pending draft nobody has written into is moved to `archive/` instead of blocking.
2. Claude as Orchestrator fills the proposal by distilling the logs (not copying them raw).
3. `vurctos reflect-apply --check` shows the exact result (sizes after the apply, lines pruned) and an approval hash; it writes nothing.
4. A human reviews that result, then sets `status: approved`.
5. `vurctos reflect-apply --approved-hash <hash>` applies it: it copies `USER.md` / `MEMORY.md` to `backup/<proposal>/`, removes the pruned lines, appends the additions under a dated `## Reflected Updates` block, replaces each file atomically, and advances `.last-reflected` so those sessions are not reflected again. It refuses a result over the size budget, an added line over 512 bytes, or a hash that no longer matches.

The human approval gate is the point: distilled memory only reaches `USER.md` / `MEMORY.md` after you confirm it, so a wrong distilled fact never silently shapes later sessions.

The `.last-reflected` marker is generated machine-local instance data. Reflection proposals that contain real project detail are private instance data; keep them out of the public repository, as described in [docs/privacy-model.md](https://github.com/Vurctne/VurctOS/blob/main/docs/privacy-model.md) in the public VurctOS repository (this file is not included in the project folder).
