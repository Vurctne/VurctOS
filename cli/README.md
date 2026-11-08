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

Durable memory (`USER.md`, `MEMORY.md`) is not touched: only an approved `reflect-apply` writes there, so every durable line has been consolidated on purpose. `remember` prints the entry's id (its day plus its position in that day's log, such as `2026-06-30#2`; day logs are append-only, so ids never move) and ends with a one-line status of what is waiting to be reflected. It appends under a file lock, so two sessions filing at once both land. If the index cannot be updated, the entry is still filed and the output says to run `reindex`.

`--kind` is one of `decision`, `style`, `tool`, `fail`, `note`, `correction`, or `fact`; without it, a leading `[kind]` tag in the text becomes the kind, and anything else is a `note`. `--evidence` is optional. `--supersedes <id>` (repeatable) marks an earlier entry this one replaces, `--scope global|project:<name>` says where the lesson applies, and `--landed "<where>"` says it is already written into a skill, a rule file or a project record; each is stored as a line under the entry and shows up in the next reflect's digest. Text that looks like a phone, ID-card or card number is refused (personal form data belongs in a private profile, not in day logs); `--allow-pii` files a number that only looks personal. `--date YYYY-MM-DD` overrides today. The CLI only files what you give it; deciding what is worth remembering stays with Claude as Orchestrator.

```bash
python3 cli/vurctos.py remember --global \
  --what "setup skipped the review checklist" \
  --skill vibe-coding-setup --signal correction --rule "steps/4" \
  --evidence "review of the new repo, missing checklist file"
python3 cli/vurctos.py remember --global --what "that lesson was wrong" \
  --signal retract --supersedes 2026-06-30#2
```

Skill feedback: `--skill NAME` (repeatable) names the skill folder, or a target registered in the private `<memory root>/skill-loop.json`, the lesson is about, and needs `--signal` (`correction`, `reject`, `pick`, `review`, `check`, `deviation`, `data`; `retract` / `decline` withdraw the entry given by `--supersedes` and may name no skill). `--outcome fail|held|mixed` is fixed for correction, reject, deviation (`fail`) and pick (`mixed`), defaults to `fail` for check, and is required for review (`fail` or `held`, and `held` needs `--rule`) and data. `--rule REF` (repeatable; `NAME:REF` when more than one `--skill`) and `--model TEXT` are optional single lines. An unknown name is refused with close matches; the entry also gets a `skillver` line, the hash of each skill's files (left out with a note when it cannot be computed). `skill-loop.json` is optional: `skills_dirs` (default `~/.claude/skills` for `--global`, `<project>/skills` for a project), `targets` (name to file or directory) and `history_globs`.

### Recall past memory

```bash
python3 cli/vurctos.py recall --project my-project "premium lighting"
```

Full-text searches past entries and prints the matches with their dates and day-log paths. Read-only: it never creates or seeds anything. It uses SQLite FTS5 when available and falls back to a substring search otherwise, so it works on any standard Python install. FTS5 query operators work in FTS mode; an invalid FTS query is retried as a plain AND of its words.

```bash
python3 cli/vurctos.py recall --project my-project --stats "reference image"
python3 cli/vurctos.py recall --project my-project --stats
```

`--stats` turns recall into the promotion counter. With a query it reports how many matches there are across how many distinct dates; a lesson repeated on 3+ dates is flagged as a promotion candidate (a skill via `skill-new`, or user-level memory via `remember --global`), which turns the "promote only what repeats" rule from a hunch into a number. Without a query it prints per-kind totals and the fail/decision entries from the last 30 days, so capture coverage can be eyeballed.

### Rebuild the search index

```bash
python3 cli/vurctos.py reindex --project my-project
```

The markdown day-logs are the source of truth; `sessions/index.db` is derived, machine-local, and gitignored. Use `reindex` when the index is absent on a fresh machine or clone, after index corruption, or after hand-editing a day-log. `reindex` deletes the index and rebuilds it from the day-logs (also with `--global` for `~/.vurctos`). It refuses when a hand edit removed entries the reflect cursor already counted, since entry ids are positions and would shift.

