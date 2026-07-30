# Aliyun Green Video Moderation

Use this reference for 阿里云视频审核增强版 / Green CIP video review.

## Entrypoint

- `scripts/run_aliyun_video_moderation.py`: submit/query a local video or URL and normalize Aliyun result into the violation report schema.

## Credentials

Set credentials only through environment variables:

```powershell
$env:ALIBABA_CLOUD_ACCESS_KEY_ID = "..."
$env:ALIBABA_CLOUD_ACCESS_KEY_SECRET = "..."
```

Never hardcode credentials in skill files, manifests, reports, or backend code.

Required live-review inputs:

- `ALIBABA_CLOUD_ACCESS_KEY_ID`
- `ALIBABA_CLOUD_ACCESS_KEY_SECRET`
- one video source: `--video` or `--url`
- `--output`
- optional local-video routing root: `--route-dir`

Do not require a DashScope/Qwen or other multimodal model key for this provider-only report flow.

## Dependencies

Aliyun live calls need:

- `alibabacloud_green20220302`
- `alibabacloud_tea_openapi`
- `alibabacloud_tea_util`
- `oss2`

## Scope

The normalized report keeps only the current business scope:

- `military`
- `political`
- `nsfw`

Visual-only is the default. Audio/dialogue is not included unless the user explicitly asks for it.

## Commands

Single review:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_aliyun_video_moderation.py `
  --video D:\path\input.mp4 `
  --poll `
  --output D:\path\aliyun_green_report.json
```

Review and route a local video by result:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_aliyun_video_moderation.py `
  --video D:\path\input.mp4 `
  --poll `
  --output D:\path\aliyun_green_report.json `
  --route-dir D:\path\reviewed
```

Routing result:

- `PASS` -> `D:\path\reviewed\过了`
- `REVIEW` / `BLOCK` -> `D:\path\reviewed\没过`
- every routed video gets sidecar logs: `<video-stem>.audit.json` and `审核说明.txt`

Default routing mode is `copy`. Add `--route-mode move` only when the original local video should be moved.

Downstream short-drama editing gate:

- only `downstream_gate.allow_short_drama_editing == true` may enter later packaging/editing
- with local routing, this means use only files under `D:\path\reviewed\过了`
- never use files under `D:\path\reviewed\没过`; their audit logs are for failure explanation and manual review only

Review from URL:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_aliyun_video_moderation.py `
  --url "https://example.com/input.mp4" `
  --poll `
  --output D:\path\aliyun_green_report.json
```

## Output Notes

`run_aliyun_video_moderation.py` writes:

- `provider`: `aliyun_green_cip`
- `audio_policy`: `ignored_visual_only` or `included`
- `task_id`
- scrubbed `submitted` and `result`
- normalized `decision`
- `routing` when `--route-dir` is provided
- `downstream_gate` on every report
- `gate_log` paths when local routing writes sidecar logs

Inside `decision`, the important report fields are:

- `violation_points`
- `violation_groups`
- `violation_summary_text`

Inside `downstream_gate`, the important workflow field is:

- `allow_short_drama_editing`: true only for `decision.action == "PASS"`

For failed videos, read the sidecar `审核说明.txt` or `.audit.json` under `没过` to see the failed timestamps and reasons.

The cleavage hit is preserved as `乳沟` when Aliyun returns `sexual_cleavage` or a matching description such as `女性乳沟`.

Temporary Aliyun upload URLs and tokens are scrubbed from reports.
