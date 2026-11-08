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

## Skill Storage

Repository-level skills live in the top-level `skills/` directory, each as its own SKILL.md directory. Project-specific skill notes can also live inside a project folder under `skills/` when the skill is still experimental.

## Quality Rule

Do not call something a skill just because it is a prompt.

A real skill includes process, inputs, outputs, quality checks, and memory behavior.
