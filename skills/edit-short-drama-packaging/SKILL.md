---
name: edit-short-drama-packaging
description: Package a moderation-passed short-drama video for distribution while preserving story order and existing subtitles. Use only sources from the visual moderation `过了` folder or reports/logs with `downstream_gate.allow_short_drama_editing=true`; refuse `没过` sources. Audit and preserve or add the drama title, position a free-viewing benefit below it, add only missing fictional/AI notices without background bars, remove an existing source end card, and append the matching portrait or landscape tail board. Use for 短剧包装剪辑、剧名补充、免费利益点、风险提示语、AI生成提示语、原尾板替换、横竖屏尾板拼接或单视频轻剪交付。
---

# Edit Short Drama Packaging

Create one delivery-ready MP4 while preserving the source story, dialogue, embedded subtitles, and timing.

## Moderation Gate

Before any short-drama packaging/editing, verify the source passed `aivideoeditor-visual-moderation`:

- Accept only files from the moderation `过了` folder; or a report/sidecar log where `downstream_gate.allow_short_drama_editing == true`.
- Reject every file from `没过`. Do not trim, package, re-encode, or otherwise continue with failed material.
- If a failed source is encountered, report the sidecar `审核说明.txt` / `.audit.json` path and summarize its failed timestamps and reasons.
- Treat both `REVIEW` and `BLOCK` as not allowed for downstream short-drama editing.

## Workflow

1. Verify the moderation gate. Stop unless the source is in `过了` or has `downstream_gate.allow_short_drama_editing == true`.
2. Probe the source and every candidate tail board with `ffprobe`.
3. Extract and visually inspect opening, middle, and late-body frames before rendering.
4. Audit the title in source-pixel coordinates:
   - If the original title is visible, preserve it, record its lower edge, and use `--source-title-present --title-bottom <px>`.
   - If no title is visible, add the authoritative original title with `--title-text "《剧名》"`.
   - Resolve the title from task input, trusted metadata, or an unambiguous filename, in that order. Never invent a title; request it when it cannot be confirmed.
5. Audit fictional and AI notices independently:
   - Declare an existing fictional notice with `--source-risk-present`; otherwise provide `--risk-text`.
   - Declare an existing AI notice with `--source-ai-present`; otherwise provide `--ai-text`.
   - Add only missing meaning. Never repeat an existing notice.
6. Position the benefit below the preserved or newly added title. Keep at least 1% of frame height, and normally 10–20px on a 1080×1920 canvas, between their visible bounds.
7. Render added notices as white text with a 1–2px black outline, no rectangle, no translucent backing layer, and zero shadow.
8. Review the final 6–10 seconds frame by frame. If an original promotional end card exists, cut at its first frame without removing the preceding story shot.
9. Append the orientation-matched tail board. Do not overlay body copy on the tail board.
10. Fully decode-check and visually inspect the opening, middle, final story frame, and tail-board transition.

## Overlay Audit Contract

Provide exactly one choice from every row:

| Content | Source already has it | Source is missing it |
| --- | --- | --- |
| Drama title | `--source-title-present --title-bottom <px>` | `--title-text "《原剧名》"` |
| Fiction notice | `--source-risk-present` | `--risk-text "本故事纯属虚构"` |
| AI notice | `--source-ai-present` | `--ai-text "本故事由AI生成"` |

The renderer rejects an incomplete decision. `ai-video` or filename metadata is not sufficient evidence that a notice is visibly burned into the body; inspect frames.

When adding a title, the renderer places the benefit below the new title automatically. When preserving a source title, measure `--title-bottom` from reviewed frames. Use `--benefit-y` only for a deliberate override; values that collide with the title are rejected.

The renderer caps added title, benefit, and notice font sizes to the available canvas width. Explicit font-size overrides may make text smaller but cannot force it beyond the horizontal safe margin.

If an existing lower notice occupies the default added-notice position, use `--notice-y <px>` to place the missing notice above or below it without covering dialogue subtitles.

## Tail Review

Generate a temporary contact sheet when the source-tail boundary is unknown:

```bash
ffmpeg -hide_banner -loglevel error \
  -ss <duration-minus-8> -i <source.mp4> -t 8 \
  -vf "fps=4,scale=360:-1,tile=8x4:padding=4:margin=6" \
  -frames:v 1 <tail-review.jpg>
```

Refine the first end-card frame and pass it as `--cut-at`.

## Render

For a source that already contains the title and `本故事纯属虚构`, but lacks an AI notice:

```bash
python scripts/package_short_drama.py \
  <source.mp4> \
  --tailboard-dir <尾板目录> \
  --cut-at <原尾板首帧秒数> \
  --source-title-present \
  --title-bottom <剧名底边像素> \
  --benefit-text "0元免费看全集" \
  --source-risk-present \
  --ai-text "本故事由AI生成" \
  --output <成片.mp4>
```

If the source has no title, replace the two title arguments with:

```bash
--title-text "《原剧名》"
```

Useful overrides:

- `--tailboard <file>`: choose a specific tail board.
- `--font-file <ttf/otf/ttc>`: use a known local font.
- `--title-font-size`, `--benefit-font-size`, and `--notice-font-size`: override proportional sizing.
- `--title-y`, `--benefit-y`, and `--notice-y`: override audited pixel positions.
- `--cut-at` omitted: retain the complete source body before appending the tail board.
- `--overwrite`: replace an existing output intentionally.

The script writes `<output>.json` with the source-overlay audit, added copy, title/benefit positions, notice style, cut point, selected tail board, expected duration, probe result, and validation status.

## Acceptance

Require all of the following:

- Source came from the moderation `过了` folder or has `downstream_gate.allow_short_drama_editing == true`.
- Preserve the original story order and embedded subtitles.
- Show the authoritative drama title exactly once: preserve it when present; add it when absent.
- Keep the benefit below the title with visible separation and no overlap.
- Present fictional and AI meaning exactly once each across source and added overlays.
- Use no black rectangle or translucent backing layer behind added notices; retain only a thin black outline and zero shadow.
- Keep added notices clear of dialogue subtitles, existing notices, and important faces.
- Remove every frame of the replaced original end card.
- Match the tail board to source orientation and fill the canvas without distortion.
- Produce H.264 video and AAC stereo audio with matching durations and no decode errors.
