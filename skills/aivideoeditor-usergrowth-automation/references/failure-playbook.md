# Failure Playbook

Use the task folder and `debug/` artifacts first. Read `diagnostic_summary.json` before the detailed files; then inspect `task.json`, `run.log`, `error.json`, `error.log`, `debug/events.jsonl`, `debug/run.log`, and the newest `debug/*.png`/`*.txt` when live automation fails.

## Where To Read Logs

- Normal dry-run success
  Read `<output-root>/<timestamp>_<task-name>/task.json`, `run.log`, and `result.xlsx`.

- Normal multi-batch success or partial failure
  Read `<output-root>/batch_runs/<timestamp>_<task-name>/batch_summary.json` and `run.log` first. Then open each child batch's `task_json`/`run_log` or `error_json`/`error_log` paths listed in the aggregate summary.

- Normal live success or per-order platform failure
  Read `<output-root>/<timestamp>_<task-name>/task.json` and `run.log`. For browser/platform failures, also read `debug/run.log` and the newest `debug/*.png`/`debug/*.txt`.

- Failure after a task folder has been created
  Read `<output-root>/<timestamp>_<task-name>/error.json` and `error.log`. These include error type, message, selected videos, sanitized config, and Python traceback. If the browser had already started, also read `debug/run.log` and error snapshots.

- Failure before task execution, such as unmatched video selectors
  Read the CLI stderr. If `output_root` was already parsed, also check `<output-root>/_cli_errors/*.json` and `*.log`.

- Hard kill, power loss, or process termination
  Only logs flushed before termination will exist. Check the latest task folder under `output_root`, then `error.*`, `task.json`, `run.log`, and `debug/` in that order.

- Soda or Redfruit process interruption
  Read `soda_music_checkpoint.json` or `redfruit_checkpoint.json` first. `orders.<order_id>.stage` identifies the resume point, saved task IDs identify the platform task to query, and `operation_retry_counts` preserves consumed task-row retry attempts. `task.json` contains the same stage and material snapshot; `run.log` contains checkpoint transitions; `debug/run.log` contains browser/network recovery details. Resume with `--resume-task` as documented in `references/standalone-cli.md`. A `completed` checkpoint does not open a browser again. Soda `review_submitting`, `review_submitted`, and `cid_backfilling` resume without uploading files again.

- Network interruption, blank page, or browser target unexpectedly closed
  Do not treat the browser window disappearing as a normal business failure until recovery has been exhausted. Read `debug/run.log` and search for `network wait`, `network recovery wait`, `network recovered`, `网络仍未恢复`, `网络已恢复`, `browser disconnected`, and `browser process tracked`. Navigation failures, network errors, and non-zero/unknown browser exits may relaunch the browser and resume from the checkpoint with a bounded automatic retry count. A clean Chromium exit code `0` is treated as the user manually closing the browser: the current task stops immediately and must not open another browser automatically.

## File Meanings

- `task.json`: final structured result when the task reaches normal completion; contains config, summary, selected videos, plans and item statuses.
- `soda_music_checkpoint.json` / `redfruit_checkpoint.json`: workflow checkpoint with per-order stage, task IDs, `operation_retry_counts`, material CID/status, row-scoped retry metadata, and duplicate-recovery metadata such as `existing_creative_unit_id`; inspect the matching file first after interruption. If the stage is `upload_processing`, inspect item metadata before resuming: handled retry rows and deferred duplicate files are not blindly re-uploaded.
- `run.log`: final human-readable summary when the task reaches normal completion.
- `error.json`: structured failure record for task-level exceptions.
- `error.log`: human-readable failure record and traceback.
- `debug/run.log`: browser timing, browser error snapshot metadata, exception type/message/traceback for `_snapshot_error`.
- `debug/events.jsonl`: structured run/checkpoint and row-level recovery events. It is diagnostic-only and does not drive browser actions.
- `diagnostic_summary.json`: first-stop summary containing current stage, task IDs, checkpoint path, error details, artifact paths, and a resumable command when available.
- `debug/<name>.txt`: page URL and body text at the failing browser step.
- `debug/<name>.png`: full-page screenshot at the failing browser step.
- `duplicate_songs.xlsx`: duplicate song-name records relevant to the selected batch, when duplicates are found.
- `batch_runs/<timestamp>_<task-name>/batch_summary.json`: aggregate multi-batch result with per-batch status, child task folder paths, selected counts, and error log pointers.

