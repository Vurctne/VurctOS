# Subscription-First Principle

Vurctne OS should prefer official subscription login workflows where possible.

The first users already pay for AI tools. The system should help them use those tools together before forcing an API-first architecture.

## Why Subscription-First

Subscription-first workflows are practical because:

- creators already pay for tools like ChatGPT, Claude, Gemini, Kling, Runway, and others
- many tools have better web or desktop experiences than public APIs
- API costs can block early adoption
- not every creator wants to manage keys
- local files and copy-ready prompts are enough for a useful first workflow

## Examples

Preferred early paths:

- Claude Code login for local project execution
- Codex or ChatGPT login for review and creative work
- Gemini CLI login for video analysis or long-context analysis
- subscription web tools for Kling, Runway, Veo, Hailuo, and similar systems

## What Vurctne OS Should Do First

The first version should:

- prepare clean prompt files
- list required input files
- explain where outputs should be saved
- keep a record of which tool was used
- update memory after results are reviewed

This reduces copy-paste while staying simple.

## API Keys Later

API keys may be supported later for:

- batch processing
- server workflows
- repeatable automation
- integrations where official APIs are stable and affordable

API keys should not be required for the first MVP.

## Automation Boundaries

Subscription-first does not mean unsafe automation.

Rules:

- prefer official login flows
- respect platform terms
- do not store account sessions in project folders
- do not hide external uploads from the user
- make handoff steps explicit

The first product should be useful even when the user manually pastes prompts into subscribed tools.
