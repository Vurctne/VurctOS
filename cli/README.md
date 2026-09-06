# VurctOS CLI

A small, local-first CLI that handles the deterministic plumbing of a VurctOS project. The judgment (planning, review) stays with Claude acting as Orchestrator; this CLI only does the repeatable mechanical steps.

## Requirements

- Python 3.8+

No API keys. No dependencies.

## Commands

### Scaffold a project

```bash
python3 cli/vurctos.py new my-project
```

Copies `templates/project-template/` into `my-project/` (board, agent profiles, memory, skills, and the folder layout).

### Remember something

```bash
python3 cli/vurctos.py remember --project my-project \
  --what "warm key light reads as premium for this brand" \
  --kind style --evidence "shot 3 accepted, shot 5 rejected"
```

Files one memory entry into session recall:

- an entry in the day log `sessions/<date>.md`
- a row in `sessions/index.db`, the local SQLite full-text index

Durable memory (`USER.md`, `MEMORY.md`) is not touched: only an approved `reflect-apply` writes there, so every durable line has been consolidated on purpose. `remember` ends with a one-line status of what is waiting to be reflected.

`--kind` is one of `decision`, `style`, `tool`, `fail`, or `note` (default `note`). `--evidence` is optional. `--date YYYY-MM-DD` overrides today. The CLI only files what you give it; deciding what is worth remembering stays with Claude as Orchestrator.

### Recall past memory

```bash
python3 cli/vurctos.py recall --project my-project "premium lighting"
```

Full-text searches past entries and prints the matches with their dates and day-log paths. It uses SQLite FTS5 when available and falls back to a substring search otherwise, so it works on any standard Python install. FTS5 query operators work in FTS mode; an invalid FTS query is retried as a plain AND of its words.

```bash
python3 cli/vurctos.py recall --project my-project --stats "reference image"
python3 cli/vurctos.py recall --project my-project --stats
```

`--stats` turns recall into the promotion counter. With a query it reports how many matches there are across how many distinct dates; a lesson repeated on 3+ dates is flagged as a promotion candidate (a skill via `skill-new`, or user-level memory via `remember --global`), which turns the "promote only what repeats" rule from a hunch into a number. Without a query it prints per-kind totals and the fail/decision entries from the last 30 days, so capture coverage can be eyeballed.

### Rebuild the search index

```bash
python3 cli/vurctos.py reindex --project my-project
```

The markdown day-logs are the source of truth; `sessions/index.db` is derived, machine-local, and gitignored. Use `reindex` when the index is absent on a fresh machine or clone, after index corruption, or after hand-editing a day-log. `reindex` deletes the index and rebuilds it from the day-logs (also with `--global` for `~/.vurctos`).

### Reflect (distill sessions into durable memory)

```bash
python3 cli/vurctos.py reflect --project my-project
# the Orchestrator fills reflections/<date>.md, a human sets status: approved
python3 cli/vurctos.py reflect-apply --project my-project
```

`reflect` collects the session day logs not yet reflected (since the last `reflections/.last-reflected` marker, or `--since DATE`) and stages a proposal at `reflections/<date>.md` with empty sections: additions to `USER.md`, additions to `MEMORY.md`, exact lines to prune, and skill candidates. Claude as Orchestrator distills the logs into that proposal (it does not copy them raw); a human reviews, edits, and sets `status: approved`.

