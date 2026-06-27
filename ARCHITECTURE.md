# Architecture

## Architecture Goal

The first architecture should make Vurctne OS useful before it becomes complex.

The system starts with local files, explicit workflows, and reusable skills. It should avoid a heavy orchestration runtime until the team knows which parts of the workflow repeat often enough to automate.

## Layer Model

```text
Creator Project
  -> Workspace File Layer
  -> Workflow and Skill Layer
  -> Memory Layer
  -> Tool Adapter Layer
  -> Orchestration Layer
  -> Interface Layer
```

Only the first three layers are required for the MVP.

## 1. Workspace File Layer

The workspace file layer is the foundation. Every creative project should have a local folder with predictable files and folders.

Expected responsibilities:

- hold scripts, outlines, shot lists, prompts, assets, outputs, notes, and decisions
- expose context to humans and agents without a private database requirement
- support git or simple file backup
- make failed automation easy to inspect

Early project example:

```text
project/
  project.md
  brief.md
  script/
  shots/
  prompts/
  assets/
  outputs/
  memory/
  decisions/
```

## 2. Workflow And Skill Layer

Workflows describe repeatable production paths. Skills describe repeatable operations.

Examples:

- turn a short-drama concept into a five-episode outline
- convert a scene into a shot list
- generate image prompts from character and location references
- prepare video-generation prompts for Kling or Runway
- review generated shots against continuity rules

Workflows should be plain files first. A workflow can later be executed by scripts, agents, or MCP tools.

## 3. Memory Layer

Memory captures reusable learning from projects.

Memory should store:

- creator preferences
- project-specific decisions
- prompt patterns that worked
- tool-specific constraints
- visual continuity notes
- quality checklists

Memory must be inspectable and editable. The first version can use markdown or JSON files before introducing a database.

## 4. Tool Adapter Layer

Tool adapters represent integrations with external AI tools.

First-stage adapters should be conservative:

- generate copy-ready prompts
- prepare files for upload
- record expected output locations
- support browser or desktop handoff

Later adapters may automate browser sessions or APIs where allowed.

## 5. Orchestration Layer

MCP should be added after the file and workflow model is stable.

MCP can eventually provide:

- tool discovery
- structured context access
- controlled execution
- long-running workflow coordination
- safe boundaries between local project data and external tools

MCP is not required for the first MVP.

## 6. Interface Layer

The interface layer can be a desktop app, local web UI, CLI, or editor integration.

The MVP should not depend on a polished interface. A thin UI can help users browse project context, run workflows, and inspect memory, but the durable system of record remains the project folder.

## Future Package Boundaries

Potential package boundaries:

- `packages/core` for shared domain types and validation
- `packages/workspace` for project-folder templates and file operations
- `packages/workflows` for workflow definitions and runners
- `packages/skills` for reusable skill packaging
- `packages/adapters` for tool-specific handoff logic
- `packages/memory` for memory capture and retrieval
- `packages/ui` for a future desktop or web shell

These are placeholders, not immediate implementation requirements.

## Security And Privacy

Vurctne OS will handle creative IP, account sessions, and potentially private files. The architecture should assume:

- no secrets committed to git
- no API keys required for the first workflow
- no hidden upload of project files
- clear user consent before using browser automation or external services
- local-first storage by default

## First AI Video Flow

The first end-to-end workflow should look like this:

1. User creates a local AI video project.
2. Vurctne OS stores the brief, script, references, and constraints.
3. A workflow produces scene breakdowns and shot prompts.
4. The user hands prompts and assets to subscribed tools.
5. Generated outputs are saved back into the project folder.
6. The system records what worked as project memory and reusable skill improvements.

This flow is intentionally simple. It proves the operating model before adding heavy automation.

