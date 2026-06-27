# Architecture

## Architecture Goal

The first architecture for Vurctne OS should be simple, local-first, and useful before it becomes automated.

The system should begin as a project-folder operating layer for AI creative work. It should use files, workflows, memory, and skills as the foundation. MCP, plugins, and GUI layers should arrive later, after the local workflow proves itself.

## High-Level Flow

```text
User
  -> Vurctne OS
  -> Project Context
  -> Agent Roles
  -> Workflows
  -> Memory
  -> Skills
  -> Outputs
```

## Layer Diagram

```text
Interface Layer
  CLI now, GUI later

Subscription-First Execution Layer
  official logins, copy-ready prompts, file handoff

Skill Layer
  reusable workflow modules learned from repeated work

Workflow Layer
  markdown workflow definitions and prompt-pack processes

Memory Layer
  profile, project memory, workflow memory, style memory, decision memory

Agent Role Layer
  Claude Code, Codex, Gemini, ChatGPT, Hermes Agent

Project Context Layer
  local files, folders, inputs, frames, analysis, prompts, outputs
```

## Local-First Project Folder System

The local project folder is the source of truth.

Each creative project should be understandable from files alone. A human, coding agent, or future MCP server should be able to open the folder and know:

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

## Agent Roles

Vurctne OS should define roles before it tries to automate orchestration.

### Claude Code

Role: CTO, executor, project automation engineer.

Responsibilities:

- create and modify project files
- build simple scripts when needed
- automate repeated local steps
- keep implementation practical
- avoid overbuilding

### Codex

Role: code review, implementation review, consistency checking.

Responsibilities:

- review architecture and implementation choices
- check repo consistency
- find missing docs and quality gaps
- verify changes before completion

### Gemini

Role: video analysis and long-context research.

Responsibilities:

- analyze long videos and transcripts
- compare references
- extract structure from large context
- support research-heavy workflow steps

### ChatGPT

Role: creative direction, prompt writing, visual judgment.

Responsibilities:

- write creative prompts
- judge concept strength
- improve scene and shot language
- help with visual direction

### Hermes Agent

Role: future memory and self-learning layer.

Responsibilities:

- capture repeated decisions
- summarize workflow learnings
- update memory files
- recommend new skills from repeated work

Hermes Agent is a future concept, not part of the first implementation.

## Memory Layer

The memory layer should start as readable files.

Memory types:

- `PROFILE.md`: stable user and project preferences
- `MEMORY.md`: accumulated project memory
- project memory: facts and constraints for this project
- workflow memory: what happened in the current workflow
- style memory: recurring taste and visual rules
- decision memory: why choices were made

The system should learn decision patterns, not only facts.

## Workflow Layer

Workflows are repeatable production paths.

The first flagship workflow is:

```text
Viral Video Reverse Engineering Workflow
```

It turns one viral video into a structured prompt pack through frame extraction, video analysis, shot breakdown, image prompts, video prompts, captions, and final packaging.

Workflows should be markdown-first until repeated steps justify scripts.

## Skill Layer

Skills are reusable workflows learned from repeated work.

Early skill examples:

- Viral Video Analysis Skill
- Kling Prompt Skill
- AI Ad Film Skill
- Vibe Coding Setup Skill
- School Finance Automation Skill

The first repository should document skill concepts before building a skill runtime.

## Subscription-First Execution Layer

The execution layer should prefer official subscription login workflows where possible.

Examples:

- Claude Code login for coding execution
- Codex and ChatGPT login for review and creative prompting
- Gemini CLI login for analysis and research
- web subscriptions for Kling, Runway, Veo, Hailuo, and similar tools

The first version can create copy-ready prompts and file handoff instructions. API keys may be supported later but should not be required now.

## Future MCP Layer

MCP should be added after the local project model is stable.

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

Vurctne OS will handle creative IP and local project files.

Rules:

- do not commit secrets
- do not store account sessions in project files
- do not upload files without explicit user intent
- do not require API keys for the first workflow
- keep memory inspectable and editable
- make external handoff clear to the user
