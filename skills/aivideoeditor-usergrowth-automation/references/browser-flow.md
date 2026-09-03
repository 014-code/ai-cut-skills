# Browser Automation Flow

## Maintenance Boundary

Keep the Soda Music and Redfruit stage sequences stable. Browser failures should first be addressed with a local selector, wait condition, row-scoped recovery, retry, or checkpoint fix inside the failing step. Do not alter shared orchestration or another workflow merely to bypass one page problem.

Before changing stage order, cross-workflow shared behavior, batching/concurrency, checkpoint/resume semantics, review/CID/ARLP/classification sequencing, or completion-count requirements, explain the necessity and impact to the user and wait for explicit confirmation. Bug-fix, test, rerun, and skill-sync requests do not count as that confirmation.

## Dependencies And Launch

`UserGrowthBrowserClient.run` lazily imports Playwright. Missing Playwright raises `需要先安装 playwright，并执行 playwright install chromium`. The client launches Chromium with local browser channels in this order: `msedge`, then `chrome`. Viewport is `1440x1000`; slow motion is controlled by `browser_slow_mo_ms` and `USERGROWTH_OPERATION_SPEED_FACTOR`.

Standalone Soda Music, Redfruit short-drama, and Tomato Music runs share an account-scoped UserGrowth login cache by default. The cache is stored under the current Windows user's `LOCALAPPDATA`, uses an account hash in the file name, and encrypts the Playwright storage state with CurrentUser DPAPI. Each browser context loads it before opening a page, validates it against the authenticated home route, and falls back to the normal captcha login when invalid. A validated or freshly authenticated state is saved immediately before business navigation and again during normal cleanup. Never log cookies or copy the encrypted cache to another Windows user.

Login uses `https://usergrowth.com.cn/open/login`. Captcha recognition uses `UserGrowthCaptchaSolver` and `ddddocr`. After clicking login, keep the current page open until it reaches an authenticated page or visibly reports an account, password, or captcha failure. Do not force-navigate to `/home` on a short timer and do not refresh while the response or login form is merely slow. The credential/captcha validation has five explicit business-failure attempts. Captcha screenshot failures independently reload the login page and keep retrying until the image can be read or the user cancels. A validation message such as `图片验证码由4位字符组成` is also an explicit captcha failure: reload the login page, obtain a fresh captcha, and retry. Navigation/network failures remain recoverable outside the business-attempt loop. The client considers `/home`, `/open/customer`, `客户列表`, `墨攻AI`, or `采购中心` as logged-in signals. After login, image/font/video/favicon requests are blocked. Known third-party analytics and advertising hosts, plus conservative collection/ad paths outside `usergrowth.com.cn`, are also blocked; UserGrowth business API, JavaScript, CSS, and XHR requests are preserved.
The post-login customer chooser at `/open/customer` is also a successful authenticated state. When a customer ID is supplied, search that exact ID in 客户列表 and click the matching card's `进入`; do not treat the customer list as a failed login and do not silently continue in another customer context.
The `进入` action may be rendered as spaced text such as `进 入`, and may navigate the current page or open a new page. Reuse the selected page for all following 墨攻 steps and wait without refreshing until it has left 客户列表 before continuing.

## Tomato Music Post-Upload Tagging

`scripts/tomato_music_tagging.py` reuses the login and material-list primitives for a post-upload CID-to-BID tagging operation:

Feishu is outside this browser flow. Read song names, match `bookid`/BID across worksheets, and write the source BID column through the official Feishu API described in `references/feishu-sheets-api.md`. Do not open Feishu in Playwright or automate spreadsheet cells. The browser begins only when the BID/CID batches are ready for UserGrowth/墨攻.

