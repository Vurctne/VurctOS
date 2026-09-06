# Core Assistant Model

VurctOS is assistant-first, not folder-first.

The local project folder remains important, but it is not the product center. It is the first local context and memory substrate.

The product center is the **VurctOS Core Assistant**: a personal AI operating layer for AI coding tools that understands user intent, recalls memory, selects project context, delegates work to the available agents, reviews outputs, updates memory, and turns repeated workflows into reusable skills.

The Core Assistant is realized concretely by Claude acting as Orchestrator. This page defines the operating loop the Orchestrator runs. The coordination system that supports it (the task board, agent profiles, delegation channels, and the handoff contract) is defined in `ORCHESTRATION.md`.

Status: today this loop is run by Claude plus Codex over local files, on the user's own subscriptions with no API keys. Single-card dispatch ships; the heavier coordination and automation described across these docs (a continuous dispatcher loop, MCP, a GUI) is the target operating model, not a shipped runtime.

## What The Core Assistant Is

The Core Assistant is the central intelligence of VurctOS.

It is not just a chat interface and not just a workflow runner. It is the operating layer that sits between the user, local context, memory, workflows, skills, and the AI coding tools (Claude Code and Codex).

The Core Assistant should:

- understand what the user is trying to accomplish
- identify which project, memory, and workflow context matters
- decide which agent role should handle each part of the work
- coordinate subscription-first tools through clean handoff
- review outputs before accepting them into the project record
- summarize what was learned
- update memory
- detect repeated patterns that should become skills
- become more personalized over time

## Main Loop

Canonical loop: `User request -> intent detection -> memory lookup -> project context selection -> agent delegation -> workflow execution -> result review -> learning summary -> memory update -> skill candidate update`.

```text
User request
  -> intent detection
  -> memory lookup
  -> project context selection
  -> agent delegation
  -> workflow execution
  -> result review
  -> learning summary
  -> memory update
  -> skill candidate update
```

This loop is the core operating model. Every workflow, project folder, memory file, agent handoff, and future MCP tool should support this loop.

## Base Rule: Finding Your Unknowns

Before any substantial step of the loop (planning, delegation, execution), the Orchestrator runs a cheap unknowns pass. The map is not the territory: the gap between the request/plan and the real world is the unknowns, and for substantial work surfacing them before acting is far cheaper than discovering them after execution.

Four quadrants, three moves:

- **Known knowns**: what the request states. Just do it.
- **Known unknowns**: what you know is unclear. Batch the key questions and ask them once, up front.
- **Unknown knowns**: assumptions so obvious nobody wrote them down. Name them explicitly and confirm the load-bearing ones.
- **Unknown unknowns**: what nobody thought to ask. Run a blindspot scan: (1) which unstated assumptions could bite, (2) which implicit conventions need confirming, (3) what blind spots exist in the method or checks you are about to use.

Never guess silently through an unknown. Surface it: the Orchestrator asks the user; an Executor records it in the handoff (see `ORCHESTRATION.md`). Skip the pass for trivial, well-trodden tasks.

