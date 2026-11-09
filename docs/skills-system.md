# Skills System

A VurctOS skill is a reusable workflow learned from repeated work.

Skills should begin as documents and templates. Later, they may become scripts, plugins, or (in the future) marketplace packages.

## What A Skill Should Capture

A useful skill should define:

- when to use it
- required inputs
- output format
- step-by-step process
- agent roles
- quality checks
- memory updates
- examples

The goal is to make repeated work easier without hiding the process.

## Skill Format

VurctOS skills follow the SKILL.md open standard (agentskills.io), so a skill is portable to and from the wider ecosystem and the future Vurctne Skills marketplace stays compatible by default.

A skill is a directory under `skills/`:

```text
skills/
  vibe-coding-setup/
    SKILL.md
    references/
    scripts/
    assets/
```

`SKILL.md` is YAML frontmatter plus a markdown body. Required frontmatter is `name` and `description` only:

```text
---
name: vibe-coding-setup
description: Prepare a clean local project context for a coding agent (task file, conventions, agent rules, review checklist). Use when starting work in a new or unfamiliar repo with Claude Code or Codex.
version: 0.1.0
author: vurctne
---
```

Rules from the standard: `name` is lowercase letters, numbers, and hyphens and must match the folder name. `description` must state both what the skill does and when to use it, since that is what makes the Orchestrator select it. Avoid angle brackets in frontmatter. The markdown body should cover the items above: when to use, inputs, steps, agent roles, outputs, quality checks, and memory updates.

## Early Skill Examples

### Vibe Coding Setup Skill

Creates a clean local project context for coding agents (Claude Code, Codex).

It may include:

- repo inspection
- task file
- architecture and convention notes
- agent rules
- review checklist

This is the one worked example carried through the current docs. It is deliberately small: it produces the files a coding agent needs to start well, nothing more.

## Skill Lifecycle

1. A workflow is repeated manually.
2. The user or agent notices a pattern (reflect proposals collect these as skill candidates).
3. The pattern is documented as a skill draft: `vurctos skill-new <name>` scaffolds the compliant skeleton, the Orchestrator fills it, a human reviews it.
4. The skill is tested on another project.
5. The skill gains quality checks.
6. Only then should automation be added.

## Feedback Loop

A lesson about a skill is filed as a feedback entry (`vurctos remember --skill NAME --signal ...`, or `vurctos reject --skill NAME`; see `docs/memory-system.md`). The day log stays the only record of the lesson. Whether the skill has answered it is recorded in the skill's own files:

- A revision that answers entry `2026-11-10#4` cites `fb:2026-11-10#4` in any `.md` file of the skill, usually on the version-log line that describes the change. A bare id in prose does not count, nor does a token glued to an ASCII letter, digit, underscore or colon (`xfb:`); text in other scripts may run straight into it. `.git`, `__pycache__`, `.DS_Store`, `evals/` and nested skills (a subfolder with its own `SKILL.md`) are not part of the skill's files.
- `fb-retired: YYYY-MM-DD <ref>` records that a rule was retired; a pending entry filed later that still names that rule is flagged, since it suggests the retirement was premature. `fb-renumbered: YYYY-MM-DD <old> -> <new>` records a renumber, so a rule ref is compared in the numbering of its own date. Either line may sit in a list item.
- An entry found wrong is withdrawn with `remember --signal retract --supersedes <id>` (or `--signal decline` for one that will not be acted on), naming the skill or no skill.

`vurctos skill-status (--global | --project P) [--skill NAME] [--json] [--hook]` is read-only. For each skill or target with feedback it shows the pending entries (counts by signal and outcome, the oldest date), entries marked `landed` but not yet cited, cited entries that were later retracted, pending entries that name a retired rule, the current `skillver` against the one each entry was filed with, the character counts of `SKILL.md`, the rule files and the history files (`history_globs`), and a score for ordering the work: 3 per pending correction or reject, 1 per other pending failure, 3 per retracted-but-cited entry, 2 per entry naming a retired rule. `--json` prints the same data; `--hook` prints one line of numbers only and never entry text.

