# AGENTS.md

This repository is the foundation for Vurctne OS, an open-source personal AI operating system for AI creators.

## Current Phase

The project is in foundation and MVP-design mode.

Do not start building the full system unless the user explicitly asks for implementation. Prefer clear documents, small validated prototypes, and file-first workflow templates.

## Required Reading

Before making substantive changes, read:

- `README.md`
- `VISION.md`
- `ARCHITECTURE.md`
- `MVP.md`
- `ROADMAP.md`

## Product Principles

- Prefer subscription login workflows over API keys where practical and permitted.
- Use files and local project context as the first stable communication layer.
- Add MCP later as the orchestration layer.
- Support inspectable memory and reusable skills.
- Start with AI video production workflows.
- Keep modules small, separable, and open-source friendly.
- Do not overbuild the first version.

## Engineering Guidance

- Keep changes tightly scoped.
- Do not add a runtime, framework, package manager, or build system without explicit approval.
- Do not introduce secrets, API keys, tokens, or account credentials.
- Treat local files as the source of truth.
- Prefer markdown and simple structured files until automation is justified.
- Preserve human readability in project, workflow, skill, and memory files.
- Document important decisions in `docs/decisions/`.

## Repository Structure

- `docs/decisions/` stores architecture and product decisions.
- `examples/video-production/` will hold reference project examples.
- `workflows/` will hold workflow templates.
- `skills/` will hold reusable skill templates.
- `packages/` is reserved for future code packages after the MVP validates what should be automated.

## Git And GitHub

- Keep commits intentional and small.
- Do not commit generated secrets, local account state, browser profiles, or private creative assets.
- If creating public GitHub artifacts, make licensing state explicit before claiming the project is fully open source.

## Definition Of Done

A change is done when:

- the affected documents are internally consistent
- the repository still reflects a documentation-first foundation
- no full-system implementation has been introduced accidentally
- basic file and git status checks have been run