### Reflect (distill sessions into durable memory)

```bash
python3 cli/vurctos.py reflect --project my-project
# the Orchestrator fills reflections/<date>.md
python3 cli/vurctos.py reflect-apply --project my-project --check
# a human approves the printed result; set status: approved, then
python3 cli/vurctos.py reflect-apply --project my-project --approved-hash <hash>
```

`reflect` collects the session day logs not yet reflected (since the last `reflections/.last-reflected` marker, or `--since DATE`) and stages a proposal at `reflections/<date>.md`. It opens with a digest the CLI computed (entry counts, which entries supersede which, entries already written into a carrier, project-scoped entries, the durable sizes against their budgets), which is never applied, followed by empty sections: additions to `USER.md`, additions to `MEMORY.md`, exact lines to prune, skill candidates, and a rationale. Claude as Orchestrator distills the logs into that proposal (it does not copy them raw). Only one proposal can be pending at a time, but a pending draft nobody has written into does not block: `reflect` moves it to `reflections/archive/` and stages a fresh one.

`reflect-apply --check` validates the whole proposal and computes the exact result without writing anything: every prune target must match exactly one line in `USER.md` / `MEMORY.md`, an empty proposal must carry a Rationale, each added line must be at most 512 bytes (one fact per line), and the resulting files must stay within their size budgets. It prints both files' sizes after the apply, the lines pruned, where the cursor would move, and an approval hash over the proposal (minus its status line) and the current durable files. A human reviews that; then `reflect-apply --approved-hash <hash>` repeats every check, refuses unless the status is `approved` and the hash still matches (any later edit to the proposal or to durable memory means checking again), copies both files to `reflections/backup/<proposal>/`, replaces each atomically (pruning first, then appending the additions under a dated `## Reflected Updates` block and dropping dated headings left empty), and advances the cursor last (`reflections/.last-reflected`: the last reflected date plus how many of that day's entries were consumed, so entries filed later the same day are still picked up next time). A crash can leave one file new and the other old, never half a file; the leftover backup folder marks that case, and the next apply refuses until the files are copied back from it and the folder is removed. The proposal records the cursor it was staged against (`cursor-before`) and the one it advances to (`cursor-after`), so an apply after the cursor moved is refused, and entries filed while the proposal was being written stay unreflected. An applied proposal is kept as the record, so a second one on the same date is staged as `<date>-2.md`. This is the loop that lets memory get sharper over time instead of just longer: distill, prune, and keep a human in the loop so a wrong distilled fact never silently poisons later sessions.

### Memory status (the backlog dashboard)

```bash
python3 cli/vurctos.py memory-status --project my-project
python3 cli/vurctos.py memory-status --global
```

Prints the reflect backlog (unreflected entries and day-logs, the oldest date), the cursor, any staged reflection not yet applied, and the size of `USER.md` / `MEMORY.md`. Read-only: it never stages a draft and never creates or seeds anything. `--hook` prints the same facts as a Claude Code `SessionStart` payload, numbers only (unreviewed entry text never enters a session); both shipped nudge hooks call it. The backlog part speaks once there are 60 waiting entries or the oldest is 7 days old, and asks the assistant to offer a short consolidation at a natural pause; a waiting draft, or a durable file over its size budget (USER.md: 60 non-empty lines or 6 KiB; MEMORY.md: 60 non-empty lines or 24 KiB, since it holds dense tool and method facts), always shows, and an over-budget file points at `aging`.

### Skill status (feedback no revision has answered)

```bash
python3 cli/vurctos.py skill-status --global
python3 cli/vurctos.py skill-status --project my-project --skill vibe-coding-setup --json
python3 cli/vurctos.py skill-status --global --hook
```

Read-only (it never creates or seeds anything, not even the index or the global root). For every skill or target named by a feedback entry, or only the one given by `--skill` even when it has none, it lists the pending entries: those no `fb:<id>` citation in the skill's `.md` files answers and no later `retract` / `decline` withdraws. It also shows entries marked `landed` but not cited, cited entries a later retract withdrew, entries filed after an `fb-retired` line that name the retired rule, whether each entry was filed against the current `skillver`, character counts of `SKILL.md`, the rule files and the history files, and a score (3 per pending correction or reject, 1 per other pending failure, 3 per retracted-but-cited entry, 2 per entry naming a retired rule). `--json` prints the same data; `--hook` prints one line of numbers only and always exits 0. See `docs/skills-system.md`.

