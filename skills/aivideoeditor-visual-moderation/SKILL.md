---
name: aivideoeditor-visual-moderation
description: Aliyun Green CIP video violation reporting and result-based local routing for AIVideoEditor. Use when Codex needs to submit videos to 阿里云视频审核增强版 / Green VideoModeration, return concrete violation labels, readable violation names, and hit timestamps for real political-sensitive, real military-sensitive, and NSFW risks, and optionally copy or move reviewed local videos into pass/fail folders.
---

# AIVideoEditor Violation Detection

Use this skill only for provider-backed violation detection reports. The current workflow is:

1. Submit the video to Aliyun Green CIP.
2. Wait for the provider result.
3. Normalize Aliyun frame labels into the scoped categories: `political`, `military`, `nsfw`.
4. Return concrete evidence with provider label, description, confidence, and timestamp.
5. If `--route-dir` is provided for a local video, place `PASS` videos under `过了` and non-pass videos under `没过`.
6. Write a moderation gate: only `decision.action == "PASS"` sets `downstream_gate.allow_short_drama_editing=true`.
7. Write sidecar audit logs next to routed videos. Failed logs must explain hit timestamps, labels, categories, confidence, and reasons.
8. Stop after the report and optional routing. Do not run any downstream processing unless the user explicitly asks for a separate workflow.

## Required Inputs

- Aliyun video moderation credentials: `ALIBABA_CLOUD_ACCESS_KEY_ID` and `ALIBABA_CLOUD_ACCESS_KEY_SECRET`
- One input target: `--video` or `--url`
- Optional routing target: `--route-dir`
- Optional runtime overrides only when the user explicitly wants them: `--region-id`, `--endpoint`, `--poll`, `--include-audio`, `--route-mode`

Do not ask for a multimodal model key for this skill. This workflow is provider-only and does not depend on DashScope/Qwen credentials.

## Evidence Rules

- Keep Aliyun raw labels and descriptions in the report for traceability.
- Keep provider timestamps from `FrameResult.Frames[].Offset` as seconds.
- Preserve the cleavage/groove hit as a distinct evidence name: Aliyun `sexual_cleavage` or descriptions containing `乳沟` must be reported as `乳沟`, not generalized into `胸部`.
- Ignore unrelated provider labels that do not map to `political`, `military`, or `nsfw`; keep them only under unscoped evidence for audit.
- Default is visual-only. Do not include audio/dialogue evidence unless the user explicitly asks for it.
- Never hardcode credentials in this skill, reports, or backend code.

## Command

Set credentials through environment variables:

```powershell
$env:ALIBABA_CLOUD_ACCESS_KEY_ID = "..."
$env:ALIBABA_CLOUD_ACCESS_KEY_SECRET = "..."
```

Run a local video review:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_aliyun_video_moderation.py `
  --video D:\path\input.mp4 `
  --poll `
  --output D:\path\aliyun_green_report.json
```

Run a local video review and route by result:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_aliyun_video_moderation.py `
  --video D:\path\input.mp4 `
  --poll `
  --output D:\path\aliyun_green_report.json `
  --route-dir D:\path\reviewed
```

Routing defaults to copy mode. The script creates:

- `D:\path\reviewed\过了`: `decision.action == "PASS"`
- `D:\path\reviewed\没过`: `decision.action == "REVIEW"` or `decision.action == "BLOCK"`
- `*.audit.json` and `审核说明.txt` next to each routed video. For `没过`, these logs are the handoff artifact for why the video cannot continue.

Use `--route-mode move` only when the original local video should be moved instead of copied.

Downstream short-drama editing must consume only:

- files under the routed `过了` folder; or
- reports/logs where `downstream_gate.allow_short_drama_editing == true`.

Never feed files from `没过` into later short-drama packaging/editing. Treat both `REVIEW` and `BLOCK` as blocked for downstream work.

Run a URL review:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_aliyun_video_moderation.py `
  --url "https://example.com/input.mp4" `
  --poll `
  --output D:\path\aliyun_green_report.json
```

## Output Fields

`decision` keeps the stable high-level fields and adds report-friendly violation fields:

- `action`: `PASS`, `REVIEW`, or `BLOCK`.
- `categories`: scoped hit categories.
- `confidence`: strongest scoped confidence.
- `violation_points`: one item per hit, including `name`, `label`, `description`, `service`, `time_seconds`, and `confidence`.
- `violation_groups`: grouped hits by provider label and readable name, including `time_points` and merged `time_ranges`.
- `violation_summary_text`: human-readable lines such as `乳沟命中时间点: 13.5 秒、14.5 秒`.
- `evidence.provider_points`: same hit list for downstream audit.
- `evidence.provider_unscoped_hits`: Aliyun labels that were not mapped to the current business scope.
- `routing`: optional file routing result when `--route-dir` is provided.
- `downstream_gate`: stable pass/fail gate for later short-drama editing.
- `gate_log`: optional sidecar audit log paths when `--route-dir` is provided.

Example shape:

```json
{
  "decision": {
    "action": "BLOCK",
    "categories": ["nsfw"],
    "confidence": 0.9694,
    "violation_points": [
      {
        "source": "aliyun_green_video",
        "modality": "frame",
        "category": "nsfw",
        "category_name": "色情/低俗",
        "name": "乳沟",
        "label": "sexual_cleavage",
        "description": "女性乳沟",
        "service": "liveStreamCheck",
        "time_seconds": 13.5,
        "confidence": 0.9455
      }
    ],
    "violation_groups": [
      {
        "name": "乳沟",
        "label": "sexual_cleavage",
        "time_points": [13.5, 14.5, 16.5],
        "time_ranges": [
          {"start_time": 13.5, "end_time": 14.5},
          {"start_time": 16.5, "end_time": 16.5}
        ]
      }
    ],
    "violation_summary_text": "乳沟命中时间点: 13.5 秒、14.5 秒、16.5 秒"
  },
  "downstream_gate": {
    "allow_short_drama_editing": false,
    "status": "blocked",
    "decision_action": "BLOCK",
    "reason": "Aliyun returned BLOCK. The routed file stays in 没过 and must not enter downstream short-drama editing."
  },
  "gate_log": {
    "enabled": true,
    "json_path": "D:\\path\\reviewed\\没过\\input.audit.json",
    "text_path": "D:\\path\\reviewed\\没过\\审核说明.txt"
  }
}
```

For Aliyun SDK details, read `references/aliyun_green_video.md`.