Why the record lives in the skill's files rather than in a separate ledger: when a skill is rolled back (a restored copy, a revert, a hand edit), its citations go back with it, so the entries it had answered become pending again without anyone remembering to undo them. A ledger would keep them marked answered while the fix is gone, and the lesson would be lost silently. The opposite mistake, a forgotten citation, only shows the entry again. Whoever revises the skill writes the citation; `skill-check` (below) fails a citation that names no feedback entry for the skill and reports the evidence for the others.

## Static Check

Skills are edited on the spot by whichever session finds a problem, so a check has to be cheap enough to run after every edit. `vurctos skill-check` runs no model and takes seconds. It compares a candidate tree with a base tree and answers three questions:

1. Did the edit break anything mechanical? New lint lines, or a file over its character budget.
2. Did it touch what only the owner decides? Protected lines and files, and changed lines carrying a category word (for example spending or publishing).
3. Does it claim to answer feedback it has no entry for? A newly cited `fb:<id>` must be a feedback entry for this skill.

**Levels.** `fail` (exit 1) blocks; `hold` (exit 3) goes back to the owner; `pass` (exit 0). Info lines (net growth, unbudgeted rule files, pre-existing overruns, citation evidence) never change the level. The recognising patterns (`protected_line_patterns`, `protected_category_words`, `protected_files`) live in the private `skill-loop.json`, so no owner-specific text enters this repository. Protected lines are compared as one multiset over the skill's rule files (moving one between files is fine, renumbering a list item is fine); a new protected line also holds, so an editor cannot make its own rule unremovable by marking it.

**The store.** `vurctos skill-snapshot NAME --accept` copies the live tree to `<evals_dir>/_snapshots/<name>/<tree hash>/` and points the `accepted` file beside it at that hash. A hash directory is never modified once complete. `skill-check NAME --since-accepted` checks live against that copy and advances `accepted` on a pass (or on a hold with `--accept`, never on a fail). `skill-snapshot NAME --workdir W` makes an `orig/` and `cand/` pair for an edit made in a copy instead; `skill-check NAME --workdir W` checks it and advances nothing.

**The mirror.** Lint tools usually build their paths from `$HOME`, and skills are reached through chains of symlinks (`~/.claude/skills/x -> ~/.agents/skills/x -> <repo>/x`). To lint the base and the candidate side by side, each is run with `HOME` pointing at a mirror of the home: every entry is a symlink to the live one, except that each directory along the skill's access paths is a real directory and each access path (every hop of the chain, and the real path) is a symlink to the copy under test. Every hop has to be overlaid: an absolute link left pointing at the live tree would lead the lint straight back to live, and both runs would read the same files. A mirror holds only directories and symlinks, never file content. The two outputs are compared after replacing the mirror and copy paths and every run of digits, so line numbers and counts that move with unrelated lines do not count as new violations.

See `cli/README.md` for the command lines and the configuration keys.

## Eval Runner

An edit can pass the static check and still make the skill worse at its job. `vurctos skill-eval NAME --workdir W` runs the base copy (`W/orig`) and the candidate (`W/cand`) on the same test cases, headlessly on the subscription login, and compares them grader by grader. It never lets a model grade a model: the runner judges only what it can compute (regexes, tool calls, created files, owner-pinned judge scripts), and anything else is reported as unjudged. Cases where only a person can judge quality get a blind pick page instead.

**Cases** live in the private `evals_dir`, never inside a skill, so a candidate cannot edit what judges it. The layout follows Claude Code's plugin-eval format, so a case stays readable by `claude plugin eval`:

```text
<evals_dir>/<skill>/<case>/prompt.md            front matter + the prompt
<evals_dir>/<skill>/<case>/graders/<name>.md    one grader per file
<evals_dir>/<skill>/<case>/files/               copied into the run's cwd
<evals_dir>/<skill>/_sidecars/<case>.json       runner-only fields
<evals_dir>/_judges/<sha256>/...                pinned judge directories
```

