---
name: edit-short-drama-packaging
description: Package a short-drama video for distribution by adding a free-viewing benefit message, fictional-content risk notice, AI-generated-content notice, removing an existing source end card, and appending the matching portrait or landscape tail board. Use for 短剧包装剪辑, 免费利益点, 风险提示语, AI生成提示语, 原尾板替换, 竖屏/横屏尾板拼接, or a single-video light-edit delivery that should preserve the original story order.
---

# Edit Short Drama Packaging

Create one delivery-ready MP4 while preserving the source story, dialogue, subtitles, and timing.

## Workflow

1. Probe the source and every candidate tail board with `ffprobe`.
2. Review the final 6–10 seconds of the source frame by frame.
3. If an original promotional end card exists, cut at its first frame. Do not remove the preceding story shot.
4. Review the body for equivalent notices already burned into the video.
5. Choose concise copy:
   - Free benefit: `0元免费看全集`, `免费看全集`, or an equivalent truthful free-viewing message.
   - Fiction risk: `本故事纯属虚构`, `剧情纯属虚构，请勿模仿`, or equivalent wording.
   - AI notice: `视频由AI生成`, `画面由AI生成`, or equivalent wording.
6. Do not duplicate a risk or AI notice that is already clearly visible throughout the body. Pass an empty value for that notice.
7. Select a readable Chinese typeface. No fixed font family or point size is required. Prefer an installed open-source or system-provided bold font and never download an unlicensed commercial font.
8. Keep the benefit message visually primary, normally at the top center. Keep notices smaller and away from dialogue subtitles and important faces.
9. Append the tail board that matches the source orientation. Do not overlay body copy on the tail board.
10. Render, fully decode-check, and visually inspect the opening, middle, final story frame, and tail-board transition.

## Tail Review

Generate a temporary contact sheet for the source ending when the cut point is not already known:

```bash
ffmpeg -hide_banner -loglevel error \
  -ss <duration-minus-8> -i <source.mp4> -t 8 \
  -vf "fps=4,scale=360:-1,tile=8x4:padding=4:margin=6" \
  -frames:v 1 <tail-review.jpg>
```

Use scene detection or individual frames to refine the boundary. Record the chosen boundary in seconds and pass it as `--cut-at`.

## Render

Run the bundled renderer:

```bash
python scripts/package_short_drama.py \
  <source.mp4> \
  --tailboard-dir <尾板目录> \
  --cut-at <原尾板首帧秒数> \
  --benefit-text "0元免费看全集" \
  --risk-text "本故事纯属虚构" \
  --ai-text "视频由AI生成" \
  --output <成片.mp4>
```

Useful overrides:

- `--tailboard <file>`: choose a specific tail board.
- `--font-file <ttf/otf/ttc>`: use a known local font.
- `--benefit-font-size` and `--notice-font-size`: override proportional sizing.
- `--risk-text ""` or `--ai-text ""`: suppress a notice already burned into the source.
- `--cut-at` omitted: retain the complete source body before appending the tail board.
- `--overwrite`: replace an existing output intentionally.

The script writes `<output>.json` with the source, cut point, selected tail board, copy, font, expected duration, actual probe result, and validation status.

## Acceptance

Require all of the following:

- Source story order and embedded subtitles remain unchanged.
- No frame from the removed original end card remains.
- The free benefit message is readable without covering the original title or dialogue subtitles.
- Fiction and AI meaning is present either in the source or added overlay.
- The tail board matches the source orientation and fills the canvas without distortion.
- Output contains H.264 video and AAC stereo audio, has matching audio/video duration, and decodes without errors.
- Final handoff reports the output path, cut point, selected tail board, copy, dimensions, duration, and validation result.
