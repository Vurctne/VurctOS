# Getting Started

VurctOS is early-stage. Today, it is a local-first project structure and memory foundation for AI coding assistants (Claude Code and OpenAI Codex), not a finished application.

The intended user experience is simple:

1. Create a project folder from the template.
2. Fill in the project profile and task files.
3. Work with Claude as Orchestrator, card by card, on the board.
4. Let Codex review work independently before it is accepted.
5. Update memory with what worked and what should be reused.

Most of this works today: step 1 is `vurctos new`, step 3 uses `vurctos dispatch` to run one ready board card on your own subscription, and step 5 is `vurctos remember` plus the `reflect` / `reflect-apply` consolidation loop (see `cli/README.md`). Step 3 is still Claude as Orchestrator working through the board with you, card by card, not a single packaged command.

## Current Manual Start

**Create your project OUTSIDE this repository clone.** A real project folder will hold your private `USER.md`, `MEMORY.md`, and `sessions/` (personal data), and it should never live inside the public VurctOS checkout where a stray `git add` could stage it. Pick a location on your own machine, for example `~/VurctOSData/my-project/`, and copy the template there:

```text
templates/project-template/
```

into a new project folder in that external location. (`vurctos new <name> --dir <path outside the repo>` does this for you.)

Scaffolded projects include a `.claude/` folder with a SessionStart hook that runs a small read-only script to nudge you to consolidate memory. Claude Code will ask you to trust the folder before any project hook runs; only trust folders whose `.claude/` you have reviewed, especially if someone else scaffolded the project.

Then fill in:

- `README.md`: what the project is
- `USER.md`: your user profile, how you like to work, and how you decide
- `task.md`: current task, inputs, outputs, and success criteria
- `MEMORY.md`: project facts, conventions, and reusable learning (decisions, what worked, what failed)
- `AGENTS.md`: local rules for the AI agents working in the project

## Recommended First Workflow

Use:

```text
workflows/vibe-coding.md
```

This workflow takes a coding task through the board: Claude plans and executes it as a card, Codex reviews the result independently, and the outcome plus any lesson is filed into memory.

## What To Put Where

A scaffolded project holds working files in a few plain folders:

- `agents/`: worker profiles (Claude, Codex) for this project
- `handoffs/`: typed results passed between agents, one file per card
- `skills/`: reusable procedures in the SKILL.md format
- `sessions/`: per-day session logs and the local full-text index
- `reflections/`: staged consolidations awaiting your approval before they update memory

## What Not To Do Yet

- do not install dependencies
- do not add API keys
- do not set up MCP
- do not build a GUI
- do not create a marketplace

The first useful version should prove that the local project workflow, shared memory, and single-card agent layer work together.
