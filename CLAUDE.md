# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

This is **VurctOS**, a small, local-first tool that gives users of AI coding assistants (Claude Code and OpenAI Codex) a shared, persistent memory plus lightweight coordination, so their tools remember their preferences and decisions across sessions and across tools, running on their own subscriptions with no API keys.

Your AI coding tools forget you and do not talk to each other. VurctOS is a small local layer that gives Claude Code and Codex a shared memory, so they stop re-asking and start acting as one team.

It is plain markdown files plus a Python standard-library CLI. Public, Apache 2.0. It is for anyone who uses Claude Code and/or Codex, in any field (software, data, research, founders, students, anyone who codes with these tools).

The product center is the **Core Assistant**, realized concretely by **Claude acting as Orchestrator**. The local project folder is the first context and memory substrate, not the product center.

Current state: **v1**. A small local-first CLI exists (`cli/vurctos.py`); the coordination, memory, and skills layers are inspectable markdown files. Heavier automation (a continuous dispatcher loop, MCP, a GUI) is explicitly future. Do not introduce a runtime, dependencies, frameworks, MCP servers, or GUI code without explicit user approval, and do not overbuild.

> Note: if a sibling directory in the same local tooling folder contains an unrelated project with its own CLAUDE.md, it is not part of VurctOS and this guidance does not apply to it.

## Commands

- **Run the CLI** (Python 3.8+ standard library only):
  - `python3 cli/vurctos.py new <name> [--dir DIR]` - scaffold a project from `templates/project-template/`.
  - `python3 cli/vurctos.py remember --project <p> --what "..." [--kind decision|style|tool|fail|note] [--evidence "..."]` - file a memory entry into the day log `sessions/<date>.md` and the SQLite index (durable `USER.md` / `MEMORY.md` are written only by `reflect-apply`).
  - `python3 cli/vurctos.py memory-status --project <p> [--global]` - show the reflect backlog, the cursor, and any waiting draft; stages an empty draft once the backlog is large (the SessionStart nudge hooks call it with `--hook`).
  - `python3 cli/vurctos.py recall --project <p> "<query>"` - full-text search past entries (SQLite FTS5, LIKE fallback). `recall --stats` reports index counts.
  - `remember` / `recall` / `reflect` / `reflect-apply` all accept `--global` to target the user-level memory at `~/.vurctos/` (cross-project; loaded everywhere via the `@import` in `~/.claude/CLAUDE.md`).
  - `python3 cli/vurctos.py reflect --project <p>` then `reflect-apply` - stage and apply a human-approved consolidation of session logs into durable memory.
  - `python3 cli/vurctos.py reindex --project <p>` - rebuild the SQLite full-text index from the day logs.
  - `python3 cli/vurctos.py dispatch --project <p> [--agent claude|codex] [--dry-run]` - run one `ready` + `channel: local` board card via the chosen agent (headless `claude -p` or the Codex CLI) on the user's subscription; card moves to `review`, never `done`.
  - `python3 cli/vurctos.py reject <card-id> --project <p> --reason "..."` - reject reviewed work: files the lesson into memory, stamps it into the card notes, re-queues the card.
  - `python3 cli/vurctos.py skill-new <name> --project <p>` - scaffold a new procedural skill in the SKILL.md format.
- **Tests**: `python3 cli/test_vurctos.py` (stdlib `unittest`; no test framework dependency).
- No build or lint step, and no external tools. `vurctos.bundle` / `vurctossource.zip` are local repo snapshots, not source to edit.

## Core architecture concept

The mental model (requires reading `CORE.md`, `ORCHESTRATION.md`, `ARCHITECTURE.md` to fully grasp):

