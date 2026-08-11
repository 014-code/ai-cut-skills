---
name: aivideoeditor-visual-moderation
description: Aliyun Green CIP video violation reporting, result-based local routing, open-source ASR transcript generation, and keyword-based source-video redaction for AIVideoEditor. Use when Codex needs to submit videos to 阿里云视频审核增强版 / Green VideoModeration, return concrete violation labels and hit timestamps for political, military, and NSFW risks, copy or move reviewed local videos into pass/fail folders, generate higher-accuracy dialogue transcripts with FunASR/Whisper, or simulate/business-rule-plan subtitle keyword masking with synchronized audio muting from timestamped dialogue/subtitle text.
---

# AIVideoEditor Violation Detection

Use this skill for provider-backed violation detection reports, open-source ASR transcript generation, and local text/audio redaction simulations.

Provider-backed video review workflow:

1. Submit the video to Aliyun Green CIP.
2. Wait for the provider result.
3. Normalize Aliyun frame labels into the scoped categories: `political`, `military`, `nsfw`.
4. Return concrete evidence with provider label, description, confidence, and timestamp.
5. If `--route-dir` is provided for a local video, place `PASS` videos under `过了` and non-pass videos under `没过`.
6. Write a moderation gate: only `decision.action == "PASS"` sets `downstream_gate.allow_short_drama_editing=true`.
7. Write sidecar audit logs next to routed videos. Failed logs must explain hit timestamps, labels, categories, confidence, and reasons.
8. Stop after the report and optional routing. Do not run any downstream processing unless the user explicitly asks for a separate workflow.

Business-keyword subtitle/audio workflow:

1. Prepare timestamped dialogue/subtitle segments as JSON, or generate them with `scripts/run_high_accuracy_asr.py`.
2. Refresh the Feishu policy snapshot from the configured `素材尺度规范` wiki before every batch through the official Feishu Open API. Do not use browser automation for policy reading. Keep the URL, document title, last-modified value, sync time, API source, and policy version in `references/feishu_keyword_policy.json`; never store the Feishu password, app secret, access token, or browser tokens.
3. Run `scripts/run_keyword_text_audio_redaction.py` or `scripts/run_keyword_video_redaction.py`.
4. Mask every hit from the current Feishu-backed keyword policy in subtitles.
5. Mute the matching dialogue span for every subtitle-masked hit.
6. Return a machine-readable `redaction_plan.json` and a human-readable `字幕消音模拟说明.txt`.

For the complete keyword policy, read `references/keyword_text_audio_redaction.md`.

The keyword workflow produces:

- `字幕消音模拟说明.txt`
- `redaction_plan.json`
- `原始字幕预览.mp4`
- `字幕打码后预览.mp4`
- `原始音频示例.wav`
- `消音后音频示例.wav`

The simulation rule is:

- apply `subtitle_mask` to every keyword hit
- apply `audio_mute` to every `subtitle_mask` hit
- derive audio mute spans from the exact masked character range inside each rendered subtitle; do not mute the full subtitle line or raw transcript segment
- map `char_start` / `char_end` proportionally into the OCR-visible subtitle time window, with only a small audio safety pad; use finer word timestamps when provided
- prefer word-level timing when the transcript includes `words`, `tokens`, or `frontend.words`
- keep every keyword occurrence by span; do not dedupe repeated hits just because the keyword text is the same
- keep a small subtitle pre-roll so the first visible frame is covered immediately
- choose a fixed anchor OCR keyword box for each hit so truncated OCR text cannot move the mask to a different character position across adjacent frames
- always run full-video OCR at a fixed interval (default 0.30 seconds), then refine OCR text transitions with frame-rate boundary scans whenever either adjacent subtitle observation contains a policy hit; ASR is a timing aid, never the OCR coverage boundary
- merge adjacent OCR hits for the same keyword into one continuous time window with a small temporal hold, so a subtitle does not become unmasked between sampled frames
- refine OCR hit boundaries against the preceding and following subtitle observations: extend across an empty/continuing subtitle sample when needed, but stop before a different sentence; extensions remain limited to the matched keyword bbox
- derive each mask from the OCR line box and the matched character span; never use a full-frame, whole-person, or full subtitle-band mask as a leak-prevention fallback
- render audio mutes by trimming, zeroing, and concatenating audio segments; do not rely on ffmpeg `enable=between(t,...)` for later timestamps
- keyword matching is normalized for spaces and punctuation, so OCR/transcript variants like `装 13` and `：tmd` still hit the canonical policy term

## Required Inputs

For Aliyun provider review:

- Aliyun video moderation credentials: `ALIBABA_CLOUD_ACCESS_KEY_ID` and `ALIBABA_CLOUD_ACCESS_KEY_SECRET`
- One input target: `--video` or `--url`
- Optional routing target: `--route-dir`
- Optional runtime overrides only when the user explicitly wants them: `--region-id`, `--endpoint`, `--poll`, `--include-audio`, `--route-mode`

