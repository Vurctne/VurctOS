# Project Agent Rules

This folder is a VurctOS project: a shared, persistent memory and a single-card agent layer for AI coding assistants (Claude Code and Codex), on your own subscriptions, no API keys.

## Project Purpose

Use this project folder as the source of truth for one body of work (a codebase, a task, an ongoing effort).

Agents should read local files before acting.

## Required Reading

Before making changes, read:

- `README.md`
- `BOARD.md`
- `USER.md`
- `task.md`
- `MEMORY.md`

## Base Rule: Finding Your Unknowns

Before substantial work, surface unknowns instead of guessing silently. The Orchestrator batch-asks the key questions once and confirms the obvious-but-unwritten assumptions; an Executor running a card records any unknown the card does not answer under Notes For Review in the handoff. Skip for trivial tasks. Canonical rule: https://github.com/Vurctne/VurctOS/blob/main/CORE.md#base-rule-finding-your-unknowns

## Durable Memory (read before acting)

This project carries durable memory in two plain files at the project root:

- `USER.md`: stable facts about the user and how they like to work, plus decision patterns.
- `MEMORY.md`: project facts, conventions, decisions, and what worked or failed.

Open both files and treat their contents as known context before you plan or act. This instruction is written out in prose on purpose: Codex does not expand `@file` import syntax, so the files are named here for you to open directly. A Claude Code session gets the same two files via the `@USER.md` / `@MEMORY.md` imports in `CLAUDE.md`.

User-level global memory lives outside this project at `~/.vurctos/USER.md` and `~/.vurctos/MEMORY.md`. In Codex, that global layer is loaded once per session from `~/.codex/AGENTS.md` and is not re-read here (see [docs/memory-system.md](https://github.com/Vurctne/VurctOS/blob/main/docs/memory-system.md) in the public VurctOS repository; that guide is not included in this project folder).

Do not write to any of these durable-memory files directly. Capture goes through `vurctos remember` (day log and index only); the durable files change only through the human-gated `vurctos reflect` / `reflect-apply`, never through an agent editing them.

## Working Rules

- Keep files human-readable.
- Coordinate through `BOARD.md` and `handoffs/`, not direct agent messaging.
- Do not delete source files.
- Do not overwrite existing work without permission.
- Save typed results (a worker's output for a board card) in `handoffs/`.
- Keep worker profiles in `agents/` and reusable procedures in `skills/`.
- Claude is the Orchestrator and an Executor; Codex is an independent reviewer and an Executor. An agent never reviews its own execution output.
- Log meaningful work with `vurctos remember` (do not hand-edit `MEMORY.md`).

## Safety Rules

- Do not store secrets or account sessions here.
- Do not upload files externally unless the user asks.
- Prefer subscription login over API keys unless instructed otherwise.
- Keep steps explicit.

## Done Criteria

A task is done when:

- the requested change or output exists and is where it belongs
- project memory is updated via the `vurctos remember` CLI if useful
- unclear assumptions are documented
- files remain easy for another agent or human to inspect