### Skill snapshot and skill check (static check of a skill edit)

```bash
# after editing a skill in place: compare live with the last accepted copy
python3 cli/vurctos.py skill-snapshot vibe-coding-setup --global --accept   # once, to bootstrap
python3 cli/vurctos.py skill-check vibe-coding-setup --global --since-accepted [--accept] [--json]

# or edit a copy: snapshot a work pair, edit W/cand/, then check it
python3 cli/vurctos.py skill-snapshot vibe-coding-setup --global --workdir /tmp/w1
python3 cli/vurctos.py skill-check vibe-coding-setup --global --workdir /tmp/w1 --json
```

No model, seconds. `skill-snapshot --workdir W` (W must not exist or be empty) writes two identical copies of the skill's tree, `W/orig/` and `W/cand/`, plus `W/manifest.json`; `--accept` stores the live tree as the accepted baseline under `<evals_dir>/_snapshots/<name>/<tree hash>/`, points `<evals_dir>/_snapshots/<name>/accepted` at it and prints the hash, running no checks (the bootstrap step, and the owner's explicit override). A copy holds the files the `skillver` hash covers; a nested skill becomes a symlink to its live directory.

`skill-check` compares a candidate with its base: `--workdir W` checks `W/cand` against `W/orig` and advances nothing; `--since-accepted` checks a fresh copy of live against the accepted copy (exit 0 at once when nothing changed), working under `<work_dir>/checks/<name>/<UTC stamp>/`. It reports, grouped as protected, touch, volume, lint, coverage and claims:

- **fail** (exit 1): a lint line the candidate has and the base has not; a lint command that cannot start or times out; a curated anchor missing (no rule line still contains it); a file newly over its character budget; an `fb:<id>` newly cited that is no feedback entry for this skill.
- **hold** (exit 3): a protected line removed, changed or added; a protected file changed; a removed or added line containing a category word. The owner decides these.
- **pass** (exit 0) otherwise, with info lines: net growth of rule and history files, rule files no budget covers, an overrun the base already had and the candidate did not worsen, the evidence (`landed` or `MISSING`) for each new citation.

With `--since-accepted`, a pass advances `accepted` to the candidate, a hold advances only with `--accept`, and a fail never does. `--json` prints `{schema_version, skill, level, base_tree, cand_tree, advanced, findings}`, also written to `check.json`. Errors before the check exit 1, usage errors 2. The command never changes the live tree, the day logs or the index.

Configured in the private `skill-loop.json`: `evals_dir` (default `<memory root>/skill-evals`), `work_dir` (default `<memory root>/skill-work`), `mirror_home` (default `~`), `protected_line_patterns` and `protected_category_words` (lists), `protected_files` (name to relpaths), `lint_cmds` (argv lists, run with `HOME` set to a mirror of `mirror_home`), `lint_timeout_s` (default 300) and `budget_file` (JSON: path relative to `mirror_home` to a character budget). Curated anchors (the words other files point at, such as a heading) go in `<evals_dir>/<name>/_protected.txt`, one per line (`//` lines are comments; a leading `#` is part of the anchor, so a Markdown heading works); each must still appear inside some rule line. See `docs/skills-system.md`.

### Skill eval and skill pick (run old and new on the same cases)

```bash
python3 cli/vurctos.py skill-eval vibe-coding-setup --global --workdir /tmp/w1 --list-cases
python3 cli/vurctos.py skill-eval vibe-coding-setup --global --workdir /tmp/w1 --probe        # A/A on W/orig
python3 cli/vurctos.py skill-eval vibe-coding-setup --global --workdir /tmp/w1 [--case C ...] [--runs K] [--max-runs N] [--json]
python3 cli/vurctos.py skill-pick make vibe-coding-setup --global --workdir /tmp/w1 [--batch B]
python3 cli/vurctos.py skill-pick record vibe-coding-setup --global --workdir /tmp/w1 --answers "1:A,2:same,3:B"
```

