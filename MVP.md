# MVP

## MVP Name

VurctOS Shared Memory MVP for Claude Code and Codex

## Tight Definition

The MVP is not the whole OS.

The MVP is a small local layer, plain markdown files plus a Python standard-library CLI, that gives Claude Code and Codex a shared, persistent memory plus lightweight single-card coordination, all running on the user's own subscriptions with no API keys.

The point is that a user's AI coding tools stop forgetting their preferences and decisions across sessions, and stop re-asking, because both tools read from the same local memory. The project folder and its memory files are the source of truth.

## User Story

As someone who codes with Claude Code and/or Codex, I want both tools to remember my preferences and past decisions across sessions and across tools, so they stop re-asking me the same things and start acting as one team, without any API key or cloud account.

## Inputs

Required:

- a local project folder (scaffolded from the template)
- the user's own Claude Code and/or Codex subscription

Optional:

- durable facts and preferences to record in `USER.md`
- project-specific decisions and lessons to record in `MEMORY.md`
- reusable procedures written as skills in the SKILL.md format
- global cross-project memory in `~/.vurctos` (via `--global`)

## Outputs

The MVP should produce:

- a durable memory layer (`USER.md` plus `MEMORY.md`) that persists across sessions
- a procedural memory layer (skills in the SKILL.md format)
- a session-recall layer (day logs plus a local SQLite full-text index)
- a shared-memory bridge that surfaces this memory into both tools: Claude Code via `@`-imports in `CLAUDE.md`, Codex via a prose `AGENTS.md` bridge (Codex has no `@`-import)
- a human-gated consolidation loop (`reflect` then `reflect-apply`) that distills session logs into durable memory only after the user approves
- single-card agent execution via `dispatch --agent {claude,codex}`, with work landing in review rather than auto-done, and `reject` to send it back with a recorded lesson

## Included

- local project template
- three-layer file memory: durable (`USER.md`, `MEMORY.md`), procedural (skills), session recall (day logs plus SQLite FTS index)
- the CLI commands: `new`, `remember`, `recall` (plus `--stats`), `reflect`, `reflect-apply`, `reindex`, `dispatch` (with `--agent`), `reject`, `skill-new`
- global cross-project memory under `~/.vurctos` (via `--global`), loaded into every session
- the shared-memory bridge into both Claude Code and Codex
- `skill-new` scaffolding for new procedural skills
- clear agent roles: Claude as Orchestrator and Executor, Codex as independent reviewer and Executor

## Excluded

- full GUI
- marketplace
- cloud sync
- a continuous dispatcher loop (single-card dispatch only for now)
- complex MCP orchestration
- automatic account login or web-tool automation
- required API keys
- multi-user collaboration

## Manual Steps

The user or agent may manually:

1. Create the project from the template.
2. Record preferences and project decisions with `remember` (they land in the day log; `USER.md` and `MEMORY.md` are written by an approved `reflect-apply`, and remain the user's own files to edit, never an agent's).
3. Write reusable procedures as skills, scaffolding them with `skill-new`.
4. Run `reflect`, review the staged consolidation, then `reflect-apply` to fold session logs into durable memory.
5. Queue a task as a board card and run it with `dispatch --agent {claude,codex}`.
6. Review dispatched work, and `reject` it with a reason when it is not right (the lesson is filed into memory and the card re-queued).

## Semi-Automated Steps

The CLI already handles the repeatable parts:

1. scaffold a new project folder (`new`)
2. file a memory entry into the day log and the SQLite index (`remember`)
3. full-text search past entries, with counts via `--stats` (`recall`)
4. rebuild the session index (`reindex`)
5. run one ready, local-channel board card on the user's subscription (`dispatch`)

No new dependencies should be added until a step is repeated enough to justify automation. The CLI is Python standard library only.

## Success Criteria

The MVP succeeds when:

- a preference or decision recorded once is remembered across later sessions
- both Claude Code and Codex read the same shared memory, so they stop re-asking the same questions
- the project can be understood from local files alone, with no API key or cloud account
- the `reflect` / `reflect-apply` loop turns session logs into durable memory under user approval
- a single dispatched card can be executed by either agent and lands in review, never auto-done
- a second project can reuse the same memory and skills with less setup

## Demo Script

1. Copy `templates/project-template/` into a new project folder (or run `new`).
2. Rename the project and fill in `USER.md`, `MEMORY.md`, and `task.md`.
3. Record a preference with `remember --global --what "..."` and a project decision with `remember --project <p> --what "..."`; both land in the day logs.
4. Run `reflect --global` and `reflect --project <p>`, fill and approve the staged consolidation, then `reflect-apply`: this is what writes `USER.md` and `MEMORY.md`.
5. Confirm the shared bridge: `CLAUDE.md` `@`-imports the memory for Claude Code, and `AGENTS.md` carries the same memory in prose for Codex.
6. Open both tools and confirm they surface the same preferences without re-asking.
7. Search past decisions with `recall --project <p> "<query>"`, and check counts with `recall --stats`.
8. Scaffold a reusable procedure with `skill-new <name>`.
9. Queue a board card, then run `dispatch --project <p> --agent codex` (or `--agent claude`); the card moves to review.
10. If the result is wrong, `reject <card-id> --project <p> --reason "..."`; the lesson is filed into memory and the card is re-queued.
11. As sessions accumulate, `memory-status` shows what is waiting; repeat `reflect` and `reflect-apply` to fold it into durable memory.
