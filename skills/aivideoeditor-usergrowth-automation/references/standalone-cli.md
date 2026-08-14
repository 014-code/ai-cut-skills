# Standalone CLI

Use `scripts/usergrowth_upload.py` when the user wants the skill itself to perform UserGrowth planning or upload. The script vendors the UserGrowth automation package inside this skill, so it does not import from the project repo at runtime.

## Tomato Music BID/CID Tagging

Use `scripts/tomato_music_tagging.py` after the Tomato Music upload/CID collection flow when each BID batch must be applied as a custom tag in 墨攻AI素材管理. Prefer online Feishu input through the official Wiki + Sheets OpenAPI; see `references/feishu-sheets-api.md`. Do not use browser simulation for Feishu table reads or writes.

The source is passed with `--feishu-source-url`. Pass one `--feishu-library-url` for each审核人员单曲查询表; the two library tables are merged before strict song-name-and-artist matching. Both fields must match after normalization; a missing artist or song-name-only match is excluded. A single `--feishu-library-url` remains supported for older runs.

For online Feishu access without tenant data-scope configuration, add `--feishu-user-oauth` and provide `FEISHU_APP_ID`/`FEISHU_APP_SECRET` through the environment. The command opens the domestic Feishu OAuth page and waits for `http://127.0.0.1:8765/callback`; register that URL in the app's security settings first. By default the returned user token is memory-only. Add `--feishu-oauth-persist` to request `offline_access`, save access/refresh tokens in a Windows CurrentUser DPAPI-encrypted cache, and reuse or refresh them in later processes without opening the consent page. For a business-user one-time setup, use `--feishu-oauth-bootstrap` instead: set `FEISHU_BOOTSTRAP_ACCOUNT` and `FEISHU_BOOTSTRAP_PASSWORD` only for that first process (or answer the secure prompts), and the command will complete the visible domestic Feishu login/consent and imply persistent caching. Later runs with the same Windows user profile need neither the Feishu account/password nor a browser. The default cache is under the current user's `LOCALAPPDATA`; override it with `--feishu-oauth-cache`. Use `--feishu-oauth-reauthorize` only when a fresh consent grant is required. The App Secret is never written to the cache. Do not use `--feishu-user-oauth`/`--feishu-oauth-bootstrap` together with `FEISHU_ACCESS_TOKEN`.

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --feishu-source-url 'https://donsontech.feishu.cn/wiki/W5jwwHoxei11cjkWzxZciWJWnCh' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/GuUUwCVeoiJLS8k2kTxcBS9Cngh?from=from_copylink' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/PSSYwIoBHi7JY5kB6jGcLpWundb?from=from_copylink&sheet=qTG951' `
  --feishu-user-oauth `
  --feishu-oauth-persist `
  --customer-id 3681575 `
  --output-root 'D:\Users\Donson\Downloads\番茄音乐飞书预检输出'
```

The compatible local input path accepts dry-run JSON (`batches[].bid` + `batches[].cids`) or an `.xlsx/.xlsm` containing `bid`/`bookid` and `cid` columns across multiple sheets. Excel rows with invalid/non-hex-32 CID values are ignored; duplicate CIDs within a BID are de-duplicated. If an Excel row's `打标状态`/`标签状态` is `已打标`, exclude it before BID grouping and browser launch; only blank or `未打标` rows are eligible.

First create and inspect a dry-run plan:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --input 'D:\Users\Donson\Downloads\番茄音乐_bid_cid_dry_run.json' `
  --customer-id 3681575 `
  --max-batches 1 `
  --output-root 'D:\Users\Donson\Downloads\番茄音乐打标输出'
```

The live run requires both `--live --confirm-live`, and credentials should come from environment variables. The customer ID is the 客户列表 ID, not a work-order ID:

```powershell
$env:USERGROWTH_ACCOUNT = '<account>'
$env:USERGROWTH_PASSWORD = '<password>'
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --input 'D:\Users\Donson\Downloads\番茄音乐_bid_cid_dry_run.json' `
  --customer-id 3681575 `
  --max-batches 1 `
  --live --confirm-live `
  --output-root 'D:\Users\Donson\Downloads\番茄音乐打标输出'
