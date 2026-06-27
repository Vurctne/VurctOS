# Project Agent Rules

This folder is a Vurctne OS creator project.

## Project Purpose

Use this project folder as the source of truth for one creative workflow.

Agents should read local files before acting.

## Required Reading

Before making changes, read:

- `README.md`
- `PROFILE.md`
- `task.md`
- `MEMORY.md`

## Working Rules

- Keep files human-readable.
- Do not delete source assets.
- Do not overwrite generated outputs without permission.
- Save analysis in `analysis/`.
- Save prompts in `prompts/`.
- Save generated stills in `images/`.
- Save generated videos in `videos/`.
- Save final packages in `final/`.
- Update `MEMORY.md` after meaningful work.

## Safety Rules

- Do not store secrets or account sessions here.
- Do not upload files externally unless the user asks.
- Prefer subscription login handoff over API keys unless instructed otherwise.
- Keep workflow steps explicit.

## Done Criteria

A task is done when:

- requested outputs exist in the correct folder
- project memory is updated if useful
- unclear assumptions are documented
- files remain easy for another agent or human to inspect
