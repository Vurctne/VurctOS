# Board

The task board is the single source of truth for coordination in this project. The Orchestrator (Claude) creates cards, assigns them to agent profiles, and moves them through statuses. Agents do not message each other directly. All coordination flows through this board and the files in `handoffs/`.

For the full model, see [ORCHESTRATION.md](https://github.com/Vurctne/VurctOS/blob/main/ORCHESTRATION.md) in the public VurctOS repository (this file is not included in the project folder).

## Card Format

```text
- id: card-001
  title: Short description of the unit of work
  goal: What this work ultimately serves, in one line
  assignee: codex
  channel: local
  status: ready
  inputs:
    - src/auth/session.py
    - USER.md
  expected_outputs:
    - handoffs/card-001.md
  decided:
    - A decision that is settled and must not change.
    - Another one: one item per line.
  acceptance:
    - `python3 -m unittest tests.test_session` passes
    - the session timeout matches the value in docs/auth.md
  handoff: handoffs/card-001.md
  notes: Anything the worker should know.
```

- `assignee`: an agent profile in `agents/`. The roster is Claude (Orchestrator and Executor) and Codex (independent reviewer and Executor).
- `channel`: `local` for assisted local execution on the user's own subscription (a single card run via `vurctos dispatch --agent {claude,codex}`), or `handoff` for a copy handoff where a human pastes the work into a tool and pastes the result back. VurctOS does not headlessly automate subscription logins.
- `status`: `backlog`, `ready`, `in-progress`, `review`, `done`, or `blocked`.
- `goal` (optional): one line on what the work is for, so the worker can make the calls the card does not cover.
- `decided` (optional): `- item` lines, one per line, for what is settled. The worker keeps them even when `notes` or review feedback say otherwise; to overturn one, edit it here. Anything not listed is the worker's call.
- `acceptance` (optional): `- item` lines naming how someone other than the worker will check the result, such as a command in backticks or a source to compare against. The worker runs what it can and must not edit what an item names unless it is an expected output.

Inside a card every field line stays indented; a line that is neither `key: value` nor `- item` is ignored, and an indented `key: value` line ends the list above it. Check a card with `vurctos dispatch --dry-run`, which prints the exact prompt. When the worker has to depart from the card, it records that under `## Deviations` in the handoff (see `handoffs/README.md`).

A card is never marked `done` until the Orchestrator has reviewed the result. `dispatch` moves a finished card to `review`, never straight to `done`. Claude never reviews its own execution output: send a Claude-executed card to Codex for review, and vice versa.

## Cards

```text
- id: card-001
  title:
  goal:
  assignee:
  channel:
  status: backlog
  inputs:
  expected_outputs:
  decided:
  acceptance:
  handoff:
  notes:
```
