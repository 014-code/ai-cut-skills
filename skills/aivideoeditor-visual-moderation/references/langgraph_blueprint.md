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
2. `cv_detector`: run fast detectors or consume existing detector signals.
3. `ocr_detector`: run OCR for ID text, certificate text, or military slogans.
4. `dialogue_detector`: run subtitle/OCR/ASR text moderation for the same scoped categories.
5. `risk_gate`: decide whether the VLM node is needed.
6. `vision_reasoner`: run DashScope `qwen3-vl-flash` for fast screening, `qwen3-vl-plus` for higher-quality review, or another OpenAI-compatible VLM for contextual reasoning.
7. `fusion`: normalize detector, OCR, dialogue, and VLM signals into three category scores.
8. `policy`: apply deterministic rules and emit the final schema.
9. `redaction_planner`: convert policy hits into mosaic, subtitle replacement, or audio mute targets.
10. `audit`: persist compact evidence, prompt/model version, redaction targets, and policy version.

## Edges

```text
load_input -> cv_detector -> ocr_detector -> dialogue_detector -> risk_gate
risk_gate -> vision_reasoner -> fusion -> policy -> redaction_planner -> audit
risk_gate -> fusion -> policy -> redaction_planner -> audit
```

Skip `vision_reasoner` when all detector scores are low and there are no sensitive OCR or keyword hits.

## Prompt Contract

Ask the VLM to inspect only the scoped categories and return evidence, not the final business decision:

```text
Inspect this image for real-world concrete military-sensitive content, real ID/credential content, and sexual/NSFW visual risks.
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
