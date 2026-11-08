# Architecture

## Architecture Goal

The first architecture for VurctOS should be assistant-first, local-memory-backed, and useful before it becomes automated.

The system should begin with the VurctOS Core Assistant as the product center. Project folders remain important, but they are the first local context and memory substrate, not the product center. The Core Assistant uses files, workflows, memory, skills, and agent roles to coordinate work across a user's AI coding tools. MCP, plugins, and GUI layers should arrive later, after the assistant-first local model proves itself.

## System Overview

Three parts work together: memory (how the user works), local context (the project state), and the worker layer (where work runs). Claude acts as the Orchestrator that reads the first two, drives the third, and writes back. Codex is the second worker: an independent reviewer and an executor.

```text
                          You (director)
                    intent / decisions / judgment
                                 |
                                 v
              CLAUDE = Orchestrator (the brain)
          read first  ->  act and delegate  ->  write back
            |                     |                      |
       read |               drive |                write |
            v                     v                      v
  +-------------------+ +--------------------+ +-----------------------+
  | 1  MEMORY         | | 2  WORKER LAYER    | | 3  LOCAL CONTEXT      |
  | across projects   | | does the work      | | this project, now     |
  |                   | |                    | |                       |
  | USER.md prefs     | | Claude as Executor | | project folder/       |
  | style, decisions  | | Codex reviewer +   | |  MEMORY.md USER.md    |
  | what worked,      | |  executor          | |  task.md handoffs/    |
  | what failed       | | local CLI (auto),  | |  BOARD.md             |
  | durable, editable | |  web tools (handoff)| | single source of truth |
  +---------^---------+ +---------+----------+ +-----------+-----------+
            |                     | outputs                |
            | capture lessons     +----------------------->| save to files
            +------------------------------------------------+
            loop: outputs return to context, lessons enter memory
```

- Memory is what carries across projects: stable preferences, style rules, decisions, and what worked or failed. Durable and editable. It means the user never has to re-explain how they work, and neither Claude Code nor Codex has to re-ask.
- Local context is this project's current state held as files, the single source of truth that every tool and person reads. It means the user never has to re-paste the project background between tools.
- The worker layer is where work actually runs. Local CLI execution runs automatically; any web tool goes through human handoff. This layer is not blocked by tool terms of service, because it runs on the user's own Claude and Codex subscriptions and local compute, with web tools kept human-in-the-loop.

Before each action the Orchestrator reads memory and local context, then uses the worker layer to do the work, writing outputs back into context and lessons into memory. Each loop makes the system fit the user better.

## High-Level Flow

The Core Assistant is realized by Claude acting as Orchestrator, which runs the canonical loop defined in `CORE.md`:

```text
User -> Orchestrator (Claude) -> canonical loop -> reviewed result + memory and skill updates
```

See `CORE.md` for the full nine-step loop and `ORCHESTRATION.md` for the coordination system that runs it.

## Layer Diagram

```text
Interface Layer
  CLI now, GUI later

Core Assistant Layer (Orchestrator)
  Claude as Orchestrator: intent, context selection, delegation, review, learning
  coordinates through a task board; see ORCHESTRATION.md

Agent Role Layer
  Orchestrator tier: Claude
  Worker tier: Claude as Executor, Codex (independent reviewer and executor)

Subscription-First Execution Layer
  the access mechanism for the Agent Role layer: official logins,
  CLI direct and copy handoff, explicit file handoff

Workflow Layer
  markdown workflow definitions and process descriptions

Skill Layer
  workflows promoted from the Workflow layer, in the SKILL.md standard

Memory Layer
  durable memory, procedural memory (skills), session recall

Project Context Layer
  local files, folders, board, handoffs, memory
```

The Skill layer sits on top of the Workflow layer: a skill is a workflow promoted after it proves useful. The Subscription-First Execution layer is the access mechanism for the Agent Role layer, not a separate set of agents.

## Core Assistant Layer

The Core Assistant is the operating center of VurctOS.

It should:

- understand user intent
- look up relevant memory
- select the right project context
- delegate work to Claude as Executor or to Codex
- coordinate subscription-first handoff
- run workflows
- review outputs
- produce learning summaries
- update memory
- identify skill candidates

The Core Assistant is realized by Claude acting as Orchestrator. See `CORE.md` for the canonical loop and `ORCHESTRATION.md` for the coordination system.

## Project Context Substrate

The local project folder is the first context and memory substrate.

