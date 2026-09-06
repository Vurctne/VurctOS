# Roadmap

## Phase 0: Foundation Documentation And Repo Structure

Goal: create a clear open-source foundation without building the full system.

Deliverables:

- README, vision, manifesto, architecture, roadmap, MVP, and agent instructions
- contribution guide and license placeholder
- docs for getting started, memory, skills, and subscription-first direction
- a vibe coding workflow example
- local project template
- scripts and examples placeholders

Success criteria:

- a stranger on GitHub can understand VurctOS in less than 60 seconds
- the first MVP is clearly limited
- the repo has no unnecessary dependencies
- the project structure supports future work without overbuilding

## Phase 1: Local Project Workflow

Goal: make the local project folder usable for one real project.

Deliverables:

- finalized project template
- project creation instructions
- task file convention
- memory update convention
- board and handoff conventions
- manual validation checklist

Success criteria:

- a user can copy the template and start a project
- agents can understand the project from files alone
- project context stays local and readable

## Phase 2: Memory And Skill Capture

Goal: capture what the system learns from repeated work.

Status: the core of this phase ships in v1. `vurctos remember`/`recall` file and search session memory, `reflect`/`reflect-apply` run the human-gated consolidation loop (with `--global` for user-level, cross-project memory), and `skill-new` scaffolds a skill. What remains is accumulating real memory and skill examples from use.

Deliverables:

- memory file schema
- project memory examples
- style memory examples
- decision memory examples
- skill template
- first reusable skill drafts

Success criteria:

- completed work updates memory in a readable way
- repeated steps can become skills
- users can inspect and edit what was learned

## Phase 3: Agent Orchestration

Goal: strengthen the orchestration model in `ORCHESTRATION.md`, with Claude as Orchestrator coordinating Codex.

Status: a first slice ships in v1. `vurctos dispatch` runs one `ready` + `channel: local` board card headlessly on the subscription (to `review`, never `done`) and `vurctos reject` wires review verdicts back into memory. The continuous dispatcher loop and the rest of this phase remain future.

Deliverables:

- agent role playbooks
- handoff files between agents
- review workflow
- consistency checks
- orchestration rules that avoid overbuilding

Success criteria:

- each agent role has a clear job
- project files remain the shared context
- handoffs reduce copy-paste instead of adding complexity

## Phase 4: MCP Integration

Goal: add MCP only after the local workflow is proven.

Deliverables:

- local MCP server design
- project context tools
- memory query tools
- workflow execution tools
- permission model

Success criteria:

- MCP improves reliability and ergonomics
- local files remain the source of truth
- users understand what each MCP tool can access

## Phase 5: Workflow Packs

Goal: package repeatable coding workflows for reuse.

Deliverables:

- a vibe coding workflow pack
- a code review workflow pack
- a refactor or migration workflow pack
- import and export conventions

Success criteria:

- users can reuse workflows across projects
- workflows include prompts, folder rules, memory rules, and output standards
- workflow packs remain readable and versionable

## Phase 6: Community And Open-Source Ecosystem

Goal: make contribution practical and safe.

Deliverables:

- contribution process
- issue templates
- workflow contribution standards
- skill contribution standards
- review checklist
- governance notes

Success criteria:

- contributors can add docs, workflows, and skills without touching core runtime
- project direction remains coherent
- licensing is settled before serious public release

## Phase 7: Marketplace And Commercial Layers

Goal: explore Vurctne Skills and commercial layers after the open-source foundation is useful.

Deliverables:

- skill package model
- quality standard for paid workflow packs
- compatibility rules
- author attribution model
- commercial boundary document

Success criteria:

- open-source core remains useful
- commercial layers do not compromise local-first principles
- users understand what is free, open, paid, or proprietary
