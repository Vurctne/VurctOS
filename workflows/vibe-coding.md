# Vibe Coding Workflow

## Purpose

Use Claude Code, Codex, Gemini, and ChatGPT as a coordinated coding team while keeping the project grounded in local files, documentation, and reviewable changes.

The workflow should help creators and builders move fast without letting the project become unstructured.

## Agent Cooperation Model

### Claude Code

Claude Code acts as executor and project automation engineer.

Use it for:

- repo inspection
- file creation
- implementation
- scripts
- local automation
- structured task execution

### Codex

Codex acts as reviewer and consistency checker.

Use it for:

- implementation review
- code review
- architecture consistency
- missing test or doc checks
- checking whether the change overbuilds the project

### Gemini

Gemini acts as long-context analyst and research assistant.

Use it for:

- large document review
- video or transcript analysis
- comparing long references
- research-heavy planning

### ChatGPT

ChatGPT acts as creative director and product thinking partner.

Use it for:

- product framing
- prompt writing
- UX judgment
- naming and positioning
- visual direction

## Project File Organization

Each coding project should keep:

```text
AGENTS.md
README.md
task.md
MEMORY.md
PROFILE.md
docs/
```

For Vurctne OS project templates:

```text
input/
frames/
analysis/
prompts/
images/
videos/
final/
```

The files should tell agents what exists, what matters, what not to touch, and how to verify work.

## Task Flow

1. Define the task in `task.md`.
2. Confirm project rules in `AGENTS.md`.
3. Check existing docs and structure.
4. Ask the execution agent to make the smallest useful change.
5. Ask the review agent to check consistency, risks, and overbuilding.
6. Update docs if the structure changes.
7. Update memory with lessons learned.

## Memory And Documentation Updates

After meaningful work, update:

- `MEMORY.md` with what was learned
- `README.md` if user-facing behavior changed
- `AGENTS.md` if agent rules changed
- architecture docs if boundaries changed
- workflow docs if process changed

Memory should capture decision patterns, not only facts.

## Code Review

Codex review should check:

- correctness
- missing tests or verification
- hidden complexity
- unnecessary abstractions
- docs drift
- security or privacy risks
- whether local-first assumptions are preserved

Findings should be specific and tied to files or behavior.

## Avoiding Overbuilding

Before adding code, ask:

- Is this workflow proven manually?
- Is the step repeated often enough?
- Can markdown solve it for now?
- Does this require a new dependency?
- Can a local script solve it instead of a platform layer?
- Does this change make the first demo clearer?

If the answer is uncertain, document the idea and wait.

## Output Standard

A good vibe coding session ends with:

- clear files changed
- clear verification result
- updated docs if needed
- updated memory if useful
- a next step that is small enough to execute