1. Select the requested 客户列表 customer (for example `3681575`). If the customer home shows `我已知悉`, click the actual button and verify that it disappears before entering `墨攻AI -> 素材管理`; recheck while waiting because the prompt can appear asynchronously. Retain the customer-scoped `selectorId` URL context.
2. Search space-separated CID values by reusing the Redfruit material-management CID search helper. Split one BID batch into chunks of at most 50; do not use comma-separated values or blindly paste 100+ values into the current search box.
3. Read the visible `共 N 条` result count. For Tomato Music, keep the browser open and continue waiting when the page temporarily shows `暂无数据`; do not mark the chunk skipped or refresh it on a timer. Retry the same search URL with exponential backoff only after an explicit request/load/network/service error. Never use `全选所有` until a positive exact count has been observed.
4. Click the first card's selection hotspot, then `全选 -> 全选所有`, and verify the selection counter equals the visible result count.
5. `编辑 -> 修改自定义标签`, add `bid_<BID>`, close any open tag dropdown, and click `确定`.
6. Open `查看任务详情` and poll the operation task. The chunk is successful only when `总任务数 == N`, `执行成功数量 == N`, and `执行失败数量 == 0`.
7. A non-recoverable business failure marks only the current CID chunk `failed`; continue the remaining chunks and BID batches. Network/page/session failures recover and retry the current chunk. User cancellation or manual closure of the headed browser remains terminal for the whole run.
8. For a Feishu-backed live run, only after this exact success gate passes, update the matching source rows' `打标状态` to `已打标` through the official Sheets API and reread the cells. Never mark skipped, partial, or failed chunks.

The operation is separate from upload/review/CID backfill and is idempotent at the tag level: a rerun checks whether the target tag is already present in the edit dialog before adding it again. Per-chunk results and task IDs are written to `tomato_music_tagging_checkpoint.json`.

When `tomato_music_tagging.py --concurrency N` is used, each active BID owns an independent browser session and debug subfolder. At most `N` BIDs run at once; each session still performs the complete login/customer/material/task-count/status-writeback contract above. A completed slot starts the next BID, and ordinary failure in one BID does not terminate the others.

## Network And Browser-Session Recovery

Network/navigation failures such as `ERR_CONNECTION_CLOSED`, `ERR_CONNECTION_RESET`, `ERR_CONNECTION_REFUSED`, `ERR_TIMED_OUT`, `ERR_NETWORK_CHANGED`, `ERR_INTERNET_DISCONNECTED`, `ERR_NAME_NOT_RESOLVED`, proxy failures, and navigation timeouts are treated as temporary. The runner writes `network wait`, `network recovery wait`, and `network recovered` records to `debug/run.log` and keeps waiting with exponential backoff capped at 30 seconds. It does not close the browser because a page is blank, an API is slow, or a navigation failed.

If Playwright reports that the page/context/browser target was closed unexpectedly, the runner first distinguishes a user-closing action from a crash. Closing a headed automation window or tab with the window X stops the task immediately and must not create a replacement page or enter outer automatic resume. A renderer crash, non-zero/unknown browser-process exit, network failure, or page-session failure still waits for recovery, relaunches when needed, logs in again, and resumes the current order loop. The final cleanup also runs after normal completion or a non-recoverable business failure.

## Per-Order Flow

`_process_order` handles one `UserGrowthOrderPlan`:

