# Standalone CLI

Use `scripts/usergrowth_upload.py` when the user wants the skill itself to perform UserGrowth planning or upload. The script vendors the UserGrowth automation package inside this skill, so it does not import from the project repo at runtime.

## Install Runtime Dependencies

Use the Python environment that will run the automation:

```powershell
python -m pip install -r C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\requirements.txt
python -m playwright install chromium
```

The live browser flow prefers local Edge/Chrome channels, but installing Chromium is still a useful fallback for Playwright environments.

## Dry-Run With Explicit Videos

```powershell
$script = 'C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py'
$argList = @(
    $script,
    '--video-folder', 'D:\path\videos',
    '--video', 'dxzc-001-汽水音乐-LUNA_金币音乐新-歌曲A.mp4',
    '--video', 'subfolder\dxzc-002-汽水音乐-LUNA_金币音乐旧-歌曲B.mp4',
    '--backfill-excel', 'D:\path\backfill.xlsx',
    '--song-excel', 'D:\path\songs.xlsx',
    '--output-root', 'D:\path\outputs',
    '--order-id', '123456',
    '--task-name', 'usergrowth_selected',
    '--month-tag', '26年7月dxqs'
)
& python @argList
```

Dry-run writes `<output-root>/<timestamp>_<task-name>/result.xlsx`, `task.json`, and `run.log`. It does not open the browser.

On failure after a task folder is created, read `<output-root>/<timestamp>_<task-name>/error.json` and `error.log`. On early CLI failures such as unmatched video selectors, check stderr and `<output-root>/_cli_errors/` when `output_root` was available.

## Selectors

Video selection supports:

- `--video <absolute path>`
- `--video <relative path under video-folder>`
- `--video <exact file name>`
- `--video <file stem without suffix>`
- `--video-glob '*金币音乐新*.mp4'`
- `--video-list selected.txt`, one selector per line
- `--all-videos`, explicit opt-in to scan everything

If a selector does not match, the script fails instead of silently uploading the wrong set.

## Auto Split By Song

Use `--split-by-song` when a folder or selected video set contains multiple songs. The CLI first groups videos with the same song into one batch per song, then runs those batches through the concurrent batch runner.

```powershell
& python 'C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py' `
  --video-folder 'D:\path\videos' `
  --all-videos `
  --split-by-song `
  --backfill-excel 'D:\path\backfill.xlsx' `
  --song-excel 'D:\path\songs.xlsx' `
  --output-root 'D:\path\outputs' `
  --order-id '123456'
```

Manifest equivalent:

```json
{
  "video_folder": "D:/path/videos",
  "all_videos": true,
  "split_by_song": true,
  "backfill_excel": "D:/path/backfill.xlsx",
  "song_excel": "D:/path/songs.xlsx",
  "output_root": "D:/path/outputs",
  "order_id": "123456",
  "dry_run": true
}
```

If `--split-by-song` is used without `--video`, `--video-glob`, `--video-list`, or `--all-videos`, it scans all videos in `video_folder`, matching the desktop auto-split behavior.

## Manifest

For repeated tasks, create a JSON manifest:

```json
{
  "video_folder": "D:/path/videos",
  "videos": [
    "dxzc-001-汽水音乐-LUNA_金币音乐新-歌曲A.mp4",
    "subfolder/dxzc-002-汽水音乐-LUNA_金币音乐旧-歌曲B.mp4"
  ],
  "backfill_excel": "D:/path/backfill.xlsx",
  "song_excel": "D:/path/songs.xlsx",
  "output_root": "D:/path/outputs",
  "order_id": "123456",
  "task_name": "usergrowth_selected",
  "month_tag": "26年7月dxqs",
  "custom_tag_template_name": "单曲模板",
  "custom_tag_template_fixed_tags": [
    "未成年人已授权",
    "影视版权已授权",
    "dxzc",
    "汽水音乐",
    "{月份标签}",
    "{歌曲ID}"
  ],
  "custom_tag_template_optional_tags": [],
  "recursive": true,
  "dry_run": true
}
```

Run it:

```powershell
& python 'C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py' --manifest 'D:\path\manifest.json'
```

Do not put passwords in manifests unless the user explicitly asks for that storage pattern. Prefer environment variables.

## Multi-Batch Manifest

To run multiple independent batches like the desktop queue, put `batches` at the top level. Shared fields such as `backfill_excel`, `song_excel`, `output_root`, `month_tag`, custom tag template fields, `dry_run`, and retry/browser settings can live at the top level; each batch can override them.

```json
{
  "backfill_excel": "D:/path/backfill.xlsx",
  "song_excel": "D:/path/songs.xlsx",
  "output_root": "D:/path/outputs",
  "task_name": "usergrowth_batches",
  "month_tag": "26年7月dxqs",
  "custom_tag_template_name": "单曲模板",
  "custom_tag_template_fixed_tags": ["未成年人已授权", "影视版权已授权", "dxzc", "汽水音乐", "{月份标签}", "{歌曲ID}"],
  "custom_tag_template_optional_tags": [],
  "dry_run": true,
  "recursive": true,
  "concurrency": 3,
  "batches": [
    {
      "name": "order_a_selected",
      "video_folder": "D:/path/videos_a",
      "order_id": "OrderA",
      "videos": [
        "dxzc-001-汽水音乐-LUNA_单曲-歌曲A.mp4"
      ]
    },
    {
      "name": "order_b_folder",
      "video_folder": "D:/path/videos_b",
      "order_id": "OrderB",
      "all_videos": true
    }
  ]
}
```

Run it:

```powershell
& python 'C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py' `
  --manifest 'D:\path\batch_manifest.json' `
  --concurrency 3