When a Soda Music or Redfruit multi-batch run is explicitly serial (`--concurrency 1`), a batch that reaches its retry limit remains `status=failed` in `batch_summary.json`, and the next batch is started. A user cancellation or manual browser close stops the remaining serial queue instead of opening another browser.

## Common Failures

- Feishu API reports permission denied or cannot resolve the Wiki node: verify the token mode, app scopes, Wiki-node sharing, and spreadsheet read/edit permissions. Do not fall back to browser cell automation.
- Feishu read-only plan contains conflicts: inspect `task.json.feishu.library_conflicts` and `existing_bid_conflicts`. Multi-BID library conflicts are skipped; existing source BIDs are preserved unless overwrite was explicitly requested.
- Feishu writeback verification fails: stop and inspect the exact range from `task.json.feishu.planned_writes`. Do not proceed to assume the source sheet contains the planned BID values.

- Tomato Music batch search shows `暂无数据`: confirm the customer context, preserve the material page `selectorId`, use newline-separated CID values, and reduce the search to at most 50 CIDs. Do not switch to comma-separated input or click `全选所有` on a no-result page.
- Tomato Music tag task count mismatch: inspect `tomato_music_tagging_checkpoint.json` and the chunk's visible `共 N 条` count. The runner must stop unless the operation task reports total N, success N, and failure 0.

- `需要先安装 playwright，并执行 playwright install chromium`
  Check `material_remix_desktop_source/requirements.txt`, install dependencies in the desktop environment, then run browser install for Chromium if needed.

- `需要先安装 ddddocr 才能自动识别登录验证码`
  Install desktop dependencies including `ddddocr` and `onnxruntime`.

- Login fails after 5 attempts
  Inspect `login_failed_*` snapshots. Check account/password, captcha image detection, whether the login page changed, and whether `/home` still shows `墨攻AI` or `采购中心`.

- Cannot enter work order management
  Inspect `work_order_not_reached`. Confirm `墨攻AI`, `工单管理`, and `素材管理` labels still exist or update selectors/text.

- Order not found
  Inspect `order_<id>_not_found`. Verify the order ID, placeholder `订单名称或ID`, and whether search needs a different event than Enter.

- Cannot click `新建创意单元`
  Inspect `order_<id>_create_button_not_found` or `create_click_no_effect`. Check scoped row selection, exact text, nearby button logic, and coordinate fallback.

- Clicked `新建创意单元` but page shows order deadline expired
  Inspect `order_<id>_create_blocked_*` and the snapshot text. The platform may show `已超出当前订单的交付截止时间`, which means the order itself cannot be used for a new creative unit and is not a selector bug.

- Upload page/input missing
  Check `_looks_like_upload_page`, `input[type='file']`, `点击或拖拽`, `文件上传`, and `温馨提示`. Platform UI may have changed the upload control.

- Upload limit zero or too many files
  Compare `plan.upload_limit`, item count, and page text. The code recognizes `当前选择文件数量超过订单创意单元上限` and reads numeric limits from several patterns.

- Waiting upload cards forever
  Check whether success icons are still `span.arco-upload-list-success-icon`. If the platform changed icons, update `_wait_upload_cards_ready`.

- Chameleon modal validation fails
  Inspect `chameleon_delivery_*` snapshots. Check `投放产品`, `汽水音乐`, and `投放平台` dropdown behavior.