Each project should be understandable from files alone. A human, a coding agent, the Core Assistant, or a future MCP server should be able to open the folder and know:

- what the project is
- what the current task is
- what has been done
- what was handed off and reviewed
- what decisions were made
- what the system learned

Starter folder:

```text
project/
  AGENTS.md
  BOARD.md
  MEMORY.md
  USER.md
  task.md
  README.md
  agents/
  handoffs/
  skills/
  sessions/
```

Folders store context, coordination state, handoffs, skills, and memory. The Core Assistant coordinates the work.

## Agent Roles

VurctOS defines roles before it automates orchestration. Claude acts as Orchestrator and delegates to worker roles based on intent, context, and memory.

The roster is small and deliberate: Claude as Orchestrator, Claude as Executor, and Codex as an independent reviewer and an executor. Claude never reviews its own execution output; Codex does that. The canonical role registry lives in `CORE.md`. The agent profile format and the two delegation channels (local CLI execution, and human-in-the-loop copy handoff for any web tool) are defined in `ORCHESTRATION.md`.

Hermes appears in this project only as a design-inspiration reference for the three-layer memory model. It is not a worker and is not required.

## Memory Layer

The memory layer starts as readable files, organized in three layers: durable memory (`USER.md` and `MEMORY.md`), procedural memory (skills), and session recall (session logs with a local SQLite full-text index). The system should learn decision patterns, not only facts.

The Core Assistant uses memory before delegation and updates it after result review. The full memory model is defined in `docs/memory-system.md`.

Because the memory is plain files, it is not tied to one tool. Claude Code auto-loads it through `@`-imports in `CLAUDE.md`; a Codex session loads the same durable files through a prose bridge in `AGENTS.md` (a project's `AGENTS.md` for project memory, and `~/.codex/AGENTS.md` for the user-level `~/.vurctos` layer). So a correction learned once, and consolidated once, holds across both Claude and Codex, on the user's own subscriptions, with no API keys. Writing and consolidating memory stays human-gated through the `vurctos` CLI; the bridges are read-only. See `docs/memory-system.md`.

## Workflow Layer

Workflows are repeatable working paths.

The Core Assistant runs workflows. Workflow files describe the pattern, but they are not the operating center.

The example workflow that ships is a "vibe coding" flow: a lightweight, memory-aware path for coding tasks that pulls in the user's preferences and past decisions before Claude or Codex touches the code.

Workflows should be markdown-first until repeated steps justify scripts.

## Skill Layer

Skills are reusable workflows promoted from the Workflow layer after they prove useful. They follow the SKILL.md open standard. New skills are scaffolded with `vurctos skill-new`. Lessons about a skill are filed as feedback entries in the day logs, and a revision answers one by citing `fb:<id>` in the skill's own files, so a rollback reopens it; `vurctos skill-status` reports what is still open. The skill model and the example skills are defined in `docs/skills-system.md`.

## Subscription-First Execution Layer

This layer is the access mechanism for the Agent Role layer. It prefers official subscription login over API keys and reaches tools through two channels: CLI direct (local execution, such as `dispatch --agent {claude,codex}`) and copy handoff (for any web tool, human-in-the-loop). The principle and the preferred login paths are defined in `docs/subscription-first.md`, and the channels are defined in `ORCHESTRATION.md`.

Single-card dispatch ships today: one `ready` board card runs headless on the user's subscription and moves to `review`, never straight to `done`. A continuous dispatcher loop is still future.

## Future MCP Layer

MCP should be added after the assistant-first local model is stable.

Future MCP capabilities:

- expose project context to agents
- query memory safely
- run approved workflow steps
- package outputs
- connect tool adapters
- enforce permission boundaries

MCP should not replace the local file system as the source of truth.

## Future Plugin Layer

Plugins can eventually support:

- new workflow packs
- new tool adapters
- new skill packs
- import and export formats
- community integrations

The plugin layer should wait until workflows and skills are stable enough to extend.

## Future GUI Layer

A GUI can eventually help users:

- create projects
- inspect memory
- run workflows
- review handoffs
- manage the board

The first version should not build a GUI. The local project folder and docs should come first.

## Security And Privacy

VurctOS will handle the user's project files and working memory.

Rules:

- do not commit secrets
- do not store account sessions in project files
- do not upload files without explicit user intent
- do not require API keys
- keep memory inspectable and editable
- make external handoff clear to the user
