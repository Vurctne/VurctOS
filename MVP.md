# MVP

## MVP Name

Vurctne OS AI Video Workspace

## MVP Objective

Help an AI video creator move one project from idea to generated assets with less context loss and less repeated prompting.

The MVP should prove the operating model:

- files as shared context
- workflows as repeatable production paths
- skills as reusable operations
- memory as inspectable learning
- subscription tools as external team members

## Target User

An AI creator producing short films, short dramas, ads, music videos, or social video content with a mix of chat, image, video, coding, and editing tools.

## Core User Story

As an AI video creator, I want one local project workspace that keeps my brief, script, characters, scenes, prompts, generated assets, decisions, and learnings together, so I can move between AI tools without rebuilding context every time.

## MVP Scope

### In Scope

- local project-folder convention
- markdown-first project files
- AI video workflow templates
- reusable prompt and skill templates
- project memory file
- creator preference file
- manual handoff to subscription tools
- output intake convention
- quality review checklist

### Out Of Scope

- full desktop OS
- full browser automation
- universal MCP orchestration
- account/session management
- payments
- cloud sync
- multi-user collaboration
- marketplace
- production plugin system
- required API integrations

## Minimal Project Folder

```text
my-video-project/
  project.md
  brief.md
  preferences.md
  memory.md
  script/
    outline.md
    scenes.md
  production/
    shot-list.md
    continuity.md
  prompts/
    image-prompts.md
    video-prompts.md
    review-prompts.md
  assets/
    references/
    generated/
  outputs/
  decisions/
```

## First Workflow

1. Create project brief.
2. Capture creator preferences.
3. Draft or import script.
4. Break script into scenes.
5. Convert scenes into shot list.
6. Generate image and video prompts.
7. Hand prompts and assets to subscribed tools.
8. Save outputs into the project folder.
9. Review outputs against continuity and style rules.
10. Record what worked into project memory.

## First Skills

Potential first skills:

- `script-to-scenes`
- `scene-to-shot-list`
- `character-continuity-check`
- `shot-to-image-prompt`
- `shot-to-video-prompt`
- `output-review`
- `memory-update`

These should start as documented templates, not a heavy execution runtime.

## Data Model

The MVP data model should be plain files:

- markdown for human-readable context
- JSON only where structured validation is clearly useful
- file naming conventions instead of a database
- links between files by relative path

## Handoff Model

The first handoff model is copy-ready and file-ready:

- prepare prompt blocks for each target tool
- list required reference files
- describe expected output naming
- record where outputs should be saved

Browser automation and MCP orchestration can come later.

## Validation

The MVP is valid when:

- a creator can run one real AI video project through the folder workflow
- another agent or human can understand the project state from files alone
- outputs can be traced back to prompts and source context
- at least one repeated operation becomes a reusable skill
- project memory improves the next workflow pass

## Build Rule

Do not build a full platform before this MVP is proven. Add code only when a repeated manual step has become clear enough to automate.