1. Filter out skipped items.
2. Enter `墨攻AI` and then `工单管理`.
3. Search by `订单名称或ID`.
4. For redfruit workflow, run preflight checks before opening `新建创意单元`.
5. Open `新建创意单元` for the searched order, with scoped, exact-text, nearby, and coordinate click fallbacks.
6. Read upload limit from text such as `最多上传 N`, `最多 N 个`, or `上限 N`; skip plan if active items exceed the limit.
7. Upload files and enter 录入变色龙 with retry. After the delivery product/platform modal closes, keep the page open until every material card on the current page has rendered actionable `UGC内容`, `分类标签`, and `自定义标签` controls; Redfruit also requires `创意源`. Do not start tagging while only card titles or partial forms are visible. Opening a classification selector is another wait gate: when the current classification modal shows `暂无数据`, `暂无内容`, `暂无分类`, `暂无标签`, or another short `暂无...` empty state, Soda cancels only that modal, waits with exponential backoff, then reopens it. Redfruit cancels the modal, refreshes the host page, waits for the material form to be ready again, then reopens it with exponential backoff. If a required delivery product/platform value is missing, retry the field selection locally first; when it still cannot be verified, refresh the entry page and reopen the delivery modal with exponential backoff, up to 10 reopen attempts. Only after the tenth failed recovery is the batch stopped with the missing-value error.
8. Read current task ID from a task input.
9. Wait for upload `全部成功`, fail on upload `已失败`.
10. For Soda Music, submit review; Soda writes `review_submitting` before the click and `review_submitted` after confirmation. Redfruit does not submit review.
11. For Redfruit, after the upload task is `全部成功`, open that upload task's detail, enter `素材/文案列表查看`, read CIDs and material types, then start ARLP. For Soda, open the review task detail, read CIDs, read material type by CID, and mark items success. A failed Soda review task retries only that exact row up to 3 times.

## Upload

`_upload_files` waits for `input[type='file']`, clicking `点击或拖拽文件至此区域` or `上传` while waiting. It sets all item paths at once with `set_input_files`, clicks `点我开始上传` if present, and checks page text for limit-zero and upload failure messages.

Upload-card recovery is row-scoped. The browser first waits for a concrete failed upload row, then reads that row's visible text or red-exclamation tooltip. It must not click a page-level `点击重试` or sweep arbitrary icons/buttons.

If a single uploaded row turns red with `上传检测失败：该文件曾经被上传...`, the browser first deletes that failed row, records its `creative_unit_id`, and removes it from the current batch. After the normal rows finish, it returns to `工单管理 -> 创意单元`, searches those `creative_unit_id` values with commas, and clicks `录入素材` for the reused creative units. If the platform shows `以下创意已录入为素材`, the runner maps every returned `创意id=<material_id>已录入,cid=<CID>` pair back through `existing_material_id`, records only the explicitly returned materials as successful, and confirms the dialog. If the dialog covers only part of the selected materials, it then unchecks those already-recorded creative-unit rows and continues the same entry flow with the remaining rows. If all selected materials were already recorded, the recovery branch ends without opening a new entry task. This recovery and its row metadata are checkpointed for both Soda Music and Redfruit.

The already-recorded dialog must contain concrete creative IDs that can be matched to the current batch. An unparseable dialog or an ID that does not belong to the selected batch is a hard stop; the runner must never mark the whole batch successful from a generic `已录入为素材` message. On resume, items already marked `success` or `skipped` are excluded from both upload and existing-creative-unit recovery.

If the failed row itself exposes `点击重试`, the browser clicks only that row's retry action and checkpoints an independent retry count for both workflows. A row may be retried up to three times. The run stops for that material only after the third row-scoped retry still fails. If the failure reason, duplicate creative-unit ID, delete control, or retry button is still loading or temporarily not clickable, keep polling that same row instead of ending the whole batch or clicking unrelated controls.

The CLI also supports direct redfruit recovery when the original creative-unit IDs are already known. It reuses the same row-scoped search and cross-page selection logic without creating an upload page or re-uploading files.

Upload retry has two layers:

- The upload-page layer has no fixed attempt limit. While the headed browser remains open and the page has no explicit business-fatal message, blank/loading pages, missing controls, navigation failures, temporary quota `0`, and upload initialization failures remain in the current upload stage with bounded exponential backoff. A visible quota `0` never counts as a confirmed file-count limit; only a stable positive quota smaller than the selected file count blocks the batch.
- The material-row layer remains bounded: an explicit row-level `点击重试` is attempted at most three times for that one material. Exhausting those three row retries is an explicit material failure and may stop that batch according to the existing batch policy.