- Upload row says the file was uploaded before
  Inspect `upload_failed_*` snapshots and `debug/run.log`. The recovery path parses `创意单元id` and `素材id`, deletes the failed upload row, finishes the rest of the batch, then searches `工单管理 -> 创意单元` by comma-separated creative-unit IDs and clicks `录入素材`. If the page reports `以下创意已录入为素材`, parse the returned `创意id/cid` pairs, mark only those returned materials successful, confirm the dialog, and close or continue from the creative-unit list. When only part of the selected set was already recorded, the runner unchecks those returned creative units, verifies the selected count equals the remaining count, and continues the same entry flow for the unchecked remainder. If all selected materials were already recorded, no new entry task is opened. In Redfruit checkpoint recovery this is terminal only for the returned items: write `stage=completed` only when the whole active batch is done. If some items are still unresolved, preserve `stage=upload_processing`, log the remaining count, and block re-upload until their original creative-unit IDs are recovered. If the dialog has no concrete parseable creative ID, or an ID cannot be matched to the current batch, stop instead of marking the batch successful.

- Direct existing creative-unit recovery stops after the first page
  Inspect `debug/run.log`. A correct run logs `creative unit page 1: selected_count=20`, then page transitions such as `1 -> 2` and `2 -> 3`, and waits for the visible row signature to change before selecting the next page. The final selected count must equal the requested ID count.

- Upload row shows a retry button or red exclamation
  Inspect the specific failed row, not the whole page. The browser now waits for a concrete failed row, reads its visible text or tooltip, and only clicks that row's `点击重试` when it is actually present. If the same row still fails after retry, the run should stop with the row-level reason instead of scanning other controls.

- Redfruit preflight fails
  Inspect `redfruit_preflight_*` snapshots and `debug/run.log`. Check the user-specified order ID, the work-order title, the short-drama search result, the card title-label line, and the expanded `BID`. Redfruit preflight compares only the drama/order category `动态漫` or `仿真人`; material-mode labels such as `AI前贴`/`AI后贴` only affect classification tags and custom tags. If the message says `用户指定订单与这批素材不符` or `用户指定订单与墨攻短剧选剧不符`, the order chosen by the user does not match the batch and should be corrected before upload. Common snapshots include `redfruit_preflight_order_title_missing_*`, `redfruit_preflight_order_kind_missing_*`, `redfruit_preflight_mixed_item_kinds_*`, and `redfruit_preflight_failed_*`.

- Cascader selection fails
  Inspect console/debug output around `级联选择失败`. Confirm `LUNA_` labels and field names: `汽水音乐-素材类型`, `LUNA素材来源`, `LUNA功能卖点`.

- Task never becomes `全部成功`
  Inspect task row text and refresh behavior. For Redfruit, an explicitly `已失败` upload, ARLP, or classification task is retried only through that task row's `重试` action, up to 3 times. Soda applies the same exact-row rule to its post-submit review task. The log/event records `task_id`, operation name, and `retry=N/3`; Soda also writes `operation_retry_counts["汽水音乐送审:<task_id>"]` into its checkpoint, so a browser restart cannot reset the limit. After the third failed retry the run stops. A partial-success ARLP/classification result still uses the missing-item completion loop instead of consuming this full-task retry budget.

- CID count mismatch
  Inspect `task_<id>_cid_count_mismatch`. Check material list search input, copy fallback, item order, and whether some uploads produced no CID.

- Excel read failure
  The code already attempts style repair for `.xlsx`/`.xlsm`. If it still fails, ask the user to open and resave the workbook as `.xlsx`.

- Excel save failure
  Check whether Excel/WPS has the workbook open. The runner lock does not solve external file locks.

- Batch manifest failure
  Check whether `batches` is a non-empty array, each batch has `video_folder`, `order_id`, and one of `videos`, `video_globs`, `video_list`, or `all_videos=true`. For unmatched selectors, read CLI stderr and `<output-root>/_cli_errors/`.

## High-Risk Gotchas

- UI state currently persists `account` and `password`; avoid expanding this pattern.
- Live upload writes directly to the original backfill Excel after each successful order.
- Existing CID rows are intentionally preserved; new rows start at the first empty CID row.
- Multi-batch live mode can open multiple browser instances. Keep `concurrency` conservative if the machine or platform session is unstable.
- Browser tag fill uses each item's planner values. It only uses one-click reuse when tags are identical or when non-song-ID tags are identical and `gq_id` is appended per card afterward.
- Redfruit runs a hard preflight gate before upload. If the gate fails, fix the work-order kind, the filename kind, or the BID mapping before retrying.
