# VurctOS Project

This is a local VurctOS project.

Use this folder to give your AI coding assistants (Claude Code and OpenAI Codex) a shared, persistent memory and a single-card agent layer, all on your own subscriptions, no API keys. Everything here is plain markdown plus a Python stdlib CLI.

## Folder Map

- `BOARD.md`: the task board, single source of truth for coordination
- `agents/`: agent profiles the Orchestrator assigns cards to
- `handoffs/`: structured result files, one per card
- `sessions/`: dated session logs (session recall memory)
- `reflections/`: staged consolidation proposals from `reflect`, before you apply them
- `skills/`: project-specific experimental skills
- `USER.md`: your user profile, how you like to work, and how you decide (durable memory)
- `MEMORY.md`: project facts, decisions, and what worked or failed (durable memory)
- `task.md`: current task and acceptance criteria
- `AGENTS.md`: project-level agent rules

## Agents

Two agents work on this project, both on your own subscriptions:

- Claude: Orchestrator (plans, delegates, reviews, updates memory) and Executor (writes code and files).
- Codex: independent reviewer and executor. Claude never reviews its own execution output; Codex does that.

## First Recommended Workflow

Use the vibe coding workflow: describe what you want, let the Orchestrator plan it into board cards, dispatch a card to an agent, review the result, and record what was learned.

```text
task
  -> board cards
  -> dispatch (claude or codex)
  -> review
  -> memory
```

## CLI Commands

Run from this folder (`--project .`):

- `vurctos new <name>`: scaffold a new project.
- `vurctos remember --project . --what "..."`: file a memory entry.
- `vurctos recall --project . "<query>"`: search past entries (`--stats` for index stats).
- `vurctos reindex --project .`: rebuild the session search index.
- `vurctos reflect --project .` then `vurctos reflect-apply --project .`: stage and apply a human-approved consolidation of session logs into durable memory (the only way `USER.md` / `MEMORY.md` get written).
- `vurctos memory-status --project .`: show the reflect backlog and any waiting draft; stages an empty draft once the backlog is large.
- `vurctos dispatch --project . --agent {claude,codex}`: run one ready card via the chosen agent; the card moves to review, never straight to done.
- `vurctos reject <card-id> --project . --reason "..."`: reject reviewed work, file the lesson, and re-queue the card.
- `vurctos skill-new <name> --project .`: scaffold a project skill for a human to review before first use.

## Working Rule

Keep the project understandable from files alone.

If an agent produces a result, save the result here. If a decision matters, write it down. If a step repeats, consider turning it into memory or a skill.