- `_upload_files` has no artificial attempt limit for recoverable upload-page states. A blank/loading page, missing or not-yet-initialized file input, temporary `创意单元上限: 0`, or a create-entry click with no immediate effect keeps the headed browser open and continues waiting/recovering until the upload page is actually ready, the upload succeeds, or the user cancels/closes the browser. It must recognize current quota text such as `最多可上传的创意单元数量为:250225` and wait for a positive stable quota before selecting files.
- Do not reload the entire work-order page merely because the upload component temporarily reports limit `0`; remain in the current upload dialog and wait for the component state to become ready. Reopen the create entry only for other recoverable upload failures that leave a broken partial page.
- `_upload_and_enter_chameleon_with_retry` retries the upload plus enter-chameleon sequence up to `max_status_retries` when the failure looks transient.
- For Redfruit only, after an upload operation task has been created, a target task row that explicitly becomes `已失败` uses that row's `重试` action up to 3 times. It never clicks another task row and does not restart the whole upload batch. The third failed retry stops the run with the task ID and retry count.

The no-limit rule applies only while the flow can still make progress. Explicit non-recoverable business conditions still stop the current batch: user cancellation/manual browser close, order deadline/closed/unavailable, blocking Redfruit preflight mismatch, file count exceeding a confirmed positive quota, or a terminal row/task failure after its documented row-scoped retries.

## Chameleon Entry And Tags

After upload cards are ready, the browser clicks `继续编辑`, `确认提交`, selects all creative units, clicks `录入素材`, waits for a page containing `投放平台`/`汽水音乐`, confirms the delivery modal, then fills the first material card and uses `一键复用`.

Chameleon tag strategy:

- The browser layer assumes one upload batch contains one song/type combination, so all selected material cards can share the first card's tags.
- Fill the first card with defaults and custom tags, then click `一键复用` -> `全选` -> `一键复用`, then `提交` -> `查看任务详情`.
- Do not put different songs into one browser upload batch unless the upstream planner/UI has split them first.

Important current assumptions:

- Browser filling uses the first active item in the batch as the source for shared classification/custom tags. Upstream batching must keep each browser batch homogeneous.

Default form choices:

- Redfruit short-drama cards enter with `制作团队` already set to `素材供应商 / 千沧`; do not open or click this field.
- `请选择UGC内容` -> `不包含`.
- Soda Music and Redfruit both must verify that the field is actually set to `不包含`; a failed UGC selection stops the current card instead of continuing with an incomplete form.
- Wait for the delivery modal to be fully closed before filling the first material card. The UGC check only passes when that field visibly contains `不包含` and no longer contains `请选择`.
- Cascader `汽水音乐-素材类型` -> `汽水音乐-素材类型 / LUNA_剪辑制作 / LUNA_自产`.
- Cascader `LUNA素材来源` -> `LUNA素材来源 / LUNA_千沧代理`.
- Cascader `LUNA功能卖点` -> `LUNA功能卖点 / <classification_path_for_material(file)>`.
- Custom tags from `item.custom_tags`, with a fallback to `custom_tags_for_material`.
- Radio `未成年人内容` -> `已授权`.
- Radio `影视内容` -> `已授权`.
- `一键复用` -> `全选` -> `一键复用`, then `提交` -> `查看任务详情`.
- If the first tag submission shows `以下素材的自定义标签BID校验失败，请查验`, treat it as a post-tagging confirmation warning rather than an upload/preflight failure: click `确认` inside that exact modal, click `提交` again, and continue the normal task-detail wait. This does not weaken the blocking Redfruit order/Mogong/BID preflight before upload.

## Review And CID Backfill

`_submit_review` clicks `送审`, confirms `确定`, then optionally clicks `查看任务详情`; it is used only by Soda Music. Redfruit goes from the successful upload task directly to `查看任务详情 -> 素材/文案列表查看 -> CID -> ARLP` and never calls `送审`.

