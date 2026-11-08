# AGENTS.md

This repository is the foundation for VurctOS, an open-source, local-first memory and coordination layer for AI coding assistants.

## Project Purpose

Your AI coding tools forget you and do not talk to each other. VurctOS is a small local layer that gives Claude Code and Codex a shared memory, so they stop re-asking and start acting as one team.

It runs on the user's own subscriptions with no API keys, using plain markdown files plus a Python standard-library CLI. The memory and coordination substrate is inspectable files, not a hidden runtime: durable memory (`USER.md` + `MEMORY.md`), procedural skills in the SKILL.md format, and session recall backed by a local SQLite full-text index. The shared memory bridges into both tools (Claude Code via `@`-imports in `CLAUDE.md`, Codex via a prose bridge in `AGENTS.md` since Codex has no `@`-import).

## Current Phase

The project has moved from foundation documentation into building a minimal v1.

v1 is a small, local-first CLI (`cli/`) that handles only the deterministic plumbing: scaffolding a project (`new`), filing and searching memory (`remember` / `recall`, with `recall --stats`), rebuilding the recall index (`reindex`), staging and applying human-approved consolidation (`reflect` / `reflect-apply`, also `--global` for the cross-project memory at `~/.vurctos`), reporting the consolidation backlog and old prune candidates read-only (`memory-status` / `aging`), running one board card headlessly against Claude or Codex (`dispatch --agent {claude,codex}`, to `review`, never `done`), re-queuing rejected work with the lesson filed (`reject`, optionally `--skill` to route it to a skill), scaffolding a skill skeleton (`skill-new`), reporting the feedback each skill has not answered yet, read-only (`skill-status`; feedback is filed with `remember --skill ... --signal ...`), and checking a skill edit statically with no model (`skill-snapshot` copies a skill's tree as a work pair or the accepted baseline; `skill-check` compares a candidate with its base and answers pass, hold for the owner, or fail). The intelligence (analysis, distillation, review) stays with Claude acting as Orchestrator and Codex acting as an independent reviewer. Keep the same discipline: standard library only, no heavy frameworks, runtime, or package manager, and add code only where a manual step clearly repeats. A continuous dispatcher loop, MCP, and a GUI are still future.

## Required Reading

Before making substantive changes, read:

- `README.md`
- `CORE.md`
- `ORCHESTRATION.md`
- `VISION.md`
- `MANIFESTO.md`
- `ARCHITECTURE.md`
- `MVP.md`
- `ROADMAP.md`
- `docs/subscription-first.md`
- `docs/memory-system.md`
- `docs/skills-system.md`
- `docs/privacy-model.md`

## Brand Rules

- Vurctne is the parent developer brand.
- VurctOS is the open-source personal AI operating layer for AI coding tools.
- Vurctne Skills is the future workflow and skill marketplace.

Use these names consistently.

## Writing Rules

- Avoid the English long em dash character and the Chinese double dash.
- Use commas, colons, parentheses, short hyphens, or new lines instead.
- Keep documentation practical and honest.
- Explain what exists now versus what is future direction.
- Do not pretend future systems are already implemented.

## Safety Rules

- Do not commit secrets, tokens, browser profiles, session files, real personal memory, or account credentials. The full public/private policy is `docs/privacy-model.md`; review staged files before every commit.
- Avoid APIs unless the user explicitly asks for API-based integration.
- Prefer official subscription login workflows where practical and permitted.
- Do not automate websites in ways that violate platform terms.
- Preserve local-first behavior.

## Repository Rules

- Do not delete source files or project documents unless the user explicitly asks.
- Do not overbuild.
- Prefer markdown and simple scripts first.
- Do not add package managers, frameworks, build systems, MCP servers, or GUI code without explicit approval.
- Preserve modular architecture.
- Always update docs when making structural changes.
- Keep project folders readable by humans and AI agents.

## Architecture Rules

- Local files are the first stable communication layer.
- Agents coordinate through the task board and handoff files, not direct messaging.
- The two delegation channels stay distinct: local CLI execution is automated (Channel A); any web tool is human-in-the-loop copy handoff (Channel B). The dispatcher automates only `channel: local` cards, via headless Claude Code or Codex on the user's own subscription login. `channel: handoff` cards always stay human-in-the-loop; never weaken this boundary.
- Dispatch trust model: dispatch runs a card's instructions with edits accepted (prompt-injection by design), so only dispatch boards whose cards were authored or reviewed by the operator, never a third-party board. Document this wherever dispatch is described.
- Workflows should be markdown-first until automation is justified.
- Memory should be inspectable and editable.
- Skills should be reusable workflow patterns, not hidden magic.
- MCP comes later, after local workflows prove useful.
- Plugin and marketplace layers come after the open-source foundation is coherent.

## Agent Role Defaults

- Claude: Orchestrator (the Core Assistant), and as a worker the Executor for code and local automation.
- Codex: independent code and implementation review, consistency checking, and a second executor. Claude never reviews its own execution output; Codex does that.
- Hermes: referenced only as a design inspiration for the three-layer memory model, not a worker and not required.

The canonical role registry is in `CORE.md`; the coordination system is in `ORCHESTRATION.md`.

## Definition Of Done

A change is done when:

- affected documents are internally consistent
- no forbidden long em dash or Chinese double dash characters were introduced
- the repository still reflects a documentation-first foundation
- no full-system implementation has been introduced accidentally
- git status has been checked