`prompt.md` takes the plugin-eval keys (`allowed_tools` is the run's tool set, `timeout_seconds` its wall clock, `runs` the runs per arm, `env` only `EVAL_*` variables; `model` must equal the pinned `eval_model`). Grader types are `regex`, `tool_used`, `tool_order`, `file_exists`, and `llm` / `baseline`, which load but are never judged. The front matter is read by a small parser that accepts `key: value` lines, quoted and bare scalars, flow lists and maps, and block lists, and refuses anything YAML would read differently. The sidecar holds `fails_on` (required: a case that cannot say what output makes it fail tests nothing), `status` and `approved_hash`, `targets` (feedback ids), `judge` (`runner`, `human` or `mixed`), `expect_skill`, `read_roots`, `allow_rules`, script `checks` and `flaky`. An approval counts only while `approved_hash` equals the case hash, which covers every file of the case, the sidecar (without `status` and `approved_hash`) and each referenced judge directory, so editing an input, grader, check or judge makes the approval stale. A judge directory is named by the sha256 of all its files; one whose contents no longer hash to its name stops the command before any run. A check runs only the pinned judge, never a script from the skill under test.

**Isolation.** Each run gets its own mirror of the home, built like the static check's, in which every hop to the skill leads to the arm under test. `.claude` holds only `skills`, `CLAUDE.md` and generated read permissions for both spellings (mirror and real path) of the skill and of each read root; `.claude.json` is a copy (the login); the memory root is a copy of its top-level files with an empty `sessions/` and without the cases or the work directory, so a `remember` inside a run never reaches the real day logs. The child environment is an allowlist (the parent of a real run is itself a Claude Code session, whose environment carries session ids, tokens and API keys), blocked binaries such as `codex` or `gh` are shims that log and refuse, MCP servers are disabled, writes are confined to the run's cwd, and a timeout kills the whole process group. A stop signal (SIGTERM, SIGHUP) takes the same path as an interrupt, so the mirror with its copy of the login is removed and the agent does not run on alone.

**The per-run audit** decides validity, not quality. Any failed rule makes the run invalid, never passed; an invalid run is retried once.

- A0 process: exit 0, no timeout, a whole stream with one init and one result event. Anything less is not a complete run.
- A1 init: `apiKeySource` is `none`, no MCP servers, the pinned model, only the allowed tools, only built-in plugins. A run billed elsewhere, or with extra capabilities, is not the run being compared.
- A2 the subject's load: the skill was loaded from this run's mirror and its body is this arm's `SKILL.md` (followed by `ARGUMENTS: <args>` when the run's Skill call passed args, as the CLI appends them). This is the one rule that proves which copy ran; without it, both arms could silently run the live skill and every change would look "not worse".
- A3 contamination: no tool input names the live skill, the cases or (outside a probe) the other arm. A run that read any of them was not working from its own copy alone.
- A4 side effects: no write outside the cwd, no blocked binary, no permission denial (a denied read means the skill ran without its own files).
- A5 result: the run reports success. A run that ended in an error produced no output worth comparing.

A billing failure, a failed login preflight, loader failures (A2 or A3 after the retry) on two different cases, or an arm tree, a case or a judge changing mid-batch abort the batch, and so does an agent whose version differs at the end (it updated itself, so the arms may have run different versions). A read root that changes during the batch invalidates the runs of the cases that list it.

**The verdict** compares each grader and check over valid runs only: `insufficient` when an arm has fewer than half its runs valid, `unjudged` for `llm` and `baseline`, `flaky` when the sidecar lists it, otherwise `regressed`, `improved` or `same` by pass rate. `not_worse` means no regression and no insufficient result anywhere: an improvement on one grader never offsets a regression on another. `targeted` reports, per feedback id, `fixed` (every base run fails the case, every candidate run passes), `not-discriminating` (the base already passes) or `not-fixed`. Levels: `fail` (exit 1) on an abort, a regression, an insufficient result or a blind pick for the base; `hold` (exit 3) while a person still has to pick; `pass` (exit 0). `--probe` runs `W/orig` in both arms to find unstable graders (which the owner copies into `flaky`) and to show that a targeted case fails on the base. Everything a batch writes goes to `W/eval/<batch>/`.

**The blind pick.** `vurctos skill-pick make` writes a page for the cases a person judges: per case the prompt, then outputs A and B (final message and created files), drawn at random per case. Paths (as given, resolved, or written `~/...`) are replaced with neutral placeholders, an arm's directory with `<skill>` on both sides, and the page is refused if it would still reveal the workdir, an arm directory name, a run label such as `cand-1`, a hash or the batch id; its own text never names the arms. A page is made once per batch, so answers written on it always map through its own key. The key that maps A and B back is sealed beside it with its sha256 recorded. `vurctos skill-pick record --answers "1:A,2:same"` maps the answers through the key (refusing a key that changed) and recomputes the verdict.