`_fill_cids_for_task` searches the task by ID, waits for row success, opens `素材/文案列表查看` or related text, reads CIDs from the global search input, and requires at least as many CIDs as items. It zips item order with CID order. For Soda, an explicit failed review row uses only that row's `重试` action. The retry key is `汽水音乐送审:<task_id>`, the count is checkpointed, and the total limit remains 3 even when a browser/network failure restarts the run. A terminal failed review task is not allowed to fall through to CID scraping.

If task status keeps refreshing for a long time without reaching `全部成功`, the browser can first open `查看详情`, read CIDs, write them back to Excel, and mark the row note as `未送审`. This is a backup path, not the normal success path.

## Workflow Checkpoints

Both workflows write stage transitions, task IDs, operation-task retry counts, item statuses, CID values, CID material types, and workflow metadata to the task folder. Soda resume covers upload processing, upload task completion, `review_submitting`, `review_submitted`, CID backfill, and the explicit `未送审` CID-backup terminal state. Resuming any Soda review/CID stage skips file upload. Network/navigation failures remain recoverable and do not clear these checkpoints; a user manually closing the browser still stops without automatic restart.

## Redfruit ARLP Completion

Redfruit orders are checkpointed independently of the browser session. The browser records the current order stage, upload task ID, current ARLP task ID, `arlp_stage_index`, completed `arlp_stage_task_ids`, and per-material CID/status before continuing. It also records `arlp_stage_progress`, one entry per ARLP configuration, including `selection_started`, `modal_open`, `submitting`, `task_created`, `waiting_result`, `partial_failure`, or `success`, the attempt number, task ID, total/success/failure counts, and the last error. A browser or network interruption can therefore resume from the exact upload/CID/ARLP step without re-uploading files or recreating a submitted ARLP task. A task ID already listed in `arlp_stage_task_ids` must never advance `arlp_stage_index` again. The CLI resume entry is documented in `references/standalone-cli.md`.

When every active item already has a unique CID, a Redfruit resume in the upload/CID/ARLP stages goes straight to `素材管理`, searches the saved CIDs, verifies the expected result count, and reuses the existing ARLP completion loop. It does not wait on an old review task. When `upload_processing` has no task ID, it also refuses to restart file upload; it can only recover saved original creative-unit IDs, otherwise it records a blocking checkpoint error. If that recovery opens an `已录入为素材` result, the browser confirms the dialog and marks only the returned recovered items successful. It writes `stage=completed` only when every active item is finished; a partial recovery keeps `stage=upload_processing`, records the remaining count, and blocks re-upload until the remaining original creative-unit IDs are available.

After a redfruit upload task succeeds, the browser opens its `查看任务详情 -> 素材/文案列表查看`, reads the CIDs, clicks one material card to enter selection mode, then uses `全选 -> 全选所有` before `编辑 -> 增加ARLP`. Complete these configurations in order, with a separate all-material success gate and checkpoint after each stage:

1. `红果免费短剧(8662)` + `红果免费漫剧(8704)` + `番茄免费小说(1967)` on `广点通、头条内广、穿山甲联盟、union_app、粉丝通、内广-DPA、UC浏览器、sem`.
2. `番茄畅听(3040)` on `广点通、头条内广、穿山甲联盟、union_app、sem`.
3. `蛋花免费小说(507427)` on `union_app、头条内广、广点通、穿山甲联盟`.

The ARLP submission creates a separate operation task. The browser opens `查看任务详情` and reads the task row's `总任务数`, `执行成功数量`, and `执行失败数量` instead of treating the creation-success dialog as the final result:

