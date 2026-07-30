---
name: aivideoeditor-visual-moderation
description: Aliyun Green CIP video violation reporting for AIVideoEditor. Use when Codex needs to submit videos to 阿里云视频审核增强版 / Green VideoModeration and return concrete violation labels, readable violation names, and hit timestamps for real political-sensitive, real military-sensitive, and NSFW risks.
---

# AIVideoEditor Violation Detection

Use this skill only for provider-backed violation detection reports. The current workflow is:

1. Submit the video to Aliyun Green CIP.
2. Wait for the provider result.
3. Normalize Aliyun frame labels into the scoped categories: `political`, `military`, `nsfw`.
4. Return concrete evidence with provider label, description, confidence, and timestamp.
5. Stop after the report. Do not run any downstream processing unless the user explicitly asks for a separate workflow.

## Required Inputs

- Aliyun video moderation credentials: `ALIBABA_CLOUD_ACCESS_KEY_ID` and `ALIBABA_CLOUD_ACCESS_KEY_SECRET`
- One input target: `--video` or `--url`
- Optional runtime overrides only when the user explicitly wants them: `--region-id`, `--endpoint`, `--poll`, `--include-audio`

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
  }
}
```

For Aliyun SDK details, read `references/aliyun_green_video.md`.
