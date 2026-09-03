# UserGrowth Workflow

## Inputs

A run is described by `UserGrowthRunConfig`:

- `video_folder`: folder scanned for `.mp4`, `.mov`, `.mkv`, `.avi`.
- `order_excel`: the backfill workbook.
- `song_excel`: the song library workbook.
- `output_root`: where task artifacts are written.
- `account`/`password`: only required for live upload.
- `order_id`: required; all active items in the run use this order ID.
- `batch_name` and `selected_video_paths`: optional desktop auto-split fields. When `selected_video_paths` is present, the planner only scans those files even though `video_folder` still points to the original folder.
- `task_name`, `month_tag`, `custom_tag_template_name`, `custom_tag_template_tags`, `recursive`, `dry_run`, `headless`, `max_status_retries`, `refresh_interval_seconds`, `browser_slow_mo_ms`.

## Single Run

The standalone skill CLI uses `run_selected_usergrowth_task` in `scripts/usergrowth_upload.py` when specific videos are requested. When a manifest contains top-level `batches`, the CLI resolves each batch's selected videos and runs them concurrently with `run_selected_usergrowth_batches`. The original desktop runner uses `run_usergrowth_task(config, progress, cancel_event)` for whole-folder runs.

When `--split-by-song` or `split_by_song=true` is set, the CLI first scans the selected folder or explicit video set, groups files by the resolved song name, and turns each song group into one batch before invoking the concurrent batch runner.

The selected-video standalone flow performs:

1. Resolve selected files from `--video`, `--video-glob`, `--video-list`, manifest `videos`, or explicit `--all-videos`.
2. Create `<output_root>/<timestamp>_<safe_task_name>/`.
3. Create `debug/` and prepare `duplicate_songs.xlsx`.
4. Build a plan only for the selected paths.
5. Run dry-run or live browser upload.
6. Write `task.json` and `run.log`.

The original whole-folder flow performs:

1. Create `<output_root>/<timestamp>_<safe_task_name>/`.
2. Create `debug/` and prepare `duplicate_songs.xlsx`.
3. Call `build_usergrowth_plan(config, duplicate_song_output_path=...)`.
4. If no items were scanned, raise `未扫描到可处理视频`.
5. In dry-run mode, change pending items to `ready`, skip browser automation, and write `result.xlsx` with `include_ready=True`.
6. In live mode, create `UserGrowthBrowserClient`, upload active plans, and write successful orders back into the original backfill Excel after each order completes.
7. Write `task.json` and `run.log` under the task folder.

The desktop UI can auto-split the current video folder by recognized `song_name` immediately after the video folder is selected. If a valid song library Excel is already selected, the split first uses the library as the authority: exact match wins, otherwise the normalized full material name is searched for one unique library song name, with the longest song name preferred. This covers filenames such as `新框架-1-歌曲名`, `歌曲名-10-模板`, or names separated by punctuation without adding template-word special cases. If the song library is missing or cannot be read, the split falls back to filename extraction: after the `LUNA_素材类型` marker, ordinary leading batch numbers are stripped, and generic `A-1-B` names choose the more informative side around the sequence number. Explicit leading book-title markers are preferred, so `《歌曲名》` and `《歌曲名》（歌曲名）` collapse to `歌曲名`; nested book-title text inside a longer song name, such as `寻鲸记（《心动小镇》...）`, is not treated as the primary song name. This handles both `歌曲名-10-模板名` and `模板名-10-歌曲名` without hard-coding the template text. The desktop batch table also groups fallback names by a compact normalized key, so punctuation variants such as `D.N.A`, `D-N-A`, and `D N A` stay in one batch. The split does not require the backfill Excel, output directory, or order ID yet. Each generated batch keeps the same `video_folder` and shared Excel/order settings, but sets `batch_name=<song_name>` and `selected_video_paths=[that song's files]`. Full song-library matching, skipped-song handling, and order validation still happen later during preview/upload. The batch queue then runs through the existing multi-batch runner, so each browser upload batch contains one song and can safely use first-card `一键复用`.

## Planning

`build_usergrowth_plan` scans videos, detects material type from filenames, extracts song names, loads song records, attaches song data, attaches order ID, and groups non-skipped items by order. If `config.selected_video_paths` is non-empty, it scans only those selected files.

Custom tags are rendered from the selected template, not inferred from material-type/file-name hard-coded rules. A template can use a legacy single list (`custom_tag_template_tags`) or split lists (`custom_tag_template_fixed_tags` and `custom_tag_template_optional_tags`). The final browser-filled custom tags are `fixed_tags + optional_tags` after de-duplication. The default `单曲模板` fixed tags are `未成年人已授权`, `影视版权已授权`, `dxzc`, `汽水音乐`, `{月份标签}`, `{歌曲ID}`. `{月份标签}` is replaced by `month_tag`; `{歌曲ID}` is replaced by the matched song ID and is skipped when no song ID is available.

VIP/SVIP materials skip song matching and do not append a song ID tag. Blocked songs become `skipped`. Missing song ID or duplicate song candidates do not skip upload; they keep custom tags without the song ID and carry a warning message.

After planning, the runner prints a per-video song match line before browser upload starts, for example `歌曲匹配成功：... | ID=gq_...` or `歌曲匹配未命中：...`. The same status is saved as `song_match_message` on each item.

## Live Upload

Only plans whose status is not `skipped` are sent to the browser client. After a plan succeeds, `order_complete` calls:

