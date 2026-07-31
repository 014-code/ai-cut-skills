---
name: adxray-playlet-crawler
description: Standalone AdXRay/ADX Ray short-drama crawler and downloader for Douyin hot playlet rankings. Use when Codex needs to log in to adxray.dxylds.com, open 抖音热播榜, filter 真人AI/沙雕漫/2D漫/3D漫/解说漫/游戏编辑器漫, optionally search a drama name, open a playlet detail page, and batch download first-page material videos by highest exposure, likes, and plays for later AIVideoEditor visual moderation; downstream editing must use only moderation-passed videos from `过了` or reports with `allow_short_drama_editing=true`.
---

# AdXRay Playlet Crawler

Use this skill to crawl AdXRay Douyin hot-playlet materials and download the selected playlet's first-page videos. Reuse the existing browser-automation pattern: Playwright launch, selector fallbacks, debug snapshots, direct session download, and a JSON manifest.

Do not store credentials. Pass them as `--account` / `--password` or `ADXRAY_ACCOUNT` / `ADXRAY_PASSWORD`.

## Workflow

1. Run `scripts/adxray_playlet_crawler.py`.
2. Log in to `https://adxray.dxylds.com/`.
3. Open `https://adxray.dxylds.com/rank/distribution` or click `抖音热播榜`.
4. Expand `更多`, select `真人AI`, `沙雕漫`, `2D漫`, `3D漫`, `解说漫`, `游戏编辑器漫`, then click `确定`.
5. If `--drama-name` is provided, search it; otherwise choose the first ranked playlet.
6. Open the playlet detail page and stay on `素材筛选`.
7. For `most_exposure`, `most_likes`, and `most_plays`, click the sort label, open each first-page material, extract the modal `video.src`, and download it.
8. Write `manifest.json` and `debug/` artifacts in the output directory.
9. Hand downloaded videos to `aivideoeditor-visual-moderation` with `--route-dir`. Do not pass raw downloaded videos directly into short-drama editing.
10. Use only the moderation `过了` folder, or reports/logs with `downstream_gate.allow_short_drama_editing == true`, as downstream short-drama editing input. Treat `没过` as blocked material; keep only its audit logs for reasons and timestamps.

## Quick Commands

Smoke test:

```powershell
$env:ADXRAY_ACCOUNT = '<account>'
$env:ADXRAY_PASSWORD = '<password>'
python C:\Users\Donson\.codex\skills\adxray-playlet-crawler\scripts\adxray_playlet_crawler.py `
  --output-dir D:\downloads\adxray_smoke `
  --max-videos-per-sort 1
```

Named drama:

```powershell
python C:\Users\Donson\.codex\skills\adxray-playlet-crawler\scripts\adxray_playlet_crawler.py `
  --output-dir D:\downloads\adxray_named `
  --drama-name '离婚那天我多了个董事长妈妈'
```

Dry run:

```powershell
python C:\Users\Donson\.codex\skills\adxray-playlet-crawler\scripts\adxray_playlet_crawler.py `
  --output-dir D:\downloads\adxray_dry_run `
  --dry-run `
  --max-videos-per-sort 2
```

## Options

- `--categories`: override the six default categories.
- `--sorts`: accept `most_exposure`, `most_likes`, `most_plays`, or Chinese labels such as `最多曝光`.
- `--max-videos-per-sort`: `0` means all visible first-page videos.
- `--detail-url`: skip ranking selection and open a known `/playlet/<id>` URL directly.
- `--headless`: use only after visible-browser validation.

## Moderation Handoff

Recommended handoff after download:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_aliyun_video_moderation.py `
  --video <downloaded-video.mp4> `
  --poll `
  --output <review-root>\reports\<video-stem>.aliyun_green_report.json `
  --route-dir <review-root>
```

Only `<review-root>\过了` is an editing input directory. `<review-root>\没过` is a blocked folder and should contain sidecar `审核说明.txt` / `.audit.json` logs describing failed timestamps and reasons.

## Troubleshooting

For selector failures, read `references/adxray-flow.md` and inspect `<output-dir>/debug/`. Patch only the failed navigation or selector step.

The downloaded videos are meant to feed the visual-only moderation pipeline next. Do not add dialogue or audio moderation in this crawler.
