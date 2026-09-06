# Orchestration

How the VurctOS Core Assistant coordinates your AI coding tools.

This document defines the master coordination system: Claude acting as Orchestrator, a file-based task board, agent profiles, the handoff contract, and how memory and skills align to open standards. It is the concrete realization of the Core Assistant model in `CORE.md`.

## Status: Now Versus Future

Now (v1, markdown-first, no API keys required):

- Claude (via Claude Code subscription login) acts as the Orchestrator.
- The task board, agent profiles, handoffs, memory, and skills are all plain files.
- Work is coordinated by Claude reading and writing those files, not by a background runtime.
- `vurctos dispatch` claims and runs one `ready` + `channel: local` board card headlessly on the subscription (one card per invocation; the result goes to `review`, never `done`). `--agent {claude,codex}` selects the executor. `vurctos reject` files review feedback into memory and re-queues the card.

Future (only after the local model proves useful):

- Scripts that assemble handoffs and count skill repetitions.
- A continuous dispatcher loop and MCP tools.
- A GUI over the same files.

VurctOS borrows the three-layer memory model as a design inspiration from the Hermes Agent project, but does not depend on its runtime. Subscription-first coordination and copy handoff for web tools are non-negotiable, so the Orchestrator is always VurctOS itself.

## 1. Claude As Orchestrator

The Core Assistant is an abstract role. In VurctOS it is realized concretely by Claude acting as Orchestrator.

The Orchestrator owns the canonical loop in `CORE.md`. It does not do all the work itself. It decomposes the request, assigns work to agent profiles through the board, coordinates subscription-first handoff, reviews typed results, and updates memory and skills.

Claude wears two hats, same engine, different identity:

- Claude as Orchestrator: the Core Assistant. Plans, delegates, reviews, learns. Never reviews its own execution output.
- Claude as Executor: a worker profile for coding and local file work, assigned a board card like any other worker.

The Orchestrator should prefer to delegate. When Claude executes a card as Executor, an independent reviewer (Codex) checks the result, so the planner and the judge are never the same hat.

## 2. The Board: File-Based Coordination

The board is the single source of truth for coordination, implemented as a markdown file, `BOARD.md`, in the project folder.

Principle: agents do not talk to each other directly. All coordination flows through the board and through handoff files. This keeps the project understandable from files alone and avoids hidden state.

Each task is a card:

```text
- id: card-003
  title: Refactor the config loader to read from a single source
  assignee: claude-exec
  channel: local
  status: ready
  inputs:
    - src/config.py
    - USER.md
  expected_outputs:
    - src/config.py
    - tests/test_config.py
  handoff: handoffs/card-003.md
  notes: Keep the public function signatures unchanged.
```

Card status values: `backlog`, `ready`, `in-progress`, `review`, `done`, `blocked`.

The Orchestrator is responsible for creating cards during task decomposition, moving cards through statuses, and never marking a card `done` before result review (loop step 7).

## 3. Agent Profiles

Agent roles are defined as profiles, where each profile can specify its own model, tools, and access. A profile lives in `agents/<name>.md` and declares how the Orchestrator should delegate to it.

Profiles are organized in two tiers.

Orchestrator tier:

- `claude` (Orchestrator): runs the loop, owns the board, delegates, reviews, updates memory.

Worker tier:

- `codex`: independent reviewer and executor. Reviews Claude-as-Executor output for correctness and consistency, and can also be assigned cards to implement.
- `claude-exec` (Claude as Executor): file creation, local automation, small scripts, coding tasks.

### Delegation Channels And The Automation Boundary

Workers are reached through delegation channels, with a hard compliance rule between them.

Compliance rule: VurctOS does not automate subscription tools as if they were APIs. A third party programmatically driving a subscription login can violate those tools' terms of service and risk account suspension. Background, headless, or batch automation of a subscription web login is not allowed.

- Channel A, assisted local execution: the Orchestrator runs only tools whose terms permit programmatic local use, such as `claude-exec` (its own code and file work) and the Codex CLI (which shares its own subscription login locally). `vurctos dispatch --agent {claude,codex}` uses this channel to claim and run one `channel: local` card headlessly. It does not headlessly drive subscription web logins.
- Channel B, copy handoff: for any web tool without a compliant local path, the Orchestrator produces a copy-ready prompt, an input list, and an expected output location. A human runs the tool in the browser and saves the result back. Fully human-in-the-loop and within terms of service. (For example, pasting a prompt into a web chat and saving the reply into a handoff file.)
- API mode (opt-in only): for the subset of tools that offer a compliant API, the user may explicitly enable API access for a profile. This is never the default and is never required for v1.

A profile file declares: tier, channel, responsibilities, allowed skills, the handoff format it expects, and who reviews its output.

## 4. The Handoff Contract

Workers do not return free-form chat. Each handoff is a structured file, `handoffs/<card-id>.md`, so results can be validated and routed without information loss.

```text
---
card: card-003
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
Anything the Orchestrator should check, known limitations, and any unknowns the card did not answer (an Executor records these here instead of guessing silently; see the base rule in `CORE.md`).
```

