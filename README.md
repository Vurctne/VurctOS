# Vurctne OS

Vurctne OS is an open-source personal AI operating system for creators who already use tools like ChatGPT, Claude, Gemini, Codex, Kling, Runway, and other AI products.

The goal is simple: turn existing AI subscriptions into a self-learning creative team without forcing creators to copy-paste context between every tool.

## Current Status

This repository is in foundation design mode.

There is no production app, no full runtime, and no automation system yet. The first version should define the operating model clearly, then build the smallest useful workflow for AI video production.

## Principles

1. Prefer subscription login workflows over API keys where practical and permitted.
2. Use files and local project context as the first stable communication layer.
3. Add MCP later as the orchestration layer, not as the first dependency.
4. Support self-learning memory and reusable skills.
5. Start with AI video production workflows.
6. Keep modules separable and open-source friendly.
7. Do not overbuild the first version.

## First Project Structure

```text
VurctneOS/
  README.md
  VISION.md
  ARCHITECTURE.md
  ROADMAP.md
  MVP.md
  AGENTS.md
  docs/
    decisions/
  examples/
    video-production/
  workflows/
  skills/
  packages/
```

The empty directories are placeholders for later work. They should stay light until the MVP proves which abstractions are actually needed.

## Document Map

- `VISION.md` explains why Vurctne OS exists and what it should become.
- `ARCHITECTURE.md` defines the first technical boundaries.
- `ROADMAP.md` lays out staged delivery without overbuilding.
- `MVP.md` defines the first useful product slice.
- `AGENTS.md` gives coding-agent instructions for future work in this repository.

## Initial MVP Theme

The first MVP is an AI video production workspace:

- one local project folder per creative project
- durable context files for script, shots, assets, prompts, decisions, and outputs
- reusable workflow templates for common video-production jobs
- simple memory capture from completed work
- manual or semi-automated handoff into subscription-based AI tools

## Non-Goals For The First Version

- A full desktop operating system.
- A universal agent marketplace.
- Full MCP orchestration.
- Browser automation for every AI website.
- Replacing ChatGPT, Claude, Gemini, Kling, Runway, or Codex.
- Requiring paid API keys before there is a proven creator workflow.

## License

License is not selected yet. Before the first public release, choose a real open-source license and add `LICENSE`.

