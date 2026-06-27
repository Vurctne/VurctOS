# Getting Started

Vurctne OS is early-stage. Today, it is a local-first project structure and workflow foundation, not a finished application.

The future user experience should be simple:

1. Create a project folder from the template.
2. Add input assets.
3. Fill in project profile and task files.
4. Run or follow a workflow.
5. Save analysis, prompts, generated media, and final outputs.
6. Update memory with what worked and what should be reused.

## Current Manual Start

For now, start by copying:

```text
templates/project-template/
```

into a new project folder.

Then fill in:

- `README.md`: what the project is
- `PROFILE.md`: creator, brand, style, and constraints
- `task.md`: current task, inputs, outputs, and success criteria
- `MEMORY.md`: reusable learning from this project
- `AGENTS.md`: local rules for AI agents working in the project

## Recommended First Workflow

Use:

```text
workflows/viral-video-reverse-engineering.md
```

This workflow turns one input video into a structured prompt pack.

## What To Put Where

- `input/`: original source video, transcript, screenshots, or references
- `frames/`: extracted key frames
- `analysis/`: video analysis and shot breakdowns
- `prompts/`: image, video, caption, and review prompts
- `images/`: generated still images
- `videos/`: generated clips
- `final/`: final prompt pack and delivery files

## What Not To Do Yet

- do not install dependencies
- do not add API keys
- do not set up MCP
- do not build a GUI
- do not create a marketplace

The first useful version should prove that the local project workflow works.
