# Violation Report Schema

All current implementations should return this decision shape.

```json
{
  "action": "BLOCK",
  "categories": ["nsfw"],
  "confidence": 0.9694,
  "reasons": ["Aliyun video moderation riskLevel=high."],
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
      "source": "aliyun_green_video",
      "modality": "frame",
      "category": "nsfw",
      "category_name": "色情/低俗",
      "name": "乳沟",
      "label": "sexual_cleavage",
      "description": "女性乳沟",
      "service": "liveStreamCheck",
      "time_points": [13.5, 14.5, 16.5],
      "time_ranges": [
        {"start_time": 13.5, "end_time": 14.5},
        {"start_time": 16.5, "end_time": 16.5}
      ],
      "count": 3,
      "max_confidence": 0.9519
    }
  ],
  "violation_summary_text": "乳沟命中时间点: 13.5 秒、14.5 秒、16.5 秒",
  "evidence": {
    "scores": {
      "military": 0.0,
      "political": 0.0,
      "nsfw": 0.9694
    },
    "labels": ["liveStreamCheck:sexual_cleavage:女性乳沟"],
    "ocr": [],
    "dialogue": [],
    "provider_points": [],
    "provider_unscoped_hits": [],
    "vision": {
      "provider": "aliyun_green_cip",
      "risk_level": "high",
      "audio_result_present": false,
      "audio_policy": "ignored_visual_only"
    },
    "policy_hits": ["external.aliyun_green_video"]
  },
  "policy_version": "visual-moderation-baseline-2026-07-29"
}
```

## Field Rules

- `action`: one of `PASS`, `REVIEW`, `BLOCK`.
- `categories`: zero or more of `military`, `political`, `nsfw`.
- `confidence`: normalized `0.0` to `1.0` confidence for the strongest scoped hit.
- `violation_points`: one item per provider hit. Frame hits use `time_seconds`.
- `violation_groups`: grouped provider hits. Frame groups expose sorted `time_points` and merged `time_ranges`.
- `violation_summary_text`: human-readable summary, for example `乳沟命中时间点: 13.5 秒、14.5 秒`.
- `evidence.scores`: always include all three scoped categories.
- `evidence.labels`: provider labels used as evidence.
- `evidence.provider_points`: same scoped hits as `violation_points`, kept for audit.
- `evidence.provider_unscoped_hits`: provider labels that were not mapped into the current business scope.
- `policy_version`: stable policy identifier.

Do not remove fields from this schema. Additive fields are allowed when they do not change existing semantics.

## Cleavage Evidence

The cleavage/groove hit must remain distinct:

- Aliyun `sexual_cleavage` should be normalized to `name: "乳沟"`.
- Descriptions containing `乳沟` should also be normalized to `name: "乳沟"`.
- Do not rename this evidence to `胸部`, `身体`, or a broad NSFW body area.
