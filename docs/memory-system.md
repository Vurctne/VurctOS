# Memory System

VurctOS memory is not only for remembering facts.

The deeper goal is learning how the user works and makes decisions.

## Three Memory Layers

VurctOS memory is organized in three layers. This is a local adoption of the open-source Hermes Agent memory design: VurctOS mirrors its layout and file names so memory stays migration-compatible, but it does not install or depend on the Hermes runtime. Every layer is an inspectable file.

1. Durable memory: stable facts, preferences, style, and decisions. Files: `USER.md` and `MEMORY.md`. `USER.md` holds the slow-changing user, brand, and style preferences and decision patterns. `MEMORY.md` holds project facts, decisions, and what worked or failed, where project, workflow, style, and decision memory are sections.
2. Procedural memory: how to do things. This is the skills layer in the SKILL.md standard; see `docs/skills-system.md`.
3. Session recall: what happened before. Dated day logs in `sessions/`, plus a local SQLite full-text index at `sessions/index.db` for search.

This keeps memory readable, editable, portable, and compatible with coding agents.

## USER.md

`USER.md` stores stable context and how the user works.

Examples:

- who the user is and how they like to work
- preferred languages, frameworks, and tools
- code and writing style preferences
- recurring constraints and conventions
- deployment or delivery targets
- decision patterns (what the user consistently values, rejects, and trades off)

This file should change slowly. It is the renamed, Hermes-compatible successor to the older `PROFILE.md`; a short `PROFILE.md` pointer remains for back-compat.

## MEMORY.md

`MEMORY.md` stores project learning.

Examples:

- approaches that worked
- approaches that failed
- tool limitations
- conventions to follow
- design and implementation decisions
- repeated patterns worth turning into skills

This file should change as work happens.

## Project Memory

Project memory captures facts and constraints for one project. In the project template this appears as the "Stable Project Facts" section of `MEMORY.md`:

- project goal
- target platform
- key files, modules, and dependencies
- conventions and style direction
- naming rules
- output locations

## Workflow Memory

Workflow memory captures what happened during a repeatable process:

- steps used
- tools used
- manual corrections
- bottlenecks
- useful prompts
- failed assumptions
- automation candidates

## Style Memory

Style memory captures taste, the preferences that are hard to state up front:

- preferred patterns and structure
- naming and formatting preferences
- what good work looks like here
- acceptable and unacceptable trade-offs
- what to avoid

Style memory is important because users judge work by standards that are easier to show than to specify.

## Decision Memory

Decision memory captures why choices were made.

Examples:

- why an approach was selected
- why an option was rejected
- why one solution worked better than another
- why a tool was chosen for a step
- what the user consistently values

This is where VurctOS can become more useful over time. It should learn decision patterns, not only store notes.

## Session Recall

Session recall is the third memory layer. Working notes are logged as dated day files in `sessions/`, for example `sessions/2026-06-28.md`, capturing what was learned, decided, accepted, or rejected. This is the learning summary made durable, and it is what the Orchestrator can search when a project comes back later.

Each entry is also indexed into `sessions/index.db`, a local SQLite full-text index, so recall is fast across many sessions. The index uses FTS5 where the local SQLite supports it, and falls back to a plain substring search otherwise, so search works on any standard Python install. The `index.db` file is generated machine-local instance data and is not committed.

The CLI files these for you: `vurctos remember` appends an entry to the day log and indexes it (durable memory is untouched until an approved `reflect-apply`), and `vurctos recall "<query>"` searches the index. `recall --stats` counts how often a lesson repeats across distinct dates (the promotion signal) and, without a query, summarizes capture coverage per kind. Because the day-logs are the source of truth and the index is derived and gitignored, `vurctos reindex` rebuilds it from the day-logs when it is absent on a fresh machine or clone, after index corruption, or after hand-editing a day-log. See `cli/README.md`.

## Obsidian Compatibility

Because every memory file is plain markdown in a local folder, a project folder doubles as an Obsidian vault. Opening it in Obsidian gives a human graph view, backlinks, and manual search over `USER.md`, `MEMORY.md`, `skills/`, and `sessions/`, with no lock-in and no dependency: VurctOS never requires Obsidian, and the CLI never writes anything Obsidian-specific.

Obsidian is a human lens, not a substitute for `sessions/index.db`. The SQLite index serves programmatic recall by the Orchestrator and CLI; Obsidian serves human browsing. Frontmatter and `[[wikilinks]]` between notes are welcome since they stay valid plain markdown. Keep `.obsidian/` (its per-machine config) out of version control; the repository `.gitignore` already excludes it.

## User-Level Global Memory

Project memory answers "what is true of this project"; the user-level layer answers "what is true of the user everywhere". It lives at `~/.vurctos/` (outside any repository, private by construction) and mirrors the project layout: `USER.md` for durable cross-project facts and decision patterns, `MEMORY.md` plus dated `sessions/` day-logs and their own full-text index, and `reflections/` with the same human-approved consolidation flow. All memory commands take `--global` to target it.

Two wires make it matter. First, `@~/.vurctos/USER.md` imported from `~/.claude/CLAUDE.md` loads the distilled global memory into every Claude Code session in every project, which is what makes a correction learned once hold everywhere. Second, the promotion rule: a lesson starts project-local and is promoted into global memory only when it is repeated across projects, explicitly accepted, and stable (the same rule as skill promotion). Corrections from the user are the highest-value signal and should be filed the moment they happen, then consolidated by `reflect --global`.

### Loading the same durable memory into Codex

