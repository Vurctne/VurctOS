# Agent Profiles

Agent profiles define the roles used to coordinate work through board cards. This scaffold includes three profiles for Claude and Codex:

- [claude.md](claude.md): Claude as Orchestrator, planning and coordinating work.
- [claude-exec.md](claude-exec.md): Claude as Executor for coding and local automation, reviewed by Codex.
- [codex.md](codex.md): Codex as independent reviewer and second executor, with its execution output reviewed by Claude.

A profile declares:

- tier: orchestrator or worker
- channel: how the Orchestrator reaches it (`local` for local CLI execution, `handoff` for human copy handoff, or `native` for Claude)
- responsibilities: what kind of cards it should receive
- reviewed by: who checks its output
- handoff: where its result is written

Neither of the two canonical documents is included in the project folder: the role registry is [CORE.md](https://github.com/Vurctne/VurctOS/blob/main/CORE.md) and the coordination model is [ORCHESTRATION.md](https://github.com/Vurctne/VurctOS/blob/main/ORCHESTRATION.md), both in the public VurctOS repository.
