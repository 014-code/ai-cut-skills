# Browser Automation Flow

## Dependencies And Launch

`UserGrowthBrowserClient.run` lazily imports Playwright. Missing Playwright raises `需要先安装 playwright，并执行 playwright install chromium`. The client launches Chromium with local browser channels in this order: `msedge`, then `chrome`. Viewport is `1440x1000`; slow motion is controlled by `browser_slow_mo_ms` and `USERGROWTH_OPERATION_SPEED_FACTOR`.

Login uses `https://usergrowth.com.cn/open/login`, then navigates/checks `https://usergrowth.com.cn/home`. Captcha recognition uses `UserGrowthCaptchaSolver` and `ddddocr`. Login retries up to 5 times and considers `/home`, `墨攻AI`, or `采购中心` as logged-in signals. After login, image/font/favicon requests are blocked.

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

## Redfruit Preflight

Redfruit runs a blocking preflight after the work-order search and before `新建创意单元`:

1. Read the order title from the search result row and normalize it to `动态漫` or `仿真人`.
2. Compare that against the file-derived kind from the selected videos.
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
