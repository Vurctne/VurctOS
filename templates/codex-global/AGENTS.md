# Codex global instructions (VurctOS memory bridge)

This file gives an OpenAI Codex session the same user-level durable memory that a Claude Code session gets from the `@~/.vurctos/USER.md` import in `~/.claude/CLAUDE.md`.

Codex reads exactly one file per level at global scope, `AGENTS.override.md` first then `AGENTS.md`, from the Codex home directory (`~/.codex` by default, or `$CODEX_HOME` if set). To install:

- copy this file to `~/.codex/AGENTS.md` if you have none there, or
- append the section below to your existing `~/.codex/AGENTS.md` (or `~/.codex/AGENTS.override.md`).

Do not blind-overwrite an existing `~/.codex/AGENTS.md`: append instead, so your other Codex preferences survive. Some other tools also keep a `~/.codex` directory for unrelated state, so confirm this is your OpenAI Codex home before writing, and set `CODEX_HOME` to disambiguate if needed. To check what Codex actually loaded, start it and ask it to list the instruction sources it loaded.

---

## VurctOS global durable memory (read before acting)

User-level durable memory lives in two plain files:

- `~/.vurctos/USER.md`: durable, cross-project facts and decision patterns about the user that nearly every session needs.
- `~/.vurctos/MEMORY.md`: consolidated cross-project knowledge for particular kinds of task (tools, platforms, methods), one fact per line under a `[tag]`.

At the start of each session, open `USER.md` (if it exists) and treat its contents as known context for every project, the same file a Claude Code session imports. Do not load `MEMORY.md` up front: when a task touches one of its topics, search it for the matching `[tag]` lines and read those. If the task's own instructions say not to read personal memory, follow them. The files are named here in prose because Codex does not expand `@file` imports.

Do not write to these files directly. Global memory is filed and consolidated only through the human-gated `vurctos remember --global` / `vurctos reflect --global` CLI.
