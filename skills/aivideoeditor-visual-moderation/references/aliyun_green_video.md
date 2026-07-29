# Aliyun Green Video Moderation

Use this reference for 阿里云视频审核增强版 / Green CIP video review.

## Entrypoints

- `scripts/run_aliyun_video_moderation.py`: submit/query a local video or URL and normalize Aliyun result into the skill decision schema.
- `scripts/run_aliyun_review_mask_rereview.py`: run `review -> scoped redactions -> masked video -> re-review`.

## Credentials

Set credentials only through environment variables:

```powershell
$env:ALIBABA_CLOUD_ACCESS_KEY_ID = "..."
$env:ALIBABA_CLOUD_ACCESS_KEY_SECRET = "..."
```

Never hardcode credentials in skill files, manifests, reports, or backend code.

## Dependencies

Aliyun live calls need:

- `alibabacloud_green20220302`
- `alibabacloud_tea_openapi`
- `alibabacloud_tea_util`
- `oss2`

The review-mask-rereview script also needs `ffmpeg`. It auto-detects `ffmpeg` from `PATH` or `material_remix_desktop_source/bin/ffmpeg.exe` when run from the backend repo. Pass `--ffmpeg` if needed.

## Visual-Only Default

Both scripts default to business-scoped visual-only normalization:

- Aliyun frame/video labels are mapped into `military`, `id_document`, and `nsfw`.
- Aliyun audio/dialogue hits are ignored unless `--include-audio` is provided.
- The rereview loop triggers on the scoped business decision by default, not raw Aliyun provider action.

Use `--include-audio` only when the user asks for audio mute/subtitle replacement. Use `--trigger-on-raw` only when the user wants to process every raw Aliyun non-PASS result.

## Commands

Single review:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_aliyun_video_moderation.py `
  --video D:\path\input.mp4 `
  --poll `
  --output D:\path\aliyun_green_report.json
```

Review from URL:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_aliyun_video_moderation.py `
  --url "https://example.com/input.mp4" `
  --poll `
  --output D:\path\aliyun_green_report.json
```

Review-mask-rereview:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_aliyun_review_mask_rereview.py `
  --video D:\path\input.mp4 `
  --output-dir D:\path\aliyun_rereview `
  --output D:\path\aliyun_rereview\flow_report.json
```

Reuse an existing first review:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_aliyun_review_mask_rereview.py `
  --video D:\path\input.mp4 `
  --initial-report D:\path\aliyun_green_report.json `
  --output-dir D:\path\aliyun_rereview `
  --output D:\path\aliyun_rereview\flow_report.json
```

## Output Notes

`run_aliyun_video_moderation.py` writes:

- `provider`: `aliyun_green_cip`
- `audio_policy`: `ignored_visual_only` or `included`
- `task_id`
- scrubbed `submitted` and `result`
- normalized `decision`

`run_aliyun_review_mask_rereview.py` writes:

- `initial` review
- scoped `redactions`
- optional `processed` video paths
- optional `rereview`
- `trigger_policy`

Temporary Aliyun upload URLs and tokens are scrubbed from reports.