Do not ask for a multimodal model key for this skill. This workflow is provider-only and does not depend on DashScope/Qwen credentials.

For keyword subtitle/audio redaction:

- Feishu Open API credentials: `FEISHU_ACCESS_TOKEN`, or both `FEISHU_APP_ID` and `FEISHU_APP_SECRET`.
- For an externally shared/personal Wiki document that cannot add an application as a collaborator, use the user OAuth flow in `scripts/sync_feishu_keyword_policy_oauth.py`; the signed-in user's read permission is used and the temporary user token is never written to disk.
- Feishu Wiki URL: `FEISHU_KEYWORD_POLICY_URL`, or the default `素材尺度规范` URL.
- Timestamped dialogue/subtitle JSON, or omit `--transcript` to run the built-in demo.
- For source-video redaction, omit `--transcript` to auto-generate one with `run_high_accuracy_asr.py` before keyword matching.
- If available, `words` / `tokens` / `frontend.words` word-level timestamps are consumed automatically for tighter audio mute spans and better subtitle timing.
- Output directory: `--output-dir`.
- Policy snapshot: `references/feishu_keyword_policy.json` by default, or pass `--policy-json` explicitly.
- `rapidocr_onnxruntime` must be available for precise OCR subtitle-line localization.
- Do not require Aliyun, DashScope, or Qwen credentials for this keyword workflow.
- A live policy refresh requires Feishu credentials and is the default when `--policy-json` is omitted; use `--no-refresh-feishu-policy` only for an explicit offline/reproducible run.

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

Run keyword subtitle masking and synchronized audio mute planning:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\sync_feishu_keyword_policy.py

python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_keyword_text_audio_redaction.py `
  --transcript D:\path\dialogue_segments.json `
  --refresh-feishu-policy `
  --output-dir D:\path\keyword_redaction_output
```

Run open-source ASR transcript generation:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_high_accuracy_asr.py `
  --input D:\path\source.mp4 `
  --output D:\path\asr\source_transcript.json
```

Run actual source-video keyword masking and synchronized audio mute:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_keyword_video_redaction.py `
  --video D:\path\source.mp4 `
  --output D:\path\processed.mp4 `
  --refresh-feishu-policy `
  --output-dir D:\path\keyword_video_redaction_output
```

`sync_feishu_keyword_policy.py` uses only official APIs: `wiki/v2/spaces/get_node` resolves the Wiki node and `docx/v1/documents/{document_id}/blocks` reads the cloud document blocks. It uses `FEISHU_ACCESS_TOKEN` when supplied, otherwise obtains a tenant token from `FEISHU_APP_ID` and `FEISHU_APP_SECRET`. The snapshot is written atomically only after all four required keyword groups are parsed successfully.

For an external/personal Wiki node where tenant-token access returns `131006 node permission denied`, configure the app's OAuth redirect URL (default `http://127.0.0.1:8765/callback`) in the Feishu developer console, then run:

```powershell
$env:FEISHU_APP_ID = "..."
$env:FEISHU_APP_SECRET = "..."
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\sync_feishu_keyword_policy_oauth.py
```

The script opens the official Feishu authorization page with user scopes `wiki:wiki:readonly docx:document:readonly`, validates `state`, exchanges the one-time code at `authen/v2/oauth/token`, and passes the user access token directly to the same Wiki/Docx reader. It does not persist the access token or refresh token. Configure these user-identity permissions in the developer console; application-identity permissions alone do not make them valid OAuth scopes. The redirect URL must exactly match the one configured in the app.

The actual source-video workflow writes:

- `redaction_plan.json`
- `keyword_video_redaction_report.json`
- `字幕消音处理说明.txt`
- the processed MP4 at `--output`

Default subtitle masking uses RapidOCR on a bottom subtitle search band, then narrows the mask to the hit character span inside the recognized subtitle line. Subtitle masks use step/hold keyframes with a fixed anchor OCR keyword box for each hit, not linear interpolation, so truncated OCR text cannot move the mask across the line and masks appear immediately. The fallback `--subtitle-bbox` only applies when OCR/text-region localization fails.
The source-video workflow always performs a full-video RapidOCR scan at a 0.30-second interval, then frame-scans the boundary between adjacent observations when the subtitle text or hit set changes. OCR hits are merged across adjacent frames and used to supplement or extend ASR hits, which prevents missed words outside the ASR transcript or during short subtitle flashes. The report records coarse scan count, adaptive boundary scan count, OCR supplement windows, and localization fallback counts.
If the transcript includes word-level timestamps, the actual source-video workflow tightens each keyword's time window before rendering and uses those word spans for audio mute instead of the whole subtitle segment.

Run the built-in simulation demo:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_keyword_text_audio_redaction.py `
  --output-dir D:\path\keyword_redaction_demo
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
