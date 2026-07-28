---
name: aivideoeditor-visual-moderation
description: Visual and dialogue content moderation workflow for AIVideoEditor image, video, video-frame, OCR, subtitle, ASR, and masking/redaction safety checks. Use when Codex needs to design, implement, test, or refine picture/frame violation detection, video moderation, multimodal moderation, LangGraph/LangChain moderation orchestration, mosaic masking, audio mute, subtitle replacement, or policies for real military-sensitive, real ID document, credential, NSFW, nudity, sexual, or vulgar visual/dialogue risks.
---

# AIVideoEditor Visual Moderation

Use this skill to build or maintain visual moderation for image, video-frame, OCR, subtitle, and dialogue inputs. Keep the initial scope limited to three categories: real-world military-sensitive content, real identity/credential documents, and sexual/NSFW content.

## Core Pattern

Separate evidence collection from final policy:

1. Collect fast CV/OCR/ASR evidence first.
2. Run a vision LLM only when evidence is risky, uncertain, or context-sensitive.
3. Distinguish real-world concrete sensitive content from fictional, historical, game, anime, or generic costume context.
4. Fuse detector, OCR, subtitle/dialogue, and VLM signals into normalized category scores.
5. Apply deterministic policy rules for `PASS`, `REVIEW`, or `BLOCK`.
6. Return the stable decision schema from `references/output_schema.md`, including redaction targets when action is not `PASS`.

Do not let a VLM directly bypass the policy layer. Treat model output as evidence, not as the final authority.

## Workflow

When asked to implement, debug, or test moderation:

1. Read `references/policy_baseline.md` for category definitions and baseline rules.
2. Read `references/output_schema.md` before changing any API, worker, or evaluator output.
3. Read `references/langgraph_blueprint.md` when adding LangGraph/LangChain orchestration.
4. Use `scripts/run_visual_moderation.py --self-test` for a quick policy regression check.
5. For video tests, sample frames and pair them with OCR/ASR transcript segments whenever available.
6. For image tests, prefer sidecar JSON or an OpenAI-compatible VLM endpoint; do not hardcode credentials.
7. If risky visual or dialogue evidence is detected, emit redaction targets and create masked outputs only from those targets.
8. Add or update regression fixtures whenever a policy rule changes.

## Backend Integration Guidance

For this repository, prefer these module boundaries when promoting the prototype into backend code:

- `app/agents/content_moderation/agent.py`: stable async entrypoint.
- `app/agents/content_moderation/graph.py`: LangGraph state machine.
- `app/agents/content_moderation/nodes.py`: CV, OCR, VLM, fusion, and policy nodes.
- `app/agents/content_moderation/prompts.py`: VLM prompts only.
- `app/agents/content_moderation/schemas.py`: Pydantic request/response contracts.
- `app/agents/content_moderation/policies.py`: deterministic rules and policy version.
- `app/services/content_moderation_service.py`: database/service orchestration if needed.
- `app/workers/moderation_worker.py`: Celery wrapper if the job is asynchronous.

Keep storage access through `app.utils.storage.storage`, write task failures to `error_message`, and keep public output fields stable.

## Local Test Commands

Run the built-in policy tests:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_visual_moderation.py --self-test
```

Run a fixture JSON:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_visual_moderation.py .\sample_fixture.json
```

Run a video with sampled frames only:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_video_visual_moderation.py .\input.mp4 --provider dashscope --model qwen3-vl-flash --sample-count 20 --output .\video_report.json
```

Video runs always use the NudeNet-based NSFW body-region localization path. Do not use hand-authored boxes for NSFW masking unless debugging or importing external tracker output.

Run a video with subtitle/dialogue evidence:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_video_visual_moderation.py .\input.mp4 --provider dashscope --model qwen3-vl-flash --transcript .\input.srt --output .\video_report.json
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_video_visual_moderation.py .\input.mp4 --dialogue "10.2,12.6,ID number is 110101199001011234" --output .\video_report.json
```

Create a visual-only masked preview video:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_video_visual_moderation.py .\input.mp4 --provider dashscope --model qwen3-vl-flash --transcript .\input.srt --masked-output .\input_masked_preview.mp4 --output .\video_report.json
```

Create a dynamic moving mosaic from externally tracked boxes:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_video_visual_moderation.py .\input.mp4 --redactions-json .\moving_redactions.json --masked-output .\input_dynamic_masked_preview.mp4 --output .\video_report.json
```

Create NSFW-sensitive body-region mosaics with the default open-source localization path:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_video_visual_moderation.py .\input.mp4 --masked-output .\input_auto_nsfw_masked_preview.mp4 --output .\video_report.json
```

For production-style gating, use NudeNet detections as primary evidence. Pose fallback already defaults to `when-nsfw`, so it only adds pose-derived boxes when the policy or VLM already says the frame is NSFW:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_video_visual_moderation.py .\input.mp4 --provider dashscope --model qwen3-vl-flash --auto-nsfw-pose-fallback when-nsfw --masked-output .\input_auto_masked_preview.mp4 --output .\video_report.json
```

Run a real image through DashScope Qwen-VL:

```powershell
$env:DASHSCOPE_API_KEY="..."
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_visual_moderation.py .\frame.jpg --provider dashscope --model qwen3-vl-flash
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_visual_moderation.py .\frame.jpg --provider dashscope --model qwen3-vl-plus
```

Create masked image copies when the decision contains visual redaction targets:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_visual_moderation.py .\frame.jpg --provider dashscope --model qwen3-vl-flash --mask-output-dir .\masked
```

The image script uses standard library HTTP calls and optional LangGraph orchestration. If `langgraph` is unavailable, it falls back to the same sequential node order. The video script uses OpenCV for sampling and preview masking, including dynamic `bbox_keyframes` for moving mosaic targets. Default NSFW localization uses NudeNet for exposed/suggestive body-part candidates, MediaPipe Pose for chest/torso/pelvis constraints or gated fallback, and simple track smoothing to avoid fixed or jittery mosaics. OpenCV preview videos are visual-only; backend production export should translate `ffmpeg_plan` into ffmpeg filters to preserve audio and apply `audio_mute` spans. Never hardcode API keys into the skill or backend repository.
