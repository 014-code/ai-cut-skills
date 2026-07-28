# Visual Moderation Output Schema

All implementations should return this stable decision shape.

```json
{
  "action": "PASS",
  "categories": [],
  "confidence": 0.74,
  "reasons": ["No scoped visual safety signals were detected."],
  "evidence": {
    "scores": {
      "military": 0.0,
      "id_document": 0.0,
      "nsfw": 0.0
    },
    "labels": [],
    "ocr": [],
    "dialogue": [],
    "vision": null,
    "policy_hits": []
  },
  "redactions": [],
  "policy_version": "visual-moderation-baseline-2026-07-28"
}
```

## Field Rules

- `action`: one of `PASS`, `REVIEW`, `BLOCK`.
- `categories`: zero or more of `military`, `id_document`, `nsfw`.
- `confidence`: normalized `0.0` to `1.0` confidence for the final action.
- `reasons`: short human-readable policy reasons.
- `evidence.scores`: always include all three scoped categories.
- `evidence.labels`: detector labels, VLM visible-object labels, or filename/mock labels.
- `evidence.ocr`: OCR snippets or VLM-read text; mask private data before long-term storage.
- `evidence.dialogue`: subtitle, ASR, burned-in subtitle OCR, title text, or user-provided transcript snippets.
- `evidence.vision`: raw compact VLM evidence when a VLM was used.
- `evidence.policy_hits`: internal rule IDs used for audit and regression tests.
- `redactions`: zero or more target instructions for mosaic, blur, subtitle replacement, or audio muting.
- `policy_version`: stable policy identifier.

Do not remove fields from this schema. Additive fields are allowed when they do not change existing semantics.

## Fixture Input Shape

The local script accepts flexible JSON fixtures like this:

```json
{
  "cv": {
    "nsfw": 0.1,
    "military": 0.92,
    "id_document": 0.0,
      "labels": ["military uniform", "rifle"],
      "ocr": [],
      "dialogue": []
  },
  "vision": {
    "categories": ["military"],
    "scores": {
      "military": 0.86
    },
    "risk_level": "review",
    "confidence": 0.86,
    "reason": "A real person appears to be wearing a military uniform."
  },
  "context": {
    "source": "frame_sample"
  }
}
```

## Redaction Shape

```json
{
  "type": "text_mosaic",
  "category": "id_document",
  "reason": "OCR contains an ID number.",
  "start_time": 12.4,
  "end_time": 15.2,
  "bbox": [0.12, 0.72, 0.88, 0.91],
  "replacement": "[已处理]"
}
```

For moving targets, use dynamic keyframes:

```json
{
  "type": "visual_mosaic",
  "category": "id_document",
  "reason": "Tracked sensitive document across frames.",
  "start_time": 1.0,
  "end_time": 4.0,
  "bbox_keyframes": [
    {"time": 1.0, "bbox": [0.12, 0.55, 0.42, 0.70]},
    {"time": 2.5, "bbox": [0.36, 0.58, 0.66, 0.73]},
    {"time": 4.0, "bbox": [0.55, 0.62, 0.86, 0.78]}
  ]
}
```

`bbox_keyframes` are normalized `[x1, y1, x2, y2]` boxes. Preview rendering linearly interpolates between keyframes so the mosaic follows moving content instead of staying fixed.

## Fixture Dialogue Shape

```json
{
  "cv": {
    "labels": [],
    "ocr": []
  },
  "dialogue": [
    {
      "start_time": 10.2,
      "end_time": 12.6,
      "text": "示例台词"
    }
  ]
}
```

## Video Report Shape

`scripts/run_video_visual_moderation.py` returns:

```json
{
  "video": "input.mp4",
  "metadata": {
    "frame_count": 8825,
    "fps": 30.0,
    "width": 720,
    "height": 1280,
    "duration": 294.167
  },
  "sampling": {
    "sample_count": 20,
    "sample_interval": null,
    "work_dir": "..."
  },
  "decision": {
    "action": "BLOCK",
    "categories": ["id_document"],
    "redactions": []
  },
  "frame_results": [],
  "transcript_results": [],
  "auto_nsfw": {
    "enabled": true
  },
  "ffmpeg_plan": {
    "audio_mutes": [],
    "visual_masks": []
  },
  "masked_output": null
}
```

`ffmpeg_plan` is the handoff surface for production video export. OpenCV preview masking supports dynamic `bbox_keyframes` but does not preserve audio.

## Auto NSFW Localization

Video moderation always runs the default NSFW localization path. The report adds an `auto_nsfw` diagnostic object. The stable decision schema is unchanged; automatic detections are merged as additive `visual_mosaic` redactions with `source: "auto_nsfw"`.

Example additive redaction:

```json
{
  "type": "visual_mosaic",
  "category": "nsfw",
  "reason": "Auto-localized NSFW-sensitive body region using nudenet; labels=FEMALE_BREAST_EXPOSED.",
  "start_time": 1.0,
  "end_time": 1.8,
  "bbox_keyframes": [
    {"time": 1.0, "bbox": [0.42, 0.31, 0.57, 0.45]},
    {"time": 1.4, "bbox": [0.43, 0.32, 0.58, 0.46]}
  ],
  "source": "auto_nsfw",
  "track_id": "nsfw_001",
  "detector_labels": ["FEMALE_BREAST_EXPOSED"],
  "detector_score": 0.88
}
```

Use NudeNet for body-part candidates, MediaPipe Pose when available for chest/torso/pelvis constraints, and track smoothing to prevent fixed or jittery mosaics. Pose fallback defaults to NSFW-gated mode (`--auto-nsfw-pose-fallback when-nsfw`) rather than masking every visible person.
