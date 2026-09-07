# VurctOS

Your AI coding tools forget you and do not talk to each other. VurctOS is a small local layer that gives Claude Code and Codex a shared memory, so they stop re-asking and start acting as one team.

VurctOS is a small, local-first tool for anyone who uses AI coding assistants. It gives Claude Code and OpenAI Codex a shared, persistent memory plus lightweight coordination, so they remember your preferences and decisions across sessions and across both tools, running on your own subscriptions with no API keys. It is plain markdown files plus a Python standard-library CLI.

> If VurctOS is useful to you, a star helps other people find it.

## The Problem

If you code with Claude Code and Codex, you already feel this:

- Each new session starts from zero. You re-explain your preferences, your project conventions, and the decisions you already made last week.
- The two tools do not share anything. What you told Claude Code, Codex has never heard, and the reverse.
- Nothing accumulates. A correction you made yesterday evaporates when the session closes, so the same mistake comes back.

You end up being the memory: copying context between tools, repeating yourself, and reminding your assistants of things they should already know.

VurctOS exists to remove that friction.

## What VurctOS Is

VurctOS is a personal AI operating layer for AI coding tools.

The center of the model is **Claude acting as the Orchestrator**: it plans, delegates, reviews, and updates memory. It also wears a second hat as an **Executor**, doing code and file work. **Codex** is the independent reviewer (Claude never reviews its own execution output) and can also act as an executor.

Coordination is local files only. A file-based task board (`BOARD.md`) is the single source of truth, worker profiles live in `agents/`, and results come back as typed handoff files. Agents never message each other directly; everything flows through inspectable files. See `CORE.md` for the operating loop and `ORCHESTRATION.md` for the coordination system.

The local project folder is the first context and memory substrate: the place where decisions, preferences, and memory stay in plain files you can read and edit.

There are two delegation channels, as a concept:

- **Local execution (Channel A):** local tools and scripts run automatically through the CLI (for example a headless coding run on your subscription).
- **Copy handoff (Channel B):** any web tool stays human-in-the-loop. VurctOS never programmatically auto-drives a subscription web tool, since that risks its terms; you copy the handoff across yourself.

The three-layer memory model is a local adoption of the Hermes Agent design (used only as design inspiration, not its runtime): durable memory, procedural skills, and session recall, all as plain files.

## What It Is Not

VurctOS is not:

- another generic agent framework
- a chatbot replacement
- a full desktop OS
- a GUI-first product
- a marketplace in version one
- an API-key-first automation framework
- a complete MCP orchestration system yet
- a tool that replaces Claude Code or Codex

The first version should stay small enough to understand, run, and improve.

## Core Principles

1. Prefer official subscription login workflows where possible.
2. Do not require API keys for the first version unless absolutely necessary.
3. Make Claude-as-Orchestrator the center of the coordination model.
4. Use local files and project context as the first context and memory substrate.
5. Add MCP orchestration later, after the assistant-first local foundation works.
6. Learn from repeated work and turn proven patterns into reusable skills.
7. Remember user preferences, style, project rules, and decision patterns.
8. Keep the system modular, open-source friendly, and easy to extend.
9. Share one memory across both Claude Code and Codex.
10. Do not overbuild the first version.

## Founder Brand Context

VurctOS sits underneath the Vurctne ecosystem:

- **Vurctne**: parent developer brand
- **VurctOS**: open-source personal AI operating layer, short for Vurctne Operating System
- **Vurctne Skills**: future workflow and skill marketplace

The open-source foundation should be useful on its own, while also supporting future Vurctne products.

## Current Project Status

This repository is in foundation mode.

Created now:

- the Orchestrator model (Claude as Orchestrator, Codex as reviewer)
- orchestration and coordination model
- project vision and manifesto
- architecture outline
- phased roadmap
- tight MVP definition
- agent instructions
- contribution guide
- documentation pages
- a coding workflow example
- local project template
- script and example placeholders

Shipped (v1):