The Orchestrator reads the handoff during result review (loop step 7), checks it against intent, project constraints, and style memory, then either accepts it (card to `done`) or reopens the card with feedback. Reopening is mechanized by `vurctos reject <card-id> --reason "..."`: the reason is filed into memory, stamped into the card notes so the re-run carries it, and the card returns to `ready`.

## 5. Memory Model: Three Layers

VurctOS uses a three-layer memory model, adapted as a design inspiration from the Hermes Agent project and implemented as inspectable files. This reconciles a mixed memory taxonomy into three clear layers.

1. Durable memory, stable facts and preferences:
   - `USER.md`: stable user and style preferences.
   - `MEMORY.md`: durable project facts, decisions, and style or rejection rules. Decision memory and style memory are sections here, not separate files.

2. Procedural memory, how to do things:
   - `skills/`: reusable procedures in the SKILL.md format (section 6).

3. Session recall, what happened before:
   - `sessions/`: dated day logs in markdown, plus a local SQLite full-text index at `sessions/index.db` (FTS5, with a LIKE fallback) for cross-session recall. Filed by `vurctos remember` and searched by `vurctos recall` (`recall --stats` reports index coverage; `vurctos reindex` rebuilds the index from the day logs).

All four memory commands (`remember`, `recall`, `reflect`, `reflect-apply`) also accept `--global`, which targets a user-level root at `~/.vurctos` with the same layout, for memory that should persist across all projects rather than one. Global memory is bridged into both tools: into Claude Code via `@`-imports in `CLAUDE.md`, and into Codex via a prose `AGENTS.md` bridge (Codex has no `@`-import). See `docs/memory-system.md`.

The Orchestrator reads durable memory and relevant skills before acting (loop steps 2 and 3) and appends to durable memory and session logs after review (loop steps 8 and 9). Memory stays human-readable and editable. VurctOS never becomes a hidden memory black box.

## 6. Skills: The SKILL.md Open Standard

VurctOS skills follow the agentskills.io open standard, so a VurctOS skill is portable to and from the wider ecosystem, and the future Vurctne Skills marketplace stays compatible by default.

A skill is a directory under `skills/`:

```text
skills/
  vibe-coding/
    SKILL.md
    references/
    scripts/
    assets/
```

`SKILL.md` is YAML frontmatter plus a markdown body. Required frontmatter is `name` and `description` only:

```text
---
name: vibe-coding
description: Turn a rough feature idea into a small, tested change through plan, implement, and review. Use when the user describes a coding task loosely and wants it decomposed and delivered.
version: 0.1.0
author: vurctne
---

## When To Use
...

## Inputs
...

## Steps
...

## Outputs
...

## Review Criteria
...

## Memory Updates
...
```

Rules from the standard: `name` is lowercase letters, numbers, and hyphens, and must match the folder name. `description` must state both what the skill does and when to use it, since that is what makes the Orchestrator select it. Avoid angle brackets in frontmatter. New skills are scaffolded with `vurctos skill-new <name>`. Promotion rule from `CORE.md` still applies: only a repeated, useful pattern becomes a skill.

## 7. How This Runs The Loop

The canonical loop lives in `CORE.md`. This is how each step maps to the orchestration primitives above:

- Intent detection: the Orchestrator reads the request and constraints.
- Memory lookup: read durable memory and relevant skills (section 5 and 6).
- Project context selection: select only the files the cards will need.
- Agent delegation: create board cards, assign profiles and channels (sections 2 and 3).
- Workflow execution: workers run through local execution or copy handoff (section 3).
- Result review: read handoffs, check against memory, accept or reopen (section 4).
- Learning summary: write what worked and failed into a session log.
- Memory update: append durable facts and decisions (section 5).
- Skill candidate update: count repetition, promote to a SKILL.md when justified (section 6).

## 8. Subscription-First And Safety

- Prefer subscription login over API keys. The first version requires no API keys.
- Do not commit secrets, tokens, browser sessions, or private data.
- Do not automate subscription web tools as if they were APIs. Driving a subscription web login from an orchestrator can violate platform terms and risk account suspension. Channel B keeps a human in the loop for these tools; programmatic access is limited to tools whose terms allow local use (such as the Claude Code and Codex CLIs on their own subscriptions), or to an explicit opt-in API mode.
- Keep the board, handoffs, memory, and skills inspectable and editable.

## 9. Growth Path

```text
v1  markdown coordination
    BOARD.md, agents/, handoffs/, three-layer memory (incl. sessions/index.db), SKILL.md skills,
    vurctos dispatch --agent {claude,codex} / reject (single-card claim and run)
      -> v2  small scripts
             assemble handoffs, count skill repetitions
        -> v3  continuous dispatcher loop, MCP tools
          -> future  a GUI over the same files
```

Each step is added only when the previous one proves useful. The file formats above follow the SKILL.md standard and a portable memory layout, so later integration stays low-cost.

## Design Inspiration: Hermes Agent

VurctOS adapts one idea as design inspiration from the open-source Hermes Agent project by Nous Research: the three-layer memory model (durable, procedural, session recall). VurctOS implements it, along with the board, agent profiles, and typed handoffs, as subscription-first markdown files rather than as a runtime. Hermes is a reference here, not a dependency or a worker.