- When the selection counter equals the batch material count, the operation task `总任务数` must also equal that same batch count, with `执行成功数量 == 总任务数` and `执行失败数量 == 0`; only then is that ARLP stage complete. After stage 3 passes the same gate, the Redfruit workflow is complete. A `20/20` task cannot finish a 32-material batch.
- When the task reaches a terminal partial result, the browser logs the task ID and counts, closes the result dialog, refreshes the material list, clears the old selection, and repeats `点第一张素材 -> 全选所有 -> 增加ARLP`.
- When the ARLP operation task explicitly becomes `已失败`, the browser first clicks that exact task row's `重试` action up to 3 times. If the third retry still fails, it stops with an error instead of creating an unlimited chain of new failed tasks. Partial-success ARLP tasks retain the existing missing-item completion loop.
- The retry loop has no artificial attempt limit. It keeps refreshing and retrying until all selected materials are reported successful or the user cancels the run.

Typical progress messages are `ARLP 第 N 轮结果：任务 <id>，成功 X/Y，失败 Z`, `ARLP 部分成功...重新增加 ARLP`, and `ARLP 全部成功...`. The task and browser diagnostics continue to be written to the normal `run.log` and `debug/` artifacts.

After all three ARLP stages are fully successful, the Redfruit workflow records `stage=completed` and does not open or wait for a final `修改分类标签` modal. All classification paths are filled during the initial 录入素材 step.

## Redfruit Preflight

Redfruit runs a blocking preflight after the work-order search and before `新建创意单元`:

1. Read the order title from the search result row and normalize the drama/order category to `动态漫`, `仿真人`, or `纯短剧`; accept `真人剧` and `真人实拍短剧` as pure-short-drama aliases. For fission filenames only, the standalone token `短剧` also normalizes to `纯短剧`.
2. Compare that against the batch-derived drama category from the selected videos. Material-mode labels such as `AI前贴`/`AI后贴` are classification/custom-tag inputs and do not affect this preflight category comparison.
3. Open the short-drama insight page at `https://usergrowth.com.cn/aigc/insight/business/playlet?source=13`.
4. Search each drama title by name, not BID.
5. Read the title-label line from the card, such as `女频 / - / - / 3D动画漫剧`.
6. Compare the card label kind against the file-derived kind.
7. Click the card `ID` button when needed and compare the expanded `BID` against the expected BID.
8. If the user-specified order does not match the selected batch, report it explicitly as `用户指定订单与这批素材不符` or `用户指定订单与墨攻短剧选剧不符`.

Any mismatch raises a loud failure and stops the run before upload.

Unknown file/order types are also blocking errors. Do not silently treat them as dynamic drama. Pure-short-drama cards use `番茄/红果小说功能卖点 > 短剧 > 纯短剧内容 > 纯短剧`. Their fixed custom tags are `dxzc、短剧洞察、生产赋能专项、DX6、dxcz-番茄测试、未成年人已授权、影视版权已授权`, plus `纯短剧`, the editor abbreviation, `bid_<BID>`, and `创意AI素材` only for AI pre-roll files. Existing dynamic/virtual-human fixed tags remain unchanged.

Fallback CID reading clicks `查看详情`, tries `一键复制对象id`, then extracts CIDs from body text. `_read_material_type_by_cid` opens `查看素材` for the CID row and extracts `分类标签` for backfill material type.

## Debug And Cancellation

`_snapshot_error` writes `debug/<name>.txt`, `debug/<name>.png`, and details to `debug/run.log`. Normal snapshots currently return early unless called with `screenshot=True`.

For diagnosis, read `diagnostic_summary.json` first, then `debug/events.jsonl`, then the referenced `debug/run.log` and snapshots. `events.jsonl` records structured checkpoints and row-level recovery actions but is not read by the browser and cannot change the workflow.

Cancellation is a threading event watched by `_watch_cancel`; when set, the browser is closed so Playwright waits are interrupted and item/plan statuses become `cancelled`.

For Soda/Redfruit ordered plan lists, a normal plan failure is checkpointed and logged as `plan_failed_continue`, then the next user-requested plan starts in the same live browser session. Only cancellation/manual browser close is allowed to terminate the remaining ordered plans.
