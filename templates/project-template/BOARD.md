# Board

The task board is the single source of truth for coordination in this project. The Orchestrator (Claude) creates cards, assigns them to agent profiles, and moves them through statuses. Agents do not message each other directly. All coordination flows through this board and the files in `handoffs/`.

For the full model, see [ORCHESTRATION.md](https://github.com/Vurctne/VurctOS/blob/main/ORCHESTRATION.md) in the public VurctOS repository (this file is not included in the project folder).

## Card Format

```text
- id: card-001
  title: Short description of the unit of work
  assignee: codex
  channel: local
  status: ready
  inputs:
    - src/auth/session.py
    - USER.md
  expected_outputs:
    - handoffs/card-001.md
  handoff: handoffs/card-001.md
  notes: Anything the worker should know.
```

- `assignee`: an agent profile in `agents/`. The roster is Claude (Orchestrator and Executor) and Codex (independent reviewer and Executor).
- `channel`: `local` for assisted local execution on the user's own subscription (a single card run via `vurctos dispatch --agent {claude,codex}`), or `handoff` for a copy handoff where a human pastes the work into a tool and pastes the result back. VurctOS does not headlessly automate subscription logins.
- `status`: `backlog`, `ready`, `in-progress`, `review`, `done`, or `blocked`.

A card is never marked `done` until the Orchestrator has reviewed the result. `dispatch` moves a finished card to `review`, never straight to `done`. Claude never reviews its own execution output: send a Claude-executed card to Codex for review, and vice versa.

## Cards

```text
- id: card-001
  title:
  assignee:
  channel:
  status: backlog
  inputs:
  expected_outputs:
  handoff:
  notes:
```