(Adapted from Thariq Shihipar's "A Field Guide to Fable: Finding Your Unknowns".)

## 1. Intent Detection

The Core Assistant first identifies what kind of request the user is making.

Examples:

- implement a feature or fix a bug
- review a code change for correctness or consistency
- refactor a module and keep tests green
- organize a project
- write a helper script or tool
- research an approach across sources
- update memory
- extract a repeated workflow into a skill

Intent detection should also identify constraints:

- which project is active
- which tools are allowed
- whether the task needs code execution, review, research, or memory work
- whether the user wants planning, execution, or critique
- whether subscription-first handoff is enough or automation is justified

## 2. Memory Lookup

The Core Assistant uses memory before acting.

Memory can include:

- user preferences
- coding conventions and project rules
- decision patterns
- project facts
- prior decisions
- approaches that worked
- repeated failures
- tool-specific constraints
- previous skill candidates

The goal is not only to recall facts. The goal is to understand how the user thinks, decides, rejects, accepts, and improves work.

## 3. Project Context Selection

Project folders are the first local context and memory layer.

The Core Assistant selects only the context needed for the request:

- project profile
- task file
- source inputs
- notes
- relevant files
- outputs
- memory entries
- workflow state

The folder stores context, evidence, outputs, and memory. The folder does not run the product. The Core Assistant runs the product.

## 4. Agent Delegation

The Orchestrator does not do all the work itself. It decomposes the request, assigns work to agent profiles through a shared task board, coordinates subscription-first handoff where a web tool is involved, and reviews results before accepting them.

This section is the canonical registry of agent roles. The coordination mechanics (the board, agent profiles, the two delegation channels, and the handoff contract) are defined in `ORCHESTRATION.md`.

The roster is two tools, Claude and Codex, each wearing more than one hat.

### Orchestrator Tier

#### Claude (Orchestrator)

Claude runs the loop on this page: intent detection, memory lookup, context selection, delegation, result review, and memory and skill updates. As Orchestrator it should prefer to delegate, and it should never review its own execution output.

### Worker Tier

#### Claude as Executor

The same Claude engine assigned a board card as a worker, for:

- execution and implementation work
- local file operations
- project automation
- scripts and tools when justified
- converting repeated manual steps into practical tools

Codex reviews Claude as Executor output, so the planner and the reviewer are never the same.

#### Codex

Codex is an independent second agent (bridged into VurctOS through a prose `AGENTS.md` memory bridge, since Codex has no `@`-import). It wears two hats.

As reviewer, use Codex for:

- code review
- implementation review
- architecture consistency
- documentation consistency
- verification planning
- overbuild detection

As executor, Codex can also take a board card and do implementation or file work, dispatched through `dispatch --agent codex`. When Codex executes, Claude reviews, so the executor and the reviewer are still never the same.

## 5. Subscription-First Tool Coordination

The Core Assistant should prefer official subscription login workflows where practical.

Both Claude Code and Codex run on the user's own subscriptions with no API keys. Instead of forcing every task through APIs, the Core Assistant coordinates work through:

- copy-ready prompts
- clean file handoff
- explicit input lists
- expected output locations
- review checklists
- memory updates after tool results are inspected

API keys and automation are explicit opt-in only. The first useful operating model works with tools the user already subscribes to.

The subscription-first principle and the preferred login paths are defined in `docs/subscription-first.md`. The two concrete delegation channels are defined in `ORCHESTRATION.md`: Channel A is local CLI execution (scripts, tests, and the local coding tools run automatically), and Channel B is human-in-the-loop copy handoff for any web tool, so the system never programmatically auto-drives a subscription web UI.

## 6. Workflow Execution

Workflows are not the center of VurctOS. They are reusable operating patterns run by the Core Assistant.

For example, a vibe coding workflow is executed by the Core Assistant through steps such as:

- detect the user wants to build or change something in a project
- select relevant project context and memory
- draft a plan and confirm intent with the user
- delegate implementation to Claude as Executor (or dispatch a card to Codex)
- delegate review and consistency checking to the other agent
- run tests and scripts locally
- review outputs against project rules
- update memory and skill candidates

The workflow definition describes the pattern. The Core Assistant coordinates the work.

## 7. Result Review

The Core Assistant should not blindly accept outputs.

It should review results against:

- user intent
- project constraints
- coding conventions and memory
- quality criteria
- tool-specific limitations
- consistency rules
- prior user feedback

Review is delegated to the agent that did not produce the work:

- Codex reviews Claude as Executor output for correctness and consistency.
- Claude reviews Codex output when Codex executed a card.

A dispatched card is claimed as `in-progress` while it runs and then moves to `review`, never straight to `done`; it only reaches `review` if the run actually wrote its handoff and expected outputs. A rejected card files the lesson into memory and re-queues the work.

## 8. Learning Summary

After meaningful work, the Core Assistant produces a learning summary.

The learning summary should capture:

- what the user asked for
- what context mattered
- which tools or agents were used
- what worked
- what failed
- what the user accepted or rejected
- what should be remembered
- what repeated pattern may become a skill

This summary is the bridge between execution and memory.

## 9. Memory Update

Memory updates should be deliberate and inspectable.

The Core Assistant should update memory with:

- stable preferences
- project decisions
- coding conventions
- approaches that worked
- workflow lessons
- tool constraints
- quality standards
- rejected directions

Memory should remain editable by the user. VurctOS should never become a hidden memory black box.

The consolidation of session logs into durable memory is human-gated: `reflect` stages a proposed consolidation and `reflect-apply` applies it only after the user approves. Cross-project memory lives at `~/.vurctos` and is reached with `--global`; it is loaded into every session (into Claude Code via `@`-imports in `CLAUDE.md`, and into Codex through the prose `AGENTS.md` bridge).

## 10. Skill Candidate Update

When a workflow repeats, the Core Assistant should identify it as a skill candidate.

A skill candidate should include:

- name
- trigger condition
- required inputs
- process steps
- expected outputs
- agent roles
- review criteria
- memory behavior
- examples

Only repeated, useful patterns should become skills. A single prompt is not a skill. New skills are scaffolded with `skill-new`.

## Key Concepts

### Core Assistant

The central operating intelligence. It understands intent, coordinates the agents, runs workflows, reviews outputs, learns from outcomes, and personalizes over time.

### Agent Roles

The roles the Core Assistant delegates to: Claude as Executor and Codex as reviewer or executor. Agent roles are workers and reviewers. They are not the operating center.

### Workflows

Repeatable process patterns. Workflows describe how work should happen, but the Core Assistant decides when and how to run them.

### Memory

The assistant's inspectable learning layer. Memory stores preferences, project state, coding conventions, decisions, workflow lessons, and skill candidates.

### Skills

Reusable workflows that have proven themselves through repetition. Skills are promoted from repeated work and should include process, inputs, outputs, review criteria, and memory behavior. Skills follow the SKILL.md open standard; see `docs/skills-system.md`.

### Project Folders

The first local context and memory substrate. Project folders store files, evidence, inputs, outputs, and memory, but they are not the product center.

### Future MCP Layer

The future orchestration protocol layer. MCP can expose project context, memory, workflow tools, and adapter functions to the Core Assistant after the local model is stable.

### Memory And Learning Model

The three-layer memory design: durable memory (`USER.md` plus `MEMORY.md`), procedural memory (skills in the SKILL.md format), and session recall (`sessions/<date>.md` plus a local SQLite full-text index). The Orchestrator reads durable memory before acting, then updates memory, session logs, and skill candidates after review. VurctOS borrows this three-layer design as an inspiration reference from the open-source Hermes Agent project, but implements it as inspectable files first, not as a runtime. See `docs/memory-system.md`.

## Personalization Over Time

VurctOS becomes more useful as it observes repeated work.

It should learn from:

- repeated user requests
- accepted outputs
- rejected outputs
- corrections
- coding preferences
- project rules
- revisions
- workflow bottlenecks
- tool-specific successes and failures

Personalization should be practical. The Core Assistant should not claim to know the user in a vague way. It should improve by using visible memory and proven workflow patterns to reduce repeated explanation, improve delegation, and make better first drafts.

## Core Principle

Folders store the work.

Workflows structure the work.

Skills reuse the work.

Memory learns from the work.

Agents perform parts of the work.

The Core Assistant coordinates the work.