```python
write_back_results(config.order_excel, config.order_excel, plan.items, include_ready=False)
```

The runner serializes writes per resolved Excel path, which protects same-process multi-batch writes. It does not protect against Excel/WPS having the file open.

For `workflow=redfruit_short_drama`, the planner requires every file name to resolve to `动态漫`, `仿真人`, or `纯短剧` (`真人剧`/`真人实拍短剧` aliases). It does not write a song Excel. After the upload task succeeds, it opens the task-created material list, reads CIDs, and completes three ordered ARLP configurations: redfruit short drama/redfruit playlet/Tomato novel, Tomato Listen, then Danhua novel. Redfruit does not submit review. Each stage waits for its separate operation task to report that every selected material succeeded and records `arlp_stage_index` plus its task ID. A partial or failed ARLP task is retried from a fresh `点第一张素材 -> 全选所有 -> 增加ARLP` selection cycle until the task row's successful count equals its total count or the user cancels the run. Once all three stages are complete, the workflow records completion directly; it does not open a final `编辑 -> 修改分类标签` modal.

## Batch Runs

Tomato Music uses the same bounded-queue contract through `tomato_music_tagging.py --concurrency N`: OAuth and Feishu preflight run once, while up to `N` independent `TomatoMusicTaggingClient` instances process one BID each. Every client has its own browser and debug subfolder. Checkpoint results are merged in source BID order, Feishu status writes remain gated per successful chunk, and a normal child failure cannot stop later queued BIDs.

The vendored desktop runner still provides `run_usergrowth_batches` for whole-folder `UserGrowthRunConfig` batches. The standalone CLI batch manifest uses the selected-video path instead:

1. Read top-level defaults and each `batches[]` entry.
2. Resolve `videos`, `video_globs`, `video_list`, or explicit `all_videos=true` per batch.
3. Clamp `concurrency` to `1..10`; when omitted, default to the batch count and keep multi-batch runs concurrent, while explicit `concurrency=1` selects serial queue execution.
4. In serial mode, run batches in manifest order. This applies equally to `soda_music` and `redfruit_short_drama`: a normal business failure or retry-exhausted batch is recorded as `failed`, and the next batch starts. Recoverable network/page failures stay in the current batch's recovery loop. In concurrent mode, run each batch in a `ThreadPoolExecutor`; each batch creates its own task folder and browser run in live mode, and one ordinary child failure must not discard the other child results. A user cancellation/manual browser close stops the serial queue without opening the next browser.
5. Write aggregate `batch_summary.json` and `run.log` under `<output-root>/batch_runs/<timestamp>_<task-name>/`.

Same-process writes to the same backfill Excel still use `_backfill_lock`, so live batch completion writes are serialized inside this CLI process. It does not protect against Excel/WPS having the file open.

## Output Artifacts

- `task.json`: machine-readable config, summary, result path, duplicate song workbook path, and plans/items. Redfruit plans include `arlp_stage_progress` for fine-grained ARLP resume.
- `run.log`: summary, selected template fields in config, `[song_matches]`, and per-item status/type/song/CID/tags.
- Soda Music and Redfruit live runs additionally update a workflow checkpoint (`soda_music_checkpoint.json` or `redfruit_checkpoint.json`) at each order stage. The file stores per-order stage, platform task IDs, operation-task retry counts, item retry metadata, and CID/status so `--resume-task` can continue without recreating completed upload, review, CID backfill, or ARLP work. Redfruit additionally records `arlp_stage_index` and `arlp_stage_task_ids`, plus `arlp_stage_progress` for each stage's selection/modal/submission/task-wait/result step, attempt, task ID, counts, and error. A restart resumes from the first unfinished ARLP configuration or continues polling an already-created task and does not count a completed task twice. Redfruit records `upload_success -> cid_backfilling -> arlp_submitting`; Soda specifically records `review_submitting -> review_submitted -> cid_backfilling`. Resuming these stages skips upload, and an exact failed review row retains its consumed retry count across browser restarts.
- `error.json` and `error.log`: task-level failure records when execution fails after the task folder is created.
- `<output_root>/_cli_errors/*.json` and `*.log`: early CLI failures before a task folder exists, when `output_root` was already parsed.
- `batch_runs/<timestamp>_<task-name>/batch_summary.json` and `run.log`: aggregate multi-batch status, `total_batches`, `attempted_batches`, `unattempted_batches`, `overall_status`, child task folders, and child error log pointers.
- `debug/run.log`: browser-level timing and error-snapshot metadata.
- `debug/events.jsonl`: append-only structured browser events for run start/end, checkpoints, row-scoped upload retry, duplicate-material deferral, and error snapshots. This is diagnostic-only and never controls browser decisions.
- `debug/*.txt` and `debug/*.png`: only written by error snapshots in current code because normal `_snapshot(..., screenshot=False)` returns early.
- `diagnostic_summary.json`: compact task status, workflow, order stages, task IDs, checkpoint path, artifact paths, latest error, and a resume command when the checkpoint is resumable.

Checkpoint JSON keeps the existing `version` contract and additionally records `artifact_schema_version`, `workflow_contract_version`, selected-video count, and order IDs. These fields are informational and backward-compatible; they do not alter stage transitions.

## Status Values

Common item statuses: `pending`, `ready`, `success`, `skipped`, `failed`, `cancelled`.

Common plan statuses: `pending`, `success`, `skipped`, `failed`, `cancelled`.
