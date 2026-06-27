# Skills System

A Vurctne OS skill is a reusable workflow learned from repeated work.

Skills should begin as documents and templates. Later, they may become scripts, MCP tools, plugins, or marketplace packages.

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

## Early Skill Examples

### Viral Video Analysis Skill

Turns a reference video into:

- hook analysis
- structure analysis
- pacing notes
- shot breakdown
- reusable creative pattern

### Kling Prompt Skill

Turns a shot description into a Kling-ready video prompt.

It should capture:

- camera movement
- subject action
- environment
- timing
- visual style
- constraints and negative guidance

### AI Ad Film Skill

Turns a product or offer into an AI ad film package.

It may include:

- offer angle
- script
- shot list
- product visual rules
- video prompts
- captions

### Vibe Coding Setup Skill

Creates a clean local project context for coding agents.

It may include:

- repo inspection
- task file
- architecture notes
- agent rules
- review checklist

### School Finance Automation Skill

Captures a repeated workflow for school finance tooling.

It may include:

- data import rules
- reconciliation steps
- report generation
- review and approval notes

## Skill Lifecycle

1. A workflow is repeated manually.
2. The user or agent notices a pattern.
3. The pattern is documented as a skill draft.
4. The skill is tested on another project.
5. The skill gains quality checks.
6. Only then should automation be added.

## Skill Storage

Early skills can live in:

```text
skills/
```

Project-specific skill notes can also live inside a project folder when the skill is still experimental.

## Quality Rule

Do not call something a skill just because it is a prompt.

A real skill includes process, inputs, outputs, quality checks, and memory behavior.