```

Use `--concurrency 3` to keep at most three independent BID browser sessions active. The command performs Feishu OAuth and source preflight once, then schedules one BID per browser; when a browser finishes, the next pending BID takes that slot. Results remain in source order, debug artifacts use separate `debug/<index>_bid_<BID>/` folders, and an ordinary batch failure is recorded without discarding later batches. Manual browser closure cancels the shared queue.

The platform accepts space-separated CID search values. The runner encodes the space-separated search in the URL and caps each search chunk at 50 CIDs, because comma-separated values and larger chunks are not reliable on the current 素材管理 page. It verifies the visible result count before `全选所有`, writes `bid_<BID>`, opens the resulting operation task, and requires `总任务数 == 批次实际命中数`, `执行成功数量 == 总任务数`, and `执行失败数量 == 0` before marking that chunk complete. Each chunk is checkpointed in `tomato_music_tagging_checkpoint.json`.

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

## Resume

Live Soda Music and Redfruit runs write `task.json` plus a workflow checkpoint incrementally (`soda_music_checkpoint.json` or `redfruit_checkpoint.json`). The checkpoint is per order, so concurrent batches do not overwrite each other. Soda stages include `pending`, `upload_processing`, `upload_task_created`, `upload_success`, `review_submitted`, `cid_backfilling`, `cid_backfilled_unreviewed`, and `completed`; Redfruit adds its ARLP/classification stages. During `upload_processing`, each duplicate-upload recovery and row-scoped `点击重试` action is persisted, so a resume does not blindly repeat already-handled upload rows. If `upload_processing` already contains an upload task ID, both workflows resume from that task and skip the file-upload stage; Soda does not run Redfruit's post-review classification stage.

After an interruption, resume from the task folder, `task.json`, or `redfruit_checkpoint.json`:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py `
  --resume-task "D:\path\to\task-folder" `
  --live --confirm-live `
  --account "$env:USERGROWTH_ACCOUNT" `
  --password "$env:USERGROWTH_PASSWORD"
```

Resume supports `soda_music` and `redfruit_short_drama`. Credentials are read again from CLI arguments or environment variables and are never stored in the checkpoint. A `completed` or `cid_backfilled_unreviewed` checkpoint returns the saved result without opening a browser; earlier stages are resumed from their saved task IDs and item metadata.

For Redfruit, when the checkpoint already contains a unique CID for every active material and the stage is `review_submitted`, `arlp_submitting`, `arlp_success`, or `classification_submitting`, resume skips the historical upload/review task and goes directly to `墨攻AI -> 素材管理`. It searches the batch CIDs (space-separated), verifies that the result count covers the batch, then continues from `arlp_stage_index` through the remaining three-stage ARLP configurations and `修改分类标签`. `arlp_stage_progress` is authoritative for the current configuration: a saved task ID in `task_created`, `waiting_result`, or `partial_failure` is reopened and polled instead of creating another ARLP task; a `selection_started`, `modal_open`, or `submitting` checkpoint safely replays only the current ARLP configuration. Completed stage task IDs are retained in `arlp_stage_task_ids` and cannot advance the index twice. It must not wait for an old upload task in this case.

For checkpoints created before the three-stage ARLP upgrade, `stage=arlp_success` or `classification_submitting` with no stage index is migrated as historical stage 1 complete (`arlp_stage_index=1`). Resume starts at `短剧端原生IAA`, then runs `番茄畅听`, instead of repeating stage 1 or skipping the two new stages. Checkpoints without `arlp_stage_progress` or `classification_progress` remain valid and use empty defaults.

For Redfruit `upload_processing` checkpoints without a task ID, resume never falls back to file upload. It only uses saved original creative-unit IDs to enter `工单管理 -> 创意单元 -> 录入素材`; if those IDs are missing, it stops with a checkpoint error instead of risking a duplicate upload.

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

Multi-batch execution remains parallel by default. If `concurrency` is omitted, it defaults to the number of batches, capped at 10. When the user specifies an order or asks for sequential execution, use `concurrency=1` or `--concurrency 1`; the manifest order is then preserved. A normal business failure, exhausted batch retry, or recoverable page issue is recorded for that batch and cannot stop later batches. The aggregate records `total_batches`, `attempted_batches`, `unattempted_batches`, and `overall_status`. A user cancellation/manual browser close stops the queue instead of opening the next batch.

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

Platform integrations can bridge a saved Playwright session without placing
cookies in task manifests or logs. Pass `--storage-state <input.json>` to
reuse a state file and `--storage-state-output <output.json>` to export the
latest authenticated state; callers should treat both files as short-lived
secrets and remove them after the process exits. The platform upload workflow
uses these options only when its “复用已保存会话” switch is enabled.

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

This mode is redfruit-only and requires `--live --confirm-live`. Pass the batch metadata explicitly. `--existing-creative-unit-drama-type` is mandatory and must be `动态漫`, `仿真人`, or `纯短剧`; `真人剧` and `真人实拍短剧` normalize to `纯短剧`:

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