- a local-first CLI (`cli/vurctos.py`, Python standard library only):
  - `vurctos new` scaffolds a project from the template
  - `vurctos remember` / `vurctos recall` file and full-text search session memory (SQLite FTS5 index, with a LIKE fallback that also covers CJK text); `recall --stats` reports what is indexed
  - `vurctos reindex` rebuilds the search index from the session day-logs
  - `vurctos reflect` / `vurctos reflect-apply` run the human-gated consolidation loop: reflect stages an empty proposal from the session day-logs, a human fills and approves it, reflect-apply mechanically prunes and appends it into durable memory (it refuses to run until status is `approved`, and writes nothing if any part of the proposal fails validation). `reflect-apply` is the only writer of `USER.md` / `MEMORY.md`; `remember` captures into the day-log and index only
  - `vurctos memory-status` shows the reflect backlog, the cursor, and any waiting draft, and stages an empty draft once the backlog is large; the shipped `SessionStart` nudge hooks call it
  - `vurctos aging` lists old reflected lines as retirement candidates once a durable file is over its size budget (read-only; retirement goes through the next approved reflect)
  - `vurctos dispatch` / `vurctos reject` are the single-card agent layer (see below)
  - `vurctos skill-new` scaffolds an empty SKILL.md skeleton for a proven, repeated pattern
- three-layer file memory, all plain inspectable files: durable (`USER.md` + `MEMORY.md`), procedural (`skills/` in the SKILL.md format), and session recall (`sessions/<date>.md` + the SQLite index)
- global, cross-project memory: `remember`, `recall`, `reflect`, and `reflect-apply` accept `--global`, which redirects the same operations to a user-level root at `~/.vurctos` (created private on first use), loaded into every session
- a shared-memory bridge into both tools: Claude Code reads memory via `@import` in `CLAUDE.md`; Codex reads the same durable memory through a prose bridge in `AGENTS.md` (Codex has no `@import`, so it is written in as plain prose). One memory, two tools.
- the repository is public and has had a security-hardening pass

Agent layer (deliberately minimal, one card at a time):

- `vurctos dispatch --project P [--agent {claude,codex}]` picks the first board card that is both `ready` and `channel: local`, claims it as `in-progress`, runs it once through a headless agent on the existing subscription (API and cloud credentials, including Codex billing tokens, are stripped from the child environment), verifies that the expected output files landed inside the project and hold different content than before the run, and moves the card to `review` on success or `blocked` when the run comes back failed. A dispatcher that is killed or crashes leaves the card `in-progress` as evidence. It never marks a card `done`; a human or Codex reviews it.
- Codex runs as an executor either directly (`--agent codex`) or through a delegation subagent wrapper (native Claude subagents are Claude-only, so Codex is invoked as a wrapper subagent).
- `vurctos reject <card-id> --reason "..."` files the reason as a memory lesson (day log plus index; it reaches durable memory through the next approved reflection), stamps it into the card, and re-queues the card as `ready` so the feedback rides along on the next dispatch.
- This is a single-run command, not a loop: repeated dispatch means repeated invocation (for example from cron), not a daemon.

Not created yet:

- a continuous dispatcher loop, daemon, or long-running runtime
- an API-key or metered-billing mode for dispatch (subscription login only)
- automated driving of subscription web tools (`channel: handoff` cards stay human-in-the-loop copy handoff)
- GUI
- MCP server
- marketplace
- browser automation
- cloud sync

## Repository Structure

```text
VurctOS/
  README.md
  CORE.md
  ORCHESTRATION.md
  VISION.md
  MANIFESTO.md
  ARCHITECTURE.md
  ROADMAP.md
  MVP.md
  AGENTS.md
  CONTRIBUTING.md
  LICENSE
  NOTICE
  docs/
  workflows/
  templates/project-template/
  scripts/
  examples/
```

## Planned Roadmap

- Phase 0: foundation documentation and repository structure
- Phase 1: local project workflow
- Phase 2: memory and skill capture (the memory layer and skill scaffolding ship in v1)
- Phase 3: agent orchestration (single-card `dispatch`/`reject` ships in v1; a continuous loop is future)
- Phase 4: MCP integration
- Phase 5: workflow packs
- Phase 6: community and open-source ecosystem
- Phase 7: marketplace and commercial layers

See `ROADMAP.md` for detail.

## License

VurctOS is open source under the Apache License 2.0. See `LICENSE` and `NOTICE`.

The project follows an open-core direction: the open-source foundation (coordination model, memory model, skill format, documentation) stays useful on its own, while future commercial layers such as the Vurctne Skills marketplace can build on top without compromising the local-first core.

The system is public; your VurctOS instance data is private. See `docs/privacy-model.md` for what is safe to commit and how to promote a private lesson into a public improvement.

## Early-Stage Disclaimer

VurctOS is an early-stage open-source project. The architecture, memory format, and implementation details may change as more real coding workflows are tested.

The foundation is intentionally practical: assistant-first coordination, local files as context substrate, subscription-first execution, memory that humans can inspect, and automation only where it proves useful.
