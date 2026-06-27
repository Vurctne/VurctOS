# Roadmap

## Phase 0: Foundation

Goal: define the project clearly without building the full system.

Deliverables:

- repository and documentation structure
- vision, architecture, roadmap, and MVP definition
- initial agent instructions
- decision log folder
- first AI video production example folder

Exit criteria:

- a contributor can explain what Vurctne OS is
- the MVP scope is clear
- non-goals are explicit
- no large runtime has been started prematurely

## Phase 1: File-Based AI Video MVP

Goal: prove that a local project workspace can reduce copy-paste and context loss.

Deliverables:

- project-folder template for AI video production
- workflow templates for brief, script, scene, shot, prompt, output, and review
- simple memory files for creator preferences and project learnings
- manual handoff instructions for subscribed tools
- validation checklist for completed workflows

Exit criteria:

- one real AI video project can run through the workflow
- outputs are traceable to prompts and source context
- useful project memory is captured without a database

## Phase 2: Assisted Handoff

Goal: make external tool handoff faster while staying conservative.

Deliverables:

- prompt packaging for major tool categories
- file preparation helpers
- output intake conventions
- optional browser or desktop handoff experiments where allowed

Exit criteria:

- creator spends less time preparing prompts and files
- handoff remains inspectable and reversible
- no fragile automation becomes a core dependency

## Phase 3: Skills And Memory

Goal: turn repeated work into reusable skills.

Deliverables:

- skill format
- examples for AI video tasks
- memory update process
- quality gates for workflow outputs
- import/export of skills and memory packs

Exit criteria:

- users can reuse a skill across projects
- memory can improve future workflows
- users can inspect and edit what was learned

## Phase 4: MCP Orchestration

Goal: add structured orchestration only after file workflows are proven.

Deliverables:

- local MCP server for project context
- workflow execution tools
- memory query and update tools
- tool-adapter discovery
- permission boundaries

Exit criteria:

- MCP improves workflow reliability
- local files remain the source of truth
- orchestration does not hide project state

## Phase 5: Open-Source Ecosystem

Goal: let the community extend Vurctne OS safely.

Deliverables:

- contribution guide
- plugin or skill publishing model
- example workflows
- test fixtures
- security and privacy review process

Exit criteria:

- contributors can add workflows without changing core runtime
- users can trust what a skill or adapter can access
- project governance is clear

## Near-Term Priorities

1. Finalize license choice.
2. Create the first AI video project template.
3. Define the workflow file format.
4. Define the memory file format.
5. Run one real production project through the manual MVP.
6. Only then decide which helpers should become code.

## Open Questions

- Which license best supports an open-source personal AI OS and commercial creator use?
- Should the first interface be CLI, local web UI, desktop app, or editor-first?
- Which AI video workflow should be the first reference example?
- Which subscription tools should be supported by handoff templates first?
- What memory format is simplest while staying future-proof?

