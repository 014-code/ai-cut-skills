# LangGraph Blueprint

Use LangGraph when moderation needs branching, retries, or cost control. Keep LangChain usage narrow: model wrappers, prompt templates, and structured output parsing. Keep deterministic policy outside the LLM.

## State

```python
class ModerationState(TypedDict, total=False):
    input_path: str
    context: dict
    cv: dict
    ocr: list[str]
    dialogue: list[dict]
    vision: dict
    scores: dict
    decision: dict
    redactions: list[dict]
    errors: list[str]
```

## Nodes

1. `load_input`: resolve image/frame path, storage URL, or fixture JSON.
2. `aliyun_gate`: submit the video to Aliyun Green CIP and normalize the provider result.
3. `gate_policy`: stop immediately when Aliyun scoped decision is `PASS`; continue only for `REVIEW` or `BLOCK`.
4. `cv_detector`: run fast detectors or consume existing detector signals only for failed videos.
5. `ocr_detector`: run OCR for political slogans, real-world political identifiers, or military slogans only for failed videos.
6. `dialogue_detector`: run subtitle/OCR/ASR text moderation for the same scoped categories only when explicitly enabled.
7. `vision_reasoner`: run DashScope `qwen3-vl-flash` for failed-video refinement, `qwen3-vl-plus` for higher-quality review, or another OpenAI-compatible VLM for contextual reasoning.
8. `fusion`: normalize provider, detector, OCR, dialogue, and VLM signals into three category scores.
9. `policy`: apply deterministic rules and emit the final schema.
10. `redaction_planner`: prefer VLM/localized targets and keep Aliyun time-window hits as localization hints only.
11. `rereview`: submit the processed video back to Aliyun.
12. `audit`: persist compact evidence, prompt/model version, redaction targets, and policy version.

## Edges

```text
load_input -> aliyun_gate -> gate_policy
gate_policy -> audit
gate_policy -> cv_detector -> ocr_detector -> dialogue_detector -> vision_reasoner -> fusion -> policy -> redaction_planner -> rereview -> audit
```

Skip `vision_reasoner` when Aliyun scoped decision is `PASS`. For failed videos, run VLM refinement before masking so the redaction plan can stay targeted. Never create full-frame, whole-person, or broad torso visual masks.

## Prompt Contract

Ask the VLM to inspect only the scoped categories and return evidence, not the final business decision:

```text
Inspect this image for real-world concrete military-sensitive content, real-world concrete political-sensitive content, and sexual/NSFW visual risks.
Read visible overlay text and subtitles when possible.
Return strict JSON with categories, scores, risk_level, confidence, reason, evidence, and redaction_targets.
Do not decide the final PASS/REVIEW/BLOCK policy.
```

## Backend Notes

- Use async node functions if the graph runs inside FastAPI or Celery.
- Store policy rules as versioned Python code or JSON/YAML config, not prompt text.
- Use timeout, retry, and fallback behavior around the VLM node.
- Keep `error_message` populated in worker failure paths.
- Read DashScope credentials from `DASHSCOPE_API_KEY` and `DASHSCOPE_BASE_URL`; do not store them in skill files or repo code.
- Use targeted ffmpeg filters for production redaction: mosaic/blur visual boxes, subtitle overlay replacement, and audio mute for unsafe dialogue spans.
- Use `scripts/run_video_visual_moderation.py` for local video sampling, transcript pairing, redaction planning, and visual-only preview masking before integrating the same contracts into Celery.