`skill-eval` runs headless Claude Code (on the subscription login, never an API key) once per case per arm per run, for the base `W/orig` and the candidate `W/cand` of a `skill-snapshot --workdir` pair, and compares them grader by grader. It costs real model runs: the planned count (cases x 2 arms x runs) is refused up front when it exceeds `--max-runs` (default `eval_max_runs`), before anything runs. Cases live in `<evals_dir>/<skill>/<case>/` (`prompt.md`, `graders/*.md`, optional `files/`) with a sidecar `<evals_dir>/<skill>/_sidecars/<case>.json`; pinned judge scripts in `<evals_dir>/_judges/<sha256>/`. `--list-cases` runs nothing and prints each case's status, hash, whether its approval is current, its graders and checks, and load errors. `--probe` runs `W/orig` in both arms (finds unstable graders, and shows a targeted case fails on the base). `--agent-cmd PATH` replaces the `claude` executable (the tests use `cli/eval_stub.py`).

Each run gets a fresh mirror of the home in which the skill resolves to the arm under test, an allowlist environment, shims for blocked binaries, no MCP servers and writes confined to its own cwd; its transcript is audited (billing, init, which copy was loaded and with which body, contamination, side effects, result) and a run that fails the audit is invalid and retried once. Results go to `W/eval/<batch>/`: `runs/<case>/<arm>-<i>/` (the cwd as left, `transcript.jsonl`, `stderr.txt`, `shim.log`, `run.json`), `results.json`, `batch.json` and `verdict.json`. Exit 0 pass, 1 fail (an abort, a regression, too few valid runs, or a pick for the base; also errors before the batch), 3 hold (a person still has to pick). `--json` prints the verdict.

`skill-pick make` writes `W/eval/<batch>/pick.md`, a blind A/B page for the cases whose sidecar says `"judge": "human"` or `"mixed"`, and seals the key in `W/eval/<batch>/sealed/key.json`; `record` maps the answers (`A`, `B`, `same` or `both-bad`, every item once) through the key, writes `picks.json` and recomputes the verdict. `make` runs once per batch; `--batch` takes a batch name. Neither runs a model.

Configured in the private `skill-loop.json`: `eval_model` (required for a run: the full model id every run must report), `eval_effort` (`low` to `max`, or null), `eval_runs` (default 2), `eval_max_runs` (default 24) and `eval_blocked_bins` (default `codex, gh, curl, wget, ssh, scp, osascript, open`). See `docs/skills-system.md` for the case format, the audit and the verdict rules.

### Aging (retire old durable lines)

```bash
python3 cli/vurctos.py aging --project my-project --older-than 90
python3 cli/vurctos.py aging --global
```

Read-only (it never creates or seeds anything, not even the global root). Durable memory that only grows gets slower and noisier, so each durable file has a size budget (USER.md 60 non-empty lines or 6 KiB, MEMORY.md 60 non-empty lines or 24 KiB) that `reflect-apply` enforces; a file within budget is reported and left alone. For a file over budget, `aging` lists every line under a `### <date>` block that `reflect-apply` wrote more than `--older-than` days ago (default 90), verbatim and complete (continuation lines included, so a pasted prune never orphans a tail), as retirement candidates. When the file is over budget but no block is that old yet, it tells you the age of the oldest block and the `--older-than` value that would reach it, or that the weight is in hand-written sections above `## Reflected Updates`. Age is by reflection date, not by how often a line was used (a local adaptation, not the Hermes original). Nothing is deleted here: paste the lines to retire into the Prune section of the next `reflect` proposal and restate whatever still matters in its Add sections, so retirement goes through the same human-approved apply as everything else.

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

