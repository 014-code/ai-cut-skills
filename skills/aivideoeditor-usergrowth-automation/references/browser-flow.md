# Browser Automation Flow

## Dependencies And Launch

`UserGrowthBrowserClient.run` lazily imports Playwright. Missing Playwright raises `需要先安装 playwright，并执行 playwright install chromium`. The client launches Chromium with local browser channels in this order: `msedge`, then `chrome`. Viewport is `1440x1000`; slow motion is controlled by `browser_slow_mo_ms` and `USERGROWTH_OPERATION_SPEED_FACTOR`.

Login uses `https://usergrowth.com.cn/open/login`, then navigates/checks `https://usergrowth.com.cn/home`. Captcha recognition uses `UserGrowthCaptchaSolver` and `ddddocr`. The credential/captcha validation itself still has five business attempts, but navigation/network failures are outside that limit: the browser remains open and waits with exponential backoff until the network or page session recovers. The client considers `/home`, `墨攻AI`, or `采购中心` as logged-in signals. After login, image/font/favicon requests are blocked.

## Network And Browser-Session Recovery

Network/navigation failures such as `ERR_CONNECTION_CLOSED`, `ERR_CONNECTION_RESET`, `ERR_CONNECTION_REFUSED`, `ERR_TIMED_OUT`, `ERR_NETWORK_CHANGED`, `ERR_INTERNET_DISCONNECTED`, `ERR_NAME_NOT_RESOLVED`, proxy failures, and navigation timeouts are treated as temporary. The runner writes `network wait`, `network recovery wait`, and `network recovered` records to `debug/run.log` and keeps waiting with exponential backoff capped at 30 seconds. It does not close the browser because a page is blank, an API is slow, or a navigation failed.

If Playwright reports that the page/context/browser target was closed unexpectedly, the runner waits for recovery and attempts to create a page again. If the browser process itself is disconnected, it relaunches the browser, logs in again, and resumes the current order loop. The only normal path that intentionally closes the browser is a user cancellation; the final cleanup also runs after normal completion or a non-recoverable business failure.

## Per-Order Flow

`_process_order` handles one `UserGrowthOrderPlan`:

1. Filter out skipped items.
2. Enter `墨攻AI` and then `工单管理`.
3. Search by `订单名称或ID`.
4. For redfruit workflow, run preflight checks before opening `新建创意单元`.
5. Open `新建创意单元` for the searched order, with scoped, exact-text, nearby, and coordinate click fallbacks.
6. Read upload limit from text such as `最多上传 N`, `最多 N 个`, or `上限 N`; skip plan if active items exceed the limit.
7. Upload files and enter 录入变色龙 with retry.
8. Read current task ID from a task input.
9. Wait for `全部成功`, fail on `已失败`.
10. Submit review.
11. Open material list/detail, read CIDs, read material type by CID, and mark items success.

## Upload

`_upload_files` waits for `input[type='file']`, clicking `点击或拖拽文件至此区域` or `上传` while waiting. It sets all item paths at once with `set_input_files`, clicks `点我开始上传` if present, and checks page text for limit-zero and upload failure messages.

Upload-card recovery is row-scoped. The browser first waits for a concrete failed upload row, then reads that row's visible text or red-exclamation tooltip. It must not click a page-level `点击重试` or sweep arbitrary icons/buttons.

If a single uploaded row turns red with `上传检测失败：该文件曾经被上传...`, the browser first deletes that failed row, records its `creative_unit_id`, and removes it from the current batch. After the normal rows finish, it returns to `工单管理 -> 创意单元`, searches those `creative_unit_id` values with commas, and clicks `录入素材` for the reused creative units. If the platform says it is already recorded, the run skips the follow-up entry step. This recovery and its row metadata are checkpointed for both Soda Music and Redfruit.

If the failed row itself exposes `点击重试`, the browser clicks only that row's retry action once and checkpoints the row-scoped action for both workflows. If the same row still fails after retry, or the reason is another upload failure, the run stops with the row-level reason and writes debug snapshots.

The CLI also supports direct redfruit recovery when the original creative-unit IDs are already known. It reuses the same row-scoped search and cross-page selection logic without creating an upload page or re-uploading files.

Upload retry has two layers:

- `_upload_files` retries up to 6 attempts and can reload/reopen the creative-unit page.
- `_upload_and_enter_chameleon_with_retry` retries the upload plus enter-chameleon sequence up to `max_status_retries` when the failure looks transient.

## Chameleon Entry And Tags

After upload cards are ready, the browser clicks `继续编辑`, `确认提交`, selects all creative units, clicks `录入素材`, waits for a page containing `投放平台`/`汽水音乐`, confirms the delivery modal, then fills the first material card and uses `一键复用`.

Chameleon tag strategy:

- The browser layer assumes one upload batch contains one song/type combination, so all selected material cards can share the first card's tags.
- Fill the first card with defaults and custom tags, then click `一键复用` -> `全选` -> `一键复用`, then `提交` -> `查看任务详情`.
- Do not put different songs into one browser upload batch unless the upstream planner/UI has split them first.

Important current assumptions:

- Browser filling uses the first active item in the batch as the source for shared classification/custom tags. Upstream batching must keep each browser batch homogeneous.

Default form choices:

- `请选择UGC内容` -> `不包含`.
- Cascader `汽水音乐-素材类型` -> `汽水音乐-素材类型 / LUNA_剪辑制作 / LUNA_自产`.
- Cascader `LUNA素材来源` -> `LUNA素材来源 / LUNA_千沧代理`.
- Cascader `LUNA功能卖点` -> `LUNA功能卖点 / <classification_path_for_material(file)>`.
- Custom tags from `item.custom_tags`, with a fallback to `custom_tags_for_material`.
- Radio `未成年人内容` -> `已授权`.
- Radio `影视内容` -> `已授权`.
- `一键复用` -> `全选` -> `一键复用`, then `提交` -> `查看任务详情`.

## Review And CID Backfill

`_submit_review` clicks `送审`, confirms `确定`, then optionally clicks `查看任务详情`.

`_fill_cids_for_task` searches the task by ID, waits for row success, opens `素材/文案列表查看` or related text, reads CIDs from the global search input, and requires at least as many CIDs as items. It zips item order with CID order.

If task status keeps refreshing for a long time without reaching `全部成功`, the browser can first open `查看详情`, read CIDs, write them back to Excel, and mark the row note as `未送审`. This is a backup path, not the normal success path.

## Workflow Checkpoints

Both workflows write stage transitions, task IDs, item statuses, CID values, CID material types, and workflow metadata to the task folder. Soda resume covers upload processing, upload task completion, review submission, CID backfill, and the explicit `未送审` CID-backup terminal state. Network/navigation failures remain recoverable and do not clear these checkpoints.

## Redfruit ARLP Completion

Redfruit orders are checkpointed independently of the browser session. The browser records the current order stage, upload task ID, review task ID, ARLP task ID, classification task ID, and per-material CID/status before continuing. The `upload_processing` stage is written before upload begins and after each duplicate-upload recovery, including the original creative-unit ID, material ID, failure reason, and deferred status. A browser or network interruption can therefore resume from the saved stage without re-uploading files whose original creative units were already recovered. The CLI resume entry is documented in `references/standalone-cli.md`.

After a redfruit review, the browser opens `素材/文案列表查看`, clicks one material card to enter selection mode, then uses `全选 -> 全选所有` before `编辑 -> 增加ARLP`.

The ARLP submission creates a separate operation task. The browser opens `查看任务详情` and reads the task row's `总任务数`, `执行成功数量`, and `执行失败数量` instead of treating the creation-success dialog as the final result:

- When the selection counter equals the batch material count, the operation task `总任务数` must also equal that same batch count, with `执行成功数量 == 总任务数` and `执行失败数量 == 0`; only then is ARLP complete and the workflow continues to `修改分类标签`. A `20/20` task cannot finish a 32-material batch.
- When the task reaches a terminal partial result, the browser logs the task ID and counts, closes the result dialog, refreshes the material list, clears the old selection, and repeats `点第一张素材 -> 全选所有 -> 增加ARLP`.
- The retry loop has no artificial attempt limit. It keeps refreshing and retrying until all selected materials are reported successful or the user cancels the run.

Typical progress messages are `ARLP 第 N 轮结果：任务 <id>，成功 X/Y，失败 Z`, `ARLP 部分成功...重新增加 ARLP`, and `ARLP 全部成功...`. The task and browser diagnostics continue to be written to the normal `run.log` and `debug/` artifacts.

After ARLP is fully successful, the browser runs `编辑 -> 修改分类标签` for the same complete material selection. Saving the classification edit also creates an operation task, so the browser does not treat the save dialog as completion: it opens `查看任务详情`, requires the task total to equal the batch count, and requires every one of those materials to report success with zero failures. A partial result or an undersized task closes the result dialog, refreshes the material list, clears the previous selection, and repeats `点第一张素材 -> 全选所有 -> 编辑 -> 修改分类标签`; this has no artificial retry limit. The selector deliberately waits for `全选所有` and does not silently fall back to `全选当前页`.

## Redfruit Preflight

Redfruit runs a blocking preflight after the work-order search and before `新建创意单元`:

1. Read the order title from the search result row and normalize the drama/order category to `动态漫` or `仿真人`.
2. Compare that against the batch-derived drama category from the selected videos. Material-mode labels such as `AI前贴`/`AI后贴` are classification/custom-tag inputs and do not affect this preflight category comparison.
3. Open the short-drama insight page at `https://usergrowth.com.cn/aigc/insight/business/playlet?source=13`.
4. Search each drama title by name, not BID.
5. Read the title-label line from the card, such as `女频 / - / - / 3D动画漫剧`.
6. Compare the card label kind against the file-derived kind.
7. Click the card `ID` button when needed and compare the expanded `BID` against the expected BID.
8. If the user-specified order does not match the selected batch, report it explicitly as `用户指定订单与这批素材不符` or `用户指定订单与墨攻短剧选剧不符`.

Any mismatch raises a loud failure and stops the run before upload.

Fallback CID reading clicks `查看详情`, tries `一键复制对象id`, then extracts CIDs from body text. `_read_material_type_by_cid` opens `查看素材` for the CID row and extracts `分类标签` for backfill material type.

## Debug And Cancellation

`_snapshot_error` writes `debug/<name>.txt`, `debug/<name>.png`, and details to `debug/run.log`. Normal snapshots currently return early unless called with `screenshot=True`.

Cancellation is a threading event watched by `_watch_cancel`; when set, the browser is closed so Playwright waits are interrupted and item/plan statuses become `cancelled`.