- **Claude is the Orchestrator, wearing two hats.** As Orchestrator (the Core Assistant) it plans, delegates, reviews, and updates memory. As Executor it is a worker for code and file tasks. It never reviews its own execution output; Codex does that.
- **Coordination is local files only.** `BOARD.md` is the single source of truth (kanban cards); `agents/<name>.md` are worker profiles; `handoffs/<card>.md` are typed results. Agents never message each other directly; everything flows through files.
- **Subscription-first, local-first, no API keys.** Hard compliance rule: never programmatically auto-drive subscription web tools, that risks their terms and bans. Any web tool is human-in-the-loop copy handoff (Channel B); local tools (the CLI, local scripts, the coding-assistant CLIs on the user's own subscription) are automated (Channel A). API mode is explicit opt-in only.
- **Shared memory bridged into both tools.** The same memory files feed Claude Code via `@`-imports in `CLAUDE.md`, and feed Codex via a prose `AGENTS.md` bridge (Codex has no `@`-import). One memory, two tools.
- **Three-layer memory, all inspectable files** (a local adoption inspired by the Hermes Agent memory design, not its runtime): durable (`USER.md` + `MEMORY.md`), procedural (`skills/` in the SKILL.md standard), session recall (`sessions/<date>.md` + a SQLite full-text index `sessions/index.db`). See `docs/memory-system.md`.
- **Agent roster**: Claude = Orchestrator and Executor; Codex = independent reviewer and Executor. Codex is a full second agent: it runs on the user's own subscription login (an OpenAI account, no API key) via the Codex CLI, is reachable through `dispatch --agent codex`, and has a `~/.claude/agents/codex.md` delegation subagent wrapper.

The kept example is a **vibe coding** workflow (`workflows/vibe-coding.md`): it is about coding, not media, and shows the memory and dispatch loop applied to an everyday coding task.

## Key files

- `README.md`, `VISION.md`, `MANIFESTO.md` - what VurctOS is and why.
- `CORE.md` - the Core Assistant model and the canonical operating loop.
- `ORCHESTRATION.md` - the coordination system: board, agent profiles, delegation channels, handoff contract, memory and skills.
- `ARCHITECTURE.md` - layer diagram and agent roles.
- `MVP.md`, `ROADMAP.md` - scope of now versus the phased plan.
- `AGENTS.md` - the authoritative working rules, and the Codex memory bridge; read before substantive changes.
- `docs/` - per-subsystem docs (memory-system, skills-system, subscription-first, getting-started, privacy-model).
- `cli/vurctos.py`, `cli/README.md` - the local CLI and its docs.
- `templates/project-template/` - the blank project skeleton (`BOARD.md`, `USER.md`, `MEMORY.md`, `task.md`, `agents/`, `handoffs/`, `skills/`, and `sessions/`).

## Hard rules

- **Open-source the system, not the user's private instance.** Never commit real personal memory, a real project's `USER.md` or `MEMORY.md`, source registers from real projects, agent logs with private context, secrets, tokens, browser profiles, or session files. Private instance data lives outside this repo. Examples must be fake or fully sanitized. Full policy: `docs/privacy-model.md`.
- **Commit author.** Commit as `Vurctne <contact@vurctne.com>` (the public Vurctne address). VurctOS is intended as a public open-source repo; confirm with the user before the first push or before adding a remote, since pushing publishes.
- **No em dash.** Never use the long em dash character anywhere. Use commas, colons, parentheses, short hyphens, or new lines instead.
- **Stdlib only.** No new dependencies, package manager, framework, runtime, or API keys without explicit approval.
- **Brand naming, used consistently:** Vurctne (parent brand), VurctOS (the OS), Vurctne Skills (future marketplace).
- **Separate now versus future.** Never describe a future system as already implemented. Single-card `dispatch` ships; a continuous dispatcher loop, MCP, and a GUI are still future. Do not delete source files or project documents unless the user explicitly asks. Keep affected docs internally consistent.

## Definition of done

A change is done when affected documents are internally consistent, no em dash was introduced, nothing was overbuilt or pushed, the tests pass if code changed, and `git status` has been checked.