Claude Code auto-loads durable memory through `@`-imports (`@USER.md` and `@MEMORY.md` in a project's `CLAUDE.md`; `@~/.vurctos/USER.md` in `~/.claude/CLAUDE.md`). Codex does not expand `@`-imports, so VurctOS wires the same files into Codex by prose instruction in the file Codex already auto-reads:

- Project memory: a scaffolded project's `AGENTS.md` (which Codex reads by walking from the git root down to the current directory, before any work) names `USER.md` and `MEMORY.md` and tells Codex to open them.
- Global memory: `templates/codex-global/AGENTS.md` is a snippet you install at `~/.codex/AGENTS.md` (append, do not overwrite; respect `CODEX_HOME`, and confirm that directory is really your OpenAI Codex home first). Codex reads it once per session and it points at `~/.vurctos/USER.md` and `~/.vurctos/MEMORY.md`.

Both are read-only bridges: Codex is told to read durable memory, never to write it. Filing and consolidation stay in the human-gated `vurctos remember` / `vurctos reflect` CLI. The `SessionStart` reflect-nudge is a Claude Code hook; Codex documents a comparable hook mechanism, but replicating the nudge there is a separate hook change, not part of this file wiring.

### The session-start nudge

What keeps the loop running is a Claude Code `SessionStart` hook that runs `vurctos memory-status --hook`: it reports how many entries are waiting, the freshest three, and any staged reflection not yet applied, and once the backlog reaches 30 entries it stages an empty `reflections/<today>.md` so the nudge points at a file to fill rather than a command to remember. Scaffolded projects get the hook from the template (`vurctos new` bakes in the CLI path). For the global layer, install the shipped copy once from your checkout, then wire it into `~/.claude/settings.json`:

```bash
mkdir -p ~/.claude/hooks && sed "s|__VURCTOS_CLI__|'$PWD/cli/vurctos.py'|" templates/global-hook/vurctos-global-nudge.sh > ~/.claude/hooks/vurctos-global-nudge.sh
```

(If your checkout path contains a single quote, `|` or `&`, copy the file and edit its `cli=` line by hand instead.) Then add to `hooks.SessionStart` in `~/.claude/settings.json`: `{"matcher": "", "hooks": [{"type": "command", "command": "sh \"$HOME/.claude/hooks/vurctos-global-nudge.sh\"", "timeout": 10}]}`. The hook is read-only apart from staging that empty draft; it never approves or applies anything.

## Reflection And Consolidation

Memory that only grows gets slower and noisier, not smarter. The step that makes memory improve over time is reflection: periodically distilling the raw session day logs into durable memory and pruning what is stale, rather than appending forever. This is the empirically load-bearing mechanism in the agent-memory literature, and an independently built production agent (Nous Research's Hermes Agent) converges on the same USER.md + MEMORY.md + skills file layering and a distillation loop.

In VurctOS this runs as `vurctos reflect` (see `cli/README.md`): the CLI gathers the unreflected day logs and stages a proposal; Claude as Orchestrator distills them into proposed `USER.md` and `MEMORY.md` updates, prunes, and skill candidates; a human approves before anything is written to durable memory. Keep both the concrete dated logs and the distilled abstractions. The human approval gate is deliberate: a single wrong distilled fact, written unchecked into `USER.md`, would quietly affect every later session.

Two mechanics enforce that gate. `remember` writes only the day log and the index, so nothing reaches `USER.md` or `MEMORY.md` except through an approved reflection. `reflect-apply` fails closed: if any prune target is missing or ambiguous, if the proposal is empty with no rationale, or if the cursor has moved since the proposal was staged, nothing is written and the cursor stays put. The cursor (`reflections/.last-reflected`) records the last reflected day and how many of that day's entries were consumed; a proposal records the cursor it was staged against and the cursor it advances to (`cursor-before` / `cursor-after`), so entries filed after staging, including later the same day, are still picked up by the next reflect. Only one proposal can be pending at a time, and an applied proposal is never overwritten (a second one on the same date becomes `<date>-2.md`). `vurctos memory-status` shows the backlog, the cursor, any waiting draft, and the size of the durable files.

Upgrading from an older CLI: `remember` used to copy every entry into `MEMORY.md` as well, as `- <date> [kind] ... -> sessions/<date>.md` lines (first under a `## Session Updates` heading, later wherever the file ended). Each completed capture wrote the same entry to its day log, so those lines are raw duplicates of what reflect reads. `memory-status` counts them, verifies each against its day log, and lists any it cannot find there (the old CLI wrote `MEMORY.md` first, so an interrupted capture can exist only there): move an unmatched line into a proper section or its day log, then delete the verified duplicates by hand along with the empty heading. The old bare-date cursor is read as "that day not yet consumed": the next reflect re-reads the cursor day, so entries the older CLI filed after that apply are not lost, and anything already distilled from that day is simply skipped in the proposal.

Two write-time disciplines keep the durable layer from bloating (adopted from the Hermes Agent memory design, which uses hard size budgets plus consolidate-on-write rather than any automatic decay): keep transient or soon-stale content out of durable memory entirely (rankings and market data, environment-dependent failures, negative tool claims, transient errors that already resolved, one-off task narratives; the day-log keeps them as dated history), and supersede instead of append (a proposal that updates an existing durable fact prunes the old line in the same apply, so revisions never stack). The staged proposal template restates both. A third discipline is the size budget: each durable file is expected to stay within 60 non-empty lines or 6 KiB, `memory-status` flags a file over it, and `vurctos aging` (read-only) lists the oldest reflected blocks as prune candidates by date for the next reflect, so retirement rides the same human-approved apply.

Full-text recall (the SQLite FTS5 index) is sufficient for a single-user local system; vector embeddings are not required and are deferred unless recall is shown to miss relevant memories at scale.

## Memory Update Rule

At the end of a workflow, update memory with:

- what worked
- what failed
- what changed
- what should be reused
- what should become a skill

Do not hide memory. Keep it inspectable.
