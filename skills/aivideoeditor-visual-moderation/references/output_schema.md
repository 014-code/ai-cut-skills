# Violation Report Schema

All current implementations should return this report shape.

```json
{
  "provider": "aliyun_green_cip",
  "decision": {
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
    "redactions": [],
    "policy_version": "visual-moderation-baseline-2026-07-29"
  },
  "routing": {
    "enabled": true,
    "mode": "copy",
    "target_status": "failed",
    "source_path": "D:\\path\\input.mp4",
    "target_path": "D:\\path\\reviewed\\没过\\input.mp4",
    "route_dir": "D:\\path\\reviewed",
    "passed_dir": "D:\\path\\reviewed\\过了",
    "failed_dir": "D:\\path\\reviewed\\没过",
    "decision_action": "BLOCK",
    "allow_short_drama_editing": false,
    "gate_log": {
      "enabled": true,
      "json_path": "D:\\path\\reviewed\\没过\\input.audit.json",
      "text_path": "D:\\path\\reviewed\\没过\\审核说明.txt"
    }
  },
  "downstream_gate": {
    "allow_short_drama_editing": false,
    "status": "blocked",
    "decision_action": "BLOCK",
    "reason": "Aliyun returned BLOCK. The routed file stays in 没过 and must not enter downstream short-drama editing.",
    "policy": "Only PASS videos may feed downstream short-drama editing.",
    "categories": ["nsfw"],
    "confidence": 0.9694,
    "violation_summary_text": "乳沟命中时间点: 13.5 秒、14.5 秒、16.5 秒"
  },
  "gate_log": {
    "enabled": true,
    "json_path": "D:\\path\\reviewed\\没过\\input.audit.json",
    "text_path": "D:\\path\\reviewed\\没过\\审核说明.txt"
  }
}
```

The sidecar `审核说明.txt` for a failed video should contain human-readable lines like:

```text
审核结果: BLOCK
是否允许进入短剧剪辑: 否
源视频: D:\path\input.mp4
路由视频: D:\path\reviewed\没过\input.mp4
命中类别: nsfw
原因:
- Aliyun video moderation riskLevel=high.
命中时间点:
- 乳沟命中时间点: 13.5 秒、14.5 秒、16.5 秒
命中明细:
- 13.5 秒 | 乳沟 | 色情/低俗 | sexual_cleavage | 女性乳沟 | confidence=0.9455 | service=liveStreamCheck
```

## Field Rules

Decision fields:

- `decision.action`: one of `PASS`, `REVIEW`, `BLOCK`.
- `decision.categories`: zero or more of `military`, `political`, `nsfw`.
- `decision.confidence`: normalized `0.0` to `1.0` confidence for the strongest scoped hit.
- `decision.violation_points`: one item per provider hit. Frame hits use `time_seconds`.
- `decision.violation_groups`: grouped provider hits. Frame groups expose sorted `time_points` and merged `time_ranges`.
- `decision.violation_summary_text`: human-readable summary, for example `乳沟命中时间点: 13.5 秒、14.5 秒`.
- `decision.evidence.scores`: always include all three scoped categories.
- `decision.evidence.labels`: provider labels used as evidence.
- `decision.evidence.provider_points`: same scoped hits as `violation_points`, kept for audit.
- `decision.evidence.provider_unscoped_hits`: provider labels that were not mapped into the current business scope.
- `decision.redactions`: retained as a compatibility field. This skill does not use it as the primary output.
- `decision.policy_version`: stable policy identifier.

Routing and gate fields:

- `routing`: top-level optional object from `run_aliyun_video_moderation.py` when `--route-dir` is used. `PASS` routes to `过了`; `REVIEW` and `BLOCK` route to `没过`.
- `routing.allow_short_drama_editing`: true only for `PASS`.
- `downstream_gate.allow_short_drama_editing`: the canonical field for downstream short-drama editing. Only true may continue.
- `downstream_gate.status`: `allowed` for `PASS`, otherwise `blocked`.
- `gate_log`: sidecar log paths. Failed human-readable logs must use a Chinese filename such as `审核说明.txt`, live next to videos in `没过`, and explain failed timestamps and reasons.
- Short-drama editing must read only files from `routing.passed_dir` or reports/logs with `downstream_gate.allow_short_drama_editing == true`.

Do not remove fields from this schema. Additive fields are allowed when they do not change existing semantics.

## Cleavage Evidence

The cleavage/groove hit must remain distinct:

- Aliyun `sexual_cleavage` should be normalized to `name: "乳沟"`.
- Descriptions containing `乳沟` should also be normalized to `name: "乳沟"`.
- Do not rename this evidence to `胸部`, `身体`, or a broad NSFW body area.