## Putting a candidate live

`vurctos skill-apply NAME --workdir W` is the only door from a judged candidate (`W/cand`) to the live skill. `--check` evaluates the predicates below and prints the planned changes and, unless blocked, an approval hash; `--approved-hash H` applies only while `H` is still the hash of exactly what was judged. The tool never applies on its own: a person passes the hash, or a private task of theirs does under a standing authorization they wrote. It also never runs git or any other subprocess; it prints the changed live paths so the caller can commit them by explicit path.

**Block** (no hash; any hash refused):

- B1 `W/orig` no longer hashes to the snapshot: the base the candidate was judged against is gone.
- B2 `W/cand` (and the snapshot) must hash to what `check.json` and the verdict recorded. Otherwise something other than what was judged would go live; edit `cand/`, then rerun the check and the eval.
- B3 live must still hash to the snapshot, and the skill must still resolve to the same real path. Skills are edited by many sessions; applying over a later edit would destroy it silently. A candidate identical to its base also blocks (nothing to apply).
- B4 the verdict: a probe batch (both arms ran the base), an aborted batch, a `fail` verdict, picks still pending for a human-judged case, or a suite with fewer than 3 approved cases that the runner can judge.
- B5 every case of the batch must still load and hash as it did, and every judge directory it references must still hash to its name. Otherwise the verdict is about other cases.
- B6 a `fail` from the static check.
- B7 a symlink the candidate adds, removes or retargets, or an entry live reaches through a symlink. The tree hash does not cover links, and writing through one could change a file another skill shares, so such a change is made by hand.
- B8 a single-file target can only have its one file changed.
- B9 the workdir was already applied.
- B10 an added file whose path is a folder in live (a folder turned into a file): the apply could never finish, so that change is made by hand.

**Hold** (the owner may still approve with the hash after reading them): a `hold` from the static check (one reason per hold finding, quoting it), and invalid runs in the batch.

**The hash** is 16 hex of a sha256 over the skill name, the bytes of `W/manifest.json`, the candidate's tree hash, the bytes of `check.json`, the batch name, the bytes of `verdict.json` and of `picks.json` (if any), and the planned changes (path, action, sha256 of the new bytes, permission bits; special bits such as setuid are dropped on the way live). Any edit to any of them makes an old hash stale, and the apply recomputes everything from the files as they are then rather than trusting what `--check` printed.

**The apply** compares the two copies file by file (add, change, remove) and changes live in that order: removes, then adds, then changes, with files matching `history_globs` (the version log whose `fb:` citations mark feedback answered) after all others. Before each entry the live file is hashed again; it must still be what the snapshot saw (or, for an add, absent). New content goes to a temp file beside its target, the live file is checked once more, and the temp file is renamed into place at once, so each file is old or new, never half. An edit can still be lost if it lands in the moment between that last check and the rename; the filesystem offers no compare-and-swap for file content. A removed file is moved to `W/apply/removed/`, never deleted. When live then hashes to the candidate, the candidate's copy is stored under `<evals_dir>/_snapshots/<name>/` and both `accepted` and `evaluated` point at it (`evaluated` is the tree last put live by an apply), and `W/apply/applied.json` records the apply. A workdir applies once.

**A stop** restores nothing, because restoring could overwrite the other session's edit. The entries already done stay done, and `W/apply/stopped.json` lists what was replaced, what was moved, where it stopped, which history files were written and where the pointers are. A stop at an entry (a live file changed under the apply, or an OS error) comes before the history files unless it is at one of them, so usually no feedback is marked answered and no pointer moves; the fix is a fresh snapshot and a candidate redone on it. A stop after the last entry (live no longer hashes to the candidate because another file changed meanwhile, or the snapshot store could not be updated) comes after everything was written, history files included: live holds the candidate plus the other change, and the stop says so and reports the pointers as they are. Look at the other change before the next snapshot. A temp file left by a killed apply makes the next `--check` block, and the reason names it.

## Skill Storage

Repository-level skills live in the top-level `skills/` directory, each as its own SKILL.md directory. Project-specific skill notes can also live inside a project folder under `skills/` when the skill is still experimental.

## Quality Rule

Do not call something a skill just because it is a prompt.

A real skill includes process, inputs, outputs, quality checks, and memory behavior.