`reflect-apply` refuses unless the proposal is approved, and validates the whole proposal before writing anything: every prune target must match exactly one line in `USER.md` / `MEMORY.md`, and an empty proposal must carry a Rationale. Then it appends the additions under a dated `## Reflected Updates` block, removes the prune lines, and advances the cursor (`reflections/.last-reflected`: the last reflected date plus how many of that day's entries were consumed, so entries filed later the same day are still picked up next time). The proposal records the cursor it was staged against (`cursor-before`) and the one it advances to (`cursor-after`), so an apply after the cursor moved is refused, and entries filed while the proposal was being written stay unreflected. Only one proposal can be pending at a time; an applied proposal is kept as the record, so a second one on the same date is staged as `<date>-2.md`. This is the loop that lets memory get sharper over time instead of just longer: distill, prune, and keep a human in the loop so a wrong distilled fact never silently poisons later sessions.

### Memory status (the backlog dashboard)

```bash
python3 cli/vurctos.py memory-status --project my-project
python3 cli/vurctos.py memory-status --global
```

Prints the reflect backlog (unreflected entries and day-logs, the oldest date), the cursor, any staged reflection not yet applied, and the size of `USER.md` / `MEMORY.md`. Once the backlog reaches 30 entries (`--stage-at N`, 0 disables) and no draft is waiting, it stages an empty `reflections/<today>.md` so there is a concrete file to fill instead of a command to remember. `--hook` prints the same facts as a Claude Code `SessionStart` payload; both shipped nudge hooks call it.

### Global memory (cross-project)

All memory commands accept `--global` to target the user-level memory at `~/.vurctos/` (override with `VURCTOS_HOME`) instead of a project:

```bash
python3 cli/vurctos.py remember --global --what "prefers warm, premium lighting" --kind style
python3 cli/vurctos.py recall --global "premium"
python3 cli/vurctos.py reflect --global
```

The global root is created and seeded on first use. It mirrors the project layout (`USER.md`, `MEMORY.md`, `sessions/` + index, `reflections/`) but holds what is true of the user across ALL projects: decision patterns, taste, recurring corrections. Add `@~/.vurctos/USER.md` to your `~/.claude/CLAUDE.md` and the distilled global memory loads in every Claude Code session, in every project. Promote a lesson from a project into global memory only when it is repeated, accepted, and useful beyond that project (see docs/memory-system.md).

Two `SessionStart` hooks drive the reflect loop instead of leaving it to be remembered: the project template ships one (`vurctos new` bakes in the CLI path), and `templates/global-hook/vurctos-global-nudge.sh` is the user-level one for `~/.vurctos` (install steps in `docs/memory-system.md`). Both call `memory-status --hook`.

### Dispatch (run one board card via headless Claude or Codex)

```bash
python3 cli/vurctos.py dispatch --project my-project --dry-run
python3 cli/vurctos.py dispatch --project my-project
python3 cli/vurctos.py dispatch --project my-project --agent codex
```

Picks the first card in `BOARD.md` with `status: ready` and `channel: local`, claims it as `in-progress`, and runs a headless agent inside the project (so `CLAUDE.md` / `AGENTS.md`, `USER.md`, `MEMORY.md`, and skills load themselves via your existing subscription login). A card whose handoff or expected output resolves outside the project is refused before anything runs.

Afterwards the typed handoff and every expected output must exist as a regular file inside the project **and hold different content than before the run**, compared by hash. Existence alone is spoofable: a file already lying in the project would let a card pass with the agent doing nothing. Content is compared rather than modification time because mtime does not answer the question (a coarse filesystem can put two writes in one tick, a restored timestamp looks unwritten, and `touch` looks written). Containment is re-checked after the run too, so an output cannot be swapped for a symlink pointing outside. The strictness is deliberate in one direction: a run that rewrote a file with byte-identical content is indistinguishable from one that did nothing, so it is blocked for you to look at. A false block costs one review; a false pass would bank unverified work.

On success the card moves to `review` (never `done`; a human or Codex reviews). A run that comes back failed or timed out settles the card to `blocked`, with the agent's own error (truncated) plus the full list of outputs it did not produce filed into memory as the reason; a usage-limit hit blocks it with `usage limit reached` instead, since the run never got to fail on its merits. A card refused before it ever ran (an output path outside the project) is blocked too.

Once a card has been claimed, only a run that comes back settles it: if the dispatcher itself is killed or crashes, the card stays `in-progress` on purpose (see below). If the card's status changed while the run was in flight, dispatch reports that and leaves your edit alone, with one deliberate exception: a card cannot come out of its own dispatch marked `done`, and a failed run cannot come out marked `review`. The prompt tells an executor not to touch `BOARD.md`, but a card runs with edits accepted, so the board enforces it rather than trusting it. Only a human moves work past review.

**If a card is stuck at `in-progress`**, its dispatcher was killed or crashed (the status is deliberately left behind as evidence, and `dispatch` will not pick it up again, since it only picks `ready`). To recover: confirm no agent process is still running, inspect whatever partial files the run left, then set the card back to `status: ready` to retry it, or to `blocked` to park it. `reject` is for cards in `review`, not for this.

`--agent` picks the executor (default `claude`, runs `claude -p`; `codex` runs `codex exec` with a `workspace-write` sandbox on your ChatGPT login). Codex loads the same VurctOS memory through the `AGENTS.md` bridge, so either executor starts with your durable context. Each executor spends its own separate subscription quota.

Boundaries by construction: cards with `channel: handoff` (subscription web tools) are never touched; the child environment strips both providers' billing routes (`ANTHROPIC_API_KEY` and auth token, base-URL and Bedrock/Vertex routes; `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`) and `--bare` is never used, so a run cannot silently switch to metered API billing. Requires the chosen CLI installed and logged in. `--timeout` caps a run (default 600s).

**Trust model (read before dispatching):** dispatch feeds the card's own fields (title, notes, inputs, outputs) into a headless `claude -p` run with edits accepted. That is prompt-injection by design: dispatching a board executes the instructions its cards contain. So only dispatch a project whose cards **you authored or reviewed**; never run `dispatch` on a board you cloned or pulled from someone else without reading its cards first. The bounds above limit billing and which cards run, not what an authored instruction can do.

To verify dispatch end to end against a real logged-in CLI once (the unit tests mock the subprocess), run `sh scripts/smoke-dispatch.sh` (add `--agent codex` for the Codex path); it costs one short headless run and checks the card lands in `review` with the exact expected output. Add `--negative` to check that a card whose expected output escapes the project is refused and lands in `blocked`.

### Delegate to Codex inside a live session (subagent)

`dispatch --agent codex` is the asynchronous, board-driven path. For synchronous delegation inside a live Claude Code conversation, `templates/codex-subagent/codex.md` is a Claude Code subagent whose only job is to run a task through `codex exec` and relay the result. Copy it to `~/.claude/agents/codex.md` (available everywhere) or a project's `.claude/agents/codex.md`. Then Claude can hand Codex a task or an independent review (Codex is the reviewer role: the planner should not grade its own work) without leaving the session. It defaults to a `read-only` sandbox for reviews. Note that Claude Code's native subagents run on Claude models; this one is a thin Claude wrapper that shells out to the Codex CLI, since a subagent cannot itself be backed by a non-Claude model. Each Codex run spends separate ChatGPT/Codex quota.

### Reject reviewed work (the verdict-to-memory wire)

```bash
python3 cli/vurctos.py reject card-001 --project my-project --reason "pacing too slow in shot 3"
```

When a `review` card fails your review, one command closes the learning loop: the reason is filed as a `fail` entry into the day log and the index (it reaches `MEMORY.md` through the next approved reflection), stamped into the card's `notes:` (so the re-run prompt carries the feedback explicitly), and the card flips back to `ready` for the next dispatch. Rejection reasons are the highest-value learning signal; never let one evaporate.

### Promote a proven pattern to a skill

```bash
python3 cli/vurctos.py skill-new premium-lighting-pass --project my-project
```

Scaffolds `skills/<name>/SKILL.md` in the agentskills.io format (frontmatter plus When To Use / Inputs / Steps / Agent Roles / Outputs / Review Criteria / Memory Updates). The name must be lowercase letters, numbers, and single hyphens, and must match the folder. The CLI only builds the skeleton: the Orchestrator fills it from the proven pattern (reflect proposals list the candidates), and a human reviews it before first use. Promotion rule: only a repeated, useful pattern becomes a skill; a single prompt is not a skill.

## Why this is the whole point

The CLI does only what is mechanical and repeatable. Everything that needs judgment is done by Claude reading the local files. This keeps the system local-first, subscription-first, and free of platform-terms risk.
