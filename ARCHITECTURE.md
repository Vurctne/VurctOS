# Architecture

## Architecture Goal

The first architecture for VurctOS should be assistant-first, local-memory-backed, and useful before it becomes automated.

The system should begin with the VurctOS Core Assistant as the product center. Project folders remain important, but they are the first local context and memory substrate, not the product center. The Core Assistant uses files, workflows, memory, skills, and agent roles to coordinate creative work. MCP, plugins, and GUI layers should arrive later, after the assistant-first local model proves itself.

## System Overview

Three parts work together: memory (how the user works), local context (the project state), and the worker layer (where work runs). Claude acts as the Orchestrator that reads the first two, drives the third, and writes back.

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
  | PROFILE prefs     | | Local tools (auto):| | project folder/       |
  | style, decisions  | |  ffmpeg, Whisper,  | |  input/  frames/      |
  | what worked,      | |  ComfyUI, scripts  | |  analysis/ prompts/   |
  | what failed       | | Web tools (handoff)| |  final/  BOARD.md      |
  | durable, editable | |  ChatGPT, Kling... | | single source of truth |
  +---------^---------+ +---------+----------+ +-----------+-----------+
            |                     | outputs                |
            | capture lessons     +----------------------->| save to files
            +------------------------------------------------+
            loop: outputs return to context, lessons enter memory
```

- Memory is what carries across projects: stable preferences, style rules, decisions, and what worked or failed. Durable and editable. It means the user never has to re-explain how they work.
- Local context is this project's current state held as files, the single source of truth that every tool and person reads. It means the user never has to re-paste the project background between tools.
- The worker layer is where work actually runs. Local tools run automatically; web tools go through human handoff. This layer is not blocked by tool terms of service, because it runs on the user's own Claude subscription and local compute, with web tools kept human-in-the-loop.

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
  Worker tier: Claude as Executor, Codex, Gemini, ChatGPT, Hermes, video tools

Subscription-First Execution Layer
  the access mechanism for the Agent Role layer: official logins,
  CLI direct and copy handoff, explicit file handoff

Workflow Layer
  markdown workflow definitions and prompt-pack processes

Skill Layer
  workflows promoted from the Workflow layer, in the SKILL.md standard

Memory Layer
  durable memory, procedural memory (skills), session recall

Project Context Layer
  local files, folders, inputs, frames, analysis, prompts, outputs
```

The Skill layer sits on top of the Workflow layer: a skill is a workflow promoted after it proves useful. The Subscription-First Execution layer is the access mechanism for the Agent Role layer, not a separate set of agents.

## Core Assistant Layer

The Core Assistant is the operating center of VurctOS.

It should:

- understand user intent
- look up relevant memory
- select the right project context
- delegate work to specialized AI tools
- coordinate subscription-first handoff
- run workflows
- review outputs
- produce learning summaries
- update memory
- identify skill candidates

The Core Assistant is realized by Claude acting as Orchestrator. See `CORE.md` for the canonical loop and `ORCHESTRATION.md` for the coordination system.

## Project Context Substrate

The local project folder is the first context and memory substrate.

Each creative project should be understandable from files alone. A human, coding agent, Core Assistant, or future MCP server should be able to open the folder and know:

- what the project is
- what input assets exist
- what has been analyzed
- what prompts were created
- which outputs were produced
- what decisions were made
- what the system learned

Starter folder:

```text
project/
  AGENTS.md
  MEMORY.md
  PROFILE.md
  task.md
  README.md
  input/
  frames/
  analysis/
  prompts/
  images/
  videos/
  final/
```

Folders store context, evidence, outputs, prompts, and memory. The Core Assistant coordinates the work.

## Agent Roles

VurctOS defines roles before it automates orchestration. Claude acts as Orchestrator and delegates to worker roles based on intent, context, and memory.

The canonical role registry (Orchestrator tier, and worker tier: Claude as Executor, Codex, Gemini, ChatGPT, Hermes) lives in `CORE.md`. The agent profile format and the two delegation channels are defined in `ORCHESTRATION.md`.

## Memory Layer

The memory layer starts as readable files, organized in three layers: durable memory (`PROFILE.md` and `MEMORY.md`), procedural memory (skills), and session recall (session logs). The system should learn decision patterns, not only facts.

The Core Assistant uses memory before delegation and updates it after result review. The full memory model is defined in `docs/memory-system.md`.

## Workflow Layer

Workflows are repeatable production paths.

The Core Assistant runs workflows. Workflow files describe the pattern, but they are not the operating center.

The first flagship workflow is:

```text
Viral Video Reverse Engineering Workflow
```

It turns one viral video into a structured prompt pack through frame extraction, video analysis, shot breakdown, image prompts, video prompts, captions, and final packaging.

Workflows should be markdown-first until repeated steps justify scripts.

## Skill Layer

Skills are reusable workflows promoted from the Workflow layer after they prove useful. They follow the SKILL.md open standard. The skill model and the example skills are defined in `docs/skills-system.md`.

## Subscription-First Execution Layer

This layer is the access mechanism for the Agent Role layer. It prefers official subscription login over API keys and reaches tools through two channels: CLI direct and copy handoff. The principle and the preferred login paths are defined in `docs/subscription-first.md`, and the channels are defined in `ORCHESTRATION.md`.

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

A GUI can eventually help non-technical creators:

- create projects
- inspect memory
- run workflows
- view frames and prompts
- package final outputs

The first version should not build a GUI. The local project folder and docs should come first.

## Security And Privacy

VurctOS will handle creative IP and local project files.

Rules:

- do not commit secrets
- do not store account sessions in project files
- do not upload files without explicit user intent
- do not require API keys for the first workflow
- keep memory inspectable and editable
- make external handoff clear to the user