Boundaries by construction: cards with `channel: handoff` (subscription web tools) are never touched; the child environment strips both providers' billing routes (`ANTHROPIC_API_KEY` and auth token, base-URL and Bedrock/Vertex routes; `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`) and `--bare` is never used, so a run cannot silently switch to metered API billing. Requires the chosen CLI installed and logged in on the subscription. Before running anything, dispatch asks the CLI for free (`claude auth status`, `codex login status`) and refuses without touching the card when the answer is a clear no: logged out, or logged in by a route other than the subscription (an API key). The check is best-effort: an older CLI without that command, a timeout (30s), or wording this code does not recognize lets the run proceed, and the run plus its output verification stays the real gate. It runs inside the project with the same stripped environment as the run, so it answers for the run rather than for your shell: a Claude desktop-app session's login is not inherited by a headless child, so run `claude auth login` once in a terminal. `--timeout` caps a run (default 600s).

**Trust model (read before dispatching):** dispatch feeds the card's own fields (title, goal, notes, inputs, outputs, decided, acceptance) into a headless `claude -p` run with edits accepted. That is prompt-injection by design: dispatching a board executes the instructions its cards contain. So only dispatch a project whose cards **you authored or reviewed**; never run `dispatch` on a board you cloned or pulled from someone else without reading its cards first. The bounds above limit billing and which cards run, not what an authored instruction can do.

To verify dispatch end to end against a real logged-in CLI once (the unit tests mock the subprocess), run `sh scripts/smoke-dispatch.sh` (add `--agent codex` for the Codex path); it costs one short headless run and checks the card lands in `review` with the exact expected output. Add `--negative` to check that a card whose expected output escapes the project is refused and lands in `blocked`.

### Delegate to Codex inside a live session (subagent)

`dispatch --agent codex` is the asynchronous, board-driven path. For synchronous delegation inside a live Claude Code conversation, `templates/codex-subagent/codex.md` is a Claude Code subagent whose only job is to run a task through `codex exec` and relay the result. Copy it to `~/.claude/agents/codex.md` (available everywhere) or a project's `.claude/agents/codex.md`. Then Claude can hand Codex a task or an independent review (Codex is the reviewer role: the planner should not grade its own work) without leaving the session. It defaults to a `read-only` sandbox for reviews. Note that Claude Code's native subagents run on Claude models; this one is a thin Claude wrapper that shells out to the Codex CLI, since a subagent cannot itself be backed by a non-Claude model. Each Codex run spends separate ChatGPT/Codex quota.

### Reject reviewed work (the verdict-to-memory wire)

```bash
python3 cli/vurctos.py reject card-001 --project my-project --reason "pacing too slow in shot 3"
python3 cli/vurctos.py reject card-001 --project my-project --reason "checklist missing" --skill vibe-coding-setup
```

When a `review` card fails your review, one command closes the learning loop: the reason is filed as a `fail` entry into the day log and the index (it reaches `MEMORY.md` through the next approved reflection), stamped into the card's `notes:` (so the re-run prompt carries the feedback explicitly), and the card flips back to `ready` for the next dispatch. The stamp goes into `notes:` only; the prompt tells the executor that `decided` outranks notes, so if the reason overturns a decided item, edit `decided` in `BOARD.md` before the next dispatch. Rejection reasons are the highest-value learning signal; never let one evaporate. `--skill NAME` (repeatable, validated like `remember --skill`) also files the lesson as feedback for the skill the work used (`signal: reject`, `outcome: fail`), so `skill-status` shows it until a revision of that skill cites it.

### Promote a proven pattern to a skill

```bash
python3 cli/vurctos.py skill-new premium-lighting-pass --project my-project
```

Scaffolds `skills/<name>/SKILL.md` in the agentskills.io format (frontmatter plus When To Use / Inputs / Steps / Agent Roles / Outputs / Review Criteria / Memory Updates). The name must be lowercase letters, numbers, and single hyphens, and must match the folder. The CLI only builds the skeleton: the Orchestrator fills it from the proven pattern (reflect proposals list the candidates), and a human reviews it before first use. Promotion rule: only a repeated, useful pattern becomes a skill; a single prompt is not a skill.

## Why this is the whole point

The CLI does only what is mechanical and repeatable. Everything that needs judgment is done by Claude reading the local files. This keeps the system local-first, subscription-first, and free of platform-terms risk.
