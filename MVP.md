# MVP

## MVP Name

Vurctne OS Viral Video Reverse Engineering MVP

## Tight Definition

The MVP is not the whole OS.

The MVP is a local CLI or folder workflow that turns one input video into a structured prompt pack.

The first implementation can be mostly manual. The important part is that the project folder, workflow files, memory files, and output files make the process repeatable.

## User Story

As an AI video creator, I want to place one viral reference video into a project folder and turn it into a structured prompt pack, so I can recreate the creative pattern with tools like ChatGPT, Gemini, Kling, Runway, and Claude Code without losing context.

## Inputs

Required:

- one input video file
- project brief or intent
- target platform, for example TikTok, YouTube Shorts, Reels, or ad creative
- target style or product context

Optional:

- transcript
- screenshots
- brand notes
- creator preferences
- reference images
- previous prompt examples

## Outputs

The MVP should produce:

- frame selection notes
- video analysis summary
- hook and structure breakdown
- shot-by-shot breakdown
- visual style notes
- image generation prompts
- Kling or Runway video prompts
- publishing caption options
- final prompt pack
- memory update notes

## Included

- local project template
- markdown workflow instructions
- manual frame extraction slot
- analysis file format
- prompt file format
- final pack format
- profile and memory templates
- clear agent roles

## Excluded

- full GUI
- marketplace
- cloud sync
- complex MCP orchestration
- automatic account login
- required API keys
- full video editor integration
- production-grade browser automation
- multi-user collaboration

## Manual Steps

The user or agent may manually:

1. Create the project from the template.
2. Place the input video in `input/`.
3. Extract representative frames into `frames/`.
4. Ask Gemini or another video-capable tool to analyze the video.
5. Ask ChatGPT or Claude to create creative prompts.
6. Paste prompts into Kling, Runway, or another video tool.
7. Save outputs into `images/`, `videos/`, or `final/`.

## Semi-Automated Steps

The first lightweight scripts may later help:

1. create a new project folder
2. generate starter task files
3. collect frame names
4. assemble prompt files into a final prompt pack
5. package outputs

No dependencies should be added until a step is repeated enough to justify automation.

## Success Criteria

The MVP succeeds when:

- one input video produces one complete prompt pack
- the prompt pack is usable in Kling or Runway
- the project can be understood from local files alone
- the workflow captures useful memory about decisions and style
- a second project can reuse the same workflow with less effort

## Demo Script

1. Copy `templates/project-template/` into a new project folder.
2. Rename the project and fill in `README.md`, `PROFILE.md`, and `task.md`.
3. Add the reference video to `input/`.
4. Extract or manually save key frames into `frames/`.
5. Use `workflows/viral-video-reverse-engineering.md` to guide analysis.
6. Save video analysis in `analysis/video-analysis.md`.
7. Save shot breakdown in `analysis/shot-breakdown.md`.
8. Save image prompts in `prompts/image-prompts.md`.
9. Save Kling or Runway prompts in `prompts/video-prompts.md`.
10. Save publishing captions in `prompts/captions.md`.
11. Save final assembled pack in `final/prompt-pack.md`.
12. Update `MEMORY.md` with what worked, what failed, and what should become reusable.