```

Batch mode writes a total summary to `<output-root>/batch_runs/<timestamp>_<task-name>/batch_summary.json` and `run.log`. Each child batch still writes its own `<output-root>/<timestamp>_<batch-task-name>/task.json`, `run.log`, `result.xlsx` in dry-run mode, and `debug/` in live mode.

Multi-batch execution always runs in parallel when there is more than one batch. If `concurrency` is omitted, it defaults to the number of batches, capped at 10. Even if `concurrency` is set to `1`, a run with two or more batches is lifted to at least `2` workers.

In the desktop app, the automatic song splitter produces the same shape conceptually: one batch per recognized song, with explicit selected video paths for that song. The browser layer still fills the first chameleon card and uses `一键复用`, so different songs should be split before live upload.

## Live Upload

Live upload writes successful orders directly back to the original backfill Excel and submits review on UserGrowth. Only run live after explicit user confirmation:

```powershell
$env:USERGROWTH_ACCOUNT = '<account>'
$env:USERGROWTH_PASSWORD = '<password>'
& python 'C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py' `
  --manifest 'D:\path\manifest.json' `
  --live `
  --confirm-live
```

For batch live upload, keep `confirm_live` at the top level or pass `--confirm-live`. Top-level `live=true` makes batches live unless a batch explicitly sets `dry_run=true`; command-line `--live` is a global override and makes every batch live.

Use `--headless` only after visible browser mode has been validated.

## Redfruit Manual Overrides

For redfruit short-drama batches, filename parsing remains the default. When the user gives explicit labels for a batch, pass overrides instead of renaming files:

```powershell
& python 'C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py' `
  --workflow redfruit_short_drama `
  --video-folder 'D:\path\redfruit' `
  --all-videos `
  --order-id 'BKvN5' `
  --redfruit-bid-map '{"四小姐不装了":"bid_7666011819416226840"}' `
  --redfruit-default-genre '宫斗宅斗' `
  --redfruit-layout-override '竖版-横改竖' `
  --redfruit-material-mode-override 'AI前贴' `
  --redfruit-ai-custom-tag '创新AI素材' `
  --redfruit-extra-custom-tag '漫剧AI前贴'
```

Manifest equivalents are `redfruit_layout_override`, `redfruit_material_mode_override`, `redfruit_ai_custom_tag`, and `redfruit_extra_custom_tags`.

## Existing Creative Unit Recovery

When the platform reports that a file was uploaded before and provides the original creative-unit IDs, run direct recovery with repeated `--existing-creative-unit-id`. This path searches the order's creative-unit list, selects the IDs across pages, and continues through 录入素材, review, ARLP, and redfruit post-review classification. It does not upload source files or create new creative units.

This mode is redfruit-only and requires `--live --confirm-live`. Pass the batch metadata explicitly:

```powershell
& python 'C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py' `
  --output-root 'D:\path\outputs' `
  --task-name 'redfruit_existing_units' `
  --workflow redfruit_short_drama `
  --order-id 'BKvN5' `
  --live `
  --confirm-live `
  --existing-creative-unit-title '剧目名称' `
  --existing-creative-unit-drama-type '动态漫' `
  --existing-creative-unit-bid 'bid_1234567890123456789' `
  --redfruit-default-genre '古风言情' `
  --redfruit-layout-override '竖版-纯竖版' `
  --redfruit-material-mode-override 'AI前/后贴' `
  --redfruit-ai-custom-tag '创新AI素材' `
  --redfruit-extra-custom-tag 'lh' `
  --redfruit-extra-custom-tag '漫剧AI前贴' `
  --existing-creative-unit-id 'Ab7DRpk' `
  --existing-creative-unit-id 'j4REY0N'
```

The same IDs can be supplied as `existing_creative_unit_ids` in a manifest. The run writes the normal `task.json`, `run.log`, `debug/run.log`, and error artifacts under the task folder.
