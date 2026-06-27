# Viral Video Reverse Engineering Workflow

## Purpose

Turn one viral reference video into a structured prompt pack for AI video production.

The goal is not to copy the original video. The goal is to understand the creative pattern, extract useful structure, and create a new production package that can be used with tools like ChatGPT, Gemini, Kling, Runway, Veo, Hailuo, and ComfyUI.

## Required Input

Minimum:

- one source video in `input/`
- project goal in `task.md`
- creator or brand context in `PROFILE.md`

Optional:

- transcript
- screenshots
- reference images
- product notes
- target platform notes
- previous prompt examples

## Folder Structure

```text
project/
  input/
    source-video.mp4
  frames/
    frame-001.png
    frame-002.png
  analysis/
    video-analysis.md
    shot-breakdown.md
    hook-analysis.md
  prompts/
    image-prompts.md
    video-prompts.md
    captions.md
    review-prompts.md
  images/
  videos/
  final/
    prompt-pack.md
  MEMORY.md
  PROFILE.md
  task.md
```

## Steps

### 1. Define The Task

Fill in `task.md`:

- source video
- target platform
- desired output
- target audience
- brand or style constraints
- success criteria

### 2. Extract Frames

Save representative frames into `frames/`.

Frame selection should capture:

- opening hook
- major visual transitions
- peak emotional moments
- product or story reveal
- ending or call to action

This step can be manual first. A script can come later.

### 3. Analyze The Video

Save analysis in `analysis/video-analysis.md`.

Analyze:

- hook
- pacing
- story structure
- emotional beats
- camera language
- visual style
- editing rhythm
- text or caption strategy
- what makes the video work

Recommended agent: Gemini for video analysis and long-context review.

### 4. Create Shot Breakdown

Save shot breakdown in `analysis/shot-breakdown.md`.

For each shot, capture:

- shot number
- frame reference
- duration estimate
- subject
- action
- camera movement
- composition
- lighting
- emotional purpose
- production notes

### 5. Build Image Prompts

Save image prompts in `prompts/image-prompts.md`.

Each prompt should include:

- subject
- setting
- composition
- lens or camera feel
- lighting
- color palette
- mood
- style constraints
- negative guidance

### 6. Build Video Prompts

Save video prompts in `prompts/video-prompts.md`.

Each prompt should include:

- starting visual
- motion
- camera movement
- subject action
- timing
- continuity constraints
- tool-specific notes for Kling, Runway, Veo, or Hailuo

### 7. Build Publishing Captions

Save captions in `prompts/captions.md`.

Include:

- short hook options
- caption options
- platform-specific variants
- call to action options
- hashtag or keyword notes if useful

### 8. Package Final Prompt Pack

Save final output in `final/prompt-pack.md`.

The final pack should include:

- project summary
- source pattern summary
- shot list
- image prompts
- video prompts
- captions
- recommended generation order
- quality checklist

### 9. Update Memory

Update `MEMORY.md` with:

- what worked
- what failed
- prompt patterns worth reusing
- tool-specific behavior
- style decisions
- possible future skills

## Agent Roles

- Gemini: analyze video, transcript, frames, and long-context structure.
- ChatGPT: improve creative direction, prompt language, captions, and visual judgment.
- Claude Code: organize local files, create simple scripts, and package outputs.
- Codex: review consistency, structure, and implementation decisions.
- Hermes Agent: future memory capture and self-learning layer.

## Expected Output Files

```text
analysis/video-analysis.md
analysis/shot-breakdown.md
analysis/hook-analysis.md
prompts/image-prompts.md
prompts/video-prompts.md
prompts/captions.md
prompts/review-prompts.md
final/prompt-pack.md
MEMORY.md
```

## Prompt Pack Structure

```text
# Prompt Pack

## Project Summary
## Source Video Pattern
## Hook Analysis
## Shot Breakdown
## Image Prompts
## Video Prompts
## Captions
## Generation Order
## Quality Checklist
## Memory Notes
```

## Future Automation Ideas

- create project folders from the template
- extract frames automatically
- generate starter analysis files
- collect prompts into a final pack
- validate missing sections
- produce a zip package for handoff
- expose project context through MCP

Automation should only be added after the manual workflow has been tested on real projects.
