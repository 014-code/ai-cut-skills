---
name: aivideoeditor-usergrowth-automation
description: "Standalone UserGrowth automation skill for AIVideoEditor desktop upload and post-upload tagging workflows. Use when Codex needs to select specific videos or run multi-batch concurrent or explicit serial UserGrowth 自动上传 by skill scripts only, including Soda Music upload/CID backfill and Redfruit short-drama upload with 动态漫/仿真人/纯短剧 preflight, direct upload-task CID extraction, three-stage ARLP, classification tags, checkpoint resume, dry-run planning, batch manifests/concurrency, song-library matching, 回填 Excel, CID 后歌曲名称列, Playwright upload/录入变色龙/送审/CID 回填, 素材分类标签/自定义标签, 飞书 Wiki/Sheets OpenAPI 跨 Sheet 歌名到 bookid/BID 匹配与回填, 番茄音乐按 CID 批量追加 bid_BID 标签, ddddocr 验证码, task.json/run.log/debug snapshots, or debugging the copied automation implementation."
---

# AIVideoEditor UserGrowth Automation

## Overview

Use this skill to run the UserGrowth automation as a standalone skill. The runnable implementation lives in `scripts/`: it vendors the UserGrowth automation package and provides a CLI that can select exact videos, auto-split a folder into song batches, do dry-run planning, or perform live browser upload after explicit confirmation.

The original repo can still be inspected for comparison, but execution should use this skill's scripts first.

This skill intentionally excludes PyInstaller/exe packaging and release tasks unless the user explicitly asks for packaging. When requested, the standalone desktop launcher is built from `desktop/usergrowth_desktop.py`; it only calls the two existing CLI entrypoints and does not duplicate workflow logic.

## Before Acting

1. For any run or "指定视频上传" request, read `references/standalone-cli.md` and use `scripts/usergrowth_upload.py`.
2. Prefer dry-run first. Live upload requires an explicit user request and the CLI flags `--live --confirm-live`.
3. Do not store or echo credentials. Prefer `USERGROWTH_ACCOUNT` and `USERGROWTH_PASSWORD`.
4. If modifying the standalone implementation, edit files under `scripts/usergrowth_automation/` and then run the validation guidance.

Soda Music upload, Redfruit short-drama upload, and Tomato Music CID/BID tagging automatically share one account-scoped UserGrowth login session. The standalone cache is encrypted with Windows CurrentUser DPAPI, validated against the authenticated home route before use, replaced after a fresh captcha login, and never written to task manifests or logs. Explicit `--storage-state`/`--storage-state-output` bridge files remain available to platform integrations and take precedence over the automatic cache.

For the post-upload Tomato Music workflow (按 CID 批量给素材追加 `bid_<BID>` 自定义标签), use `scripts/tomato_music_tagging.py`. Prefer the official Feishu Wiki + Sheets OpenAPI for online song/BID lookup and BID writeback; repeat `--feishu-library-url` for every审核人员单曲查询表. Do not automate Feishu grid reads or edits through browser clicks. Keep Playwright only for the UserGrowth/墨攻 material-management steps. When tenant data scope is not configured, use `--feishu-user-oauth` to obtain a domestic Feishu `user_access_token` with PKCE; it inherits the authorizing user's document permissions. Add `--feishu-oauth-persist` for long-lived authorization: the access/refresh tokens are stored only in a Windows CurrentUser DPAPI-encrypted cache and refreshed automatically, while the App Secret stays environment-only. For a business user's one-time setup, use `--feishu-oauth-bootstrap`: on a cache miss it reads `FEISHU_BOOTSTRAP_ACCOUNT` and `FEISHU_BOOTSTRAP_PASSWORD` (or prompts securely), completes the visible domestic Feishu login/consent, and implies persistent caching; on later runs it skips both credentials and the browser. Use `--feishu-oauth-reauthorize` only to replace an expired/revoked cache through a fresh consent flow. Require `--feishu-writeback --confirm-feishu-writeback` for online sheet changes and `--live --confirm-live` for UserGrowth tag changes.

## Online MCP

The published online package exposes three server-side Tools for this workflow:

- `tomato_music_tag_plan`: use a platform `feishuConnectionRef`, source-sheet URL, and BID-library URLs to generate a read-only plan. The server resolves the caller's saved 流量洞察墨攻账号; never send a UserGrowth password or Feishu token through MCP.
- `tomato_music_tag_run`: accepts the returned `planId` and `confirm=true`, then starts the server-side Playwright run. It only marks Feishu rows `已打标` after the matching 墨攻 operation task reports the exact expected total, all success, and zero failed.
- `tomato_music_tag_status`: returns the plan, task state, and per-chunk operation evidence.

When one platform user has only one UserGrowth configuration, `usergrowthAccountRef` defaults to `default`. The future multi-account selector must use a non-secret account reference and masked display name, never a raw account/password in MCP input.

For multiple Tomato Music BID batches, use `--concurrency N` to run at most `N` independent browser sessions inside the same process and OAuth token. Each browser handles one BID at a time; completed slots pull the next BID. Keep per-BID debug folders, preserve input-order result collection, and let one ordinary batch failure continue the remaining queue.

For Feishu BID lookup, require both song name and artist to match after whitespace removal and case folding. Never fall back to song-name-only matching. Treat a missing artist or a same-song/same-artist pair with multiple BIDs as unresolved and exclude it from live tagging.

When the source has a `打标状态`/`标签状态` column, skip rows already set to `已打标`. In a Feishu-backed live run, change pending rows to `已打标` only after the corresponding 墨攻 operation task reports the exact expected total, all successes, and zero failures, and the Sheets API reread verifies the update.

## Workflow Change Boundary

This boundary applies to both Soda Music and Redfruit short-drama automation.

- Treat the existing end-to-end stage order, shared orchestration, batch behavior, checkpoint semantics, and completion criteria as a stable contract. Do not change them when a selector, wait, retry, pagination, tag, or single-page issue can be fixed locally.
- Prefer the smallest scoped fix inside the failing step. Preserve the other workflow, existing stage order, existing successful paths, and current input/output contracts.
- An overall-flow change includes adding, removing, reordering, skipping, or merging stages; changing upload -> Chameleon entry -> review -> CID/ARLP/classification sequencing; changing automatic batching/concurrency; changing checkpoint or resume meaning; or weakening success-count validation and fallback behavior.
- If an overall-flow change is genuinely necessary, stop before editing. Explain why a local fix is insufficient, list the affected workflows/files/stages and expected behavioral impact, then ask the user for explicit confirmation. Only proceed after the user clearly approves that specific flow change.
- A request to fix a bug, retry a run, sync the skill, or improve robustness is not implicit permission to redesign the overall flow.

## Task Routing

- Running the standalone tool, selecting exact videos, manifests, dependency setup: read `references/standalone-cli.md`.
- Tomato Music Feishu song-to-BID lookup/writeback: read `references/feishu-sheets-api.md`, then use `scripts/tomato_music_tagging.py`.
- Tomato Music CID-to-BID tagging in 墨攻 after upload: read `references/standalone-cli.md` and `references/browser-flow.md`, then use `scripts/tomato_music_tagging.py`.
- Workflow, task outputs, batch behavior, dry-run/live split: read `references/workflow.md`.
- Excel backfill, CID, song-name column, song library, duplicate/blocked songs: read `references/excel-contract.md`.
- Browser upload, login, order search, 录入变色龙, review, CID scraping, Soda/Redfruit retry and checkpoint recovery, and Redfruit `动态漫/仿真人/纯短剧` preflight and three-stage ARLP: read `references/browser-flow.md`.
- Errors, flaky selectors, missing dependencies, locked Excel, debug screenshots/logs: read `references/failure-playbook.md`.
- Test selection and verification expectations: read `references/validation.md`.

## Workflow Isolation

The three UserGrowth workflows are separate contracts. Route only from the user's explicit business target; never infer or switch a workflow from an order ID, customer ID, file-name fragment, tag, or a previous task folder. If the target is not explicit, stop and ask which workflow is required. `usergrowth_upload.py` defaults only an omitted workflow to Soda Music; every explicitly supplied unknown workflow, including `tomato_music`, is rejected instead of falling back to Soda.

| User target | Workflow and entry | Must not be mixed in |
| --- | --- | --- |
| 汽水音乐 upload | `workflow=soda_music` through `scripts/usergrowth_upload.py` | Song-library matching, song batching, template tags, CID Excel/song-name backfill, upload -> Chameleon -> review -> CID. Do not run Redfruit preflight or ARLP. |
| 红果短剧 upload | `workflow=redfruit_short_drama` through `scripts/usergrowth_upload.py` | Filename/order/Mogong/BID preflight, Redfruit tags, upload -> task detail/CID -> three ordered ARLP stages. Redfruit skips review. Do not use Soda song splitting, song templates, song-library matching, or Excel backfill. |
| 番茄音乐 CID/BID tagging | `scripts/tomato_music_tagging.py` only | Existing material CIDs -> exact `bid_<BID>` tags and optional Feishu lookup/writeback. It is not a video-upload, review, CID-collection, Redfruit ARLP, or generic custom-tag flow. |

Do not carry a checkpoint between workflows. Soda resumes only `soda_music_checkpoint.json`; Redfruit resumes only `redfruit_checkpoint.json`; Tomato resumes only `tomato_music_tagging_checkpoint.json`. A checkpoint, CID list, tag template, or task folder from one workflow is invalid input to either of the other two.

## Script Entry

## Desktop Launcher

The optional Windows desktop launcher exposes the same upload and tagging CLI through a Tkinter UI. It supports dry-run preflight, explicit live confirmation, Soda/Redfruit upload parameters, batch manifest execution, resume task paths, and Tomato Music CID/BID tagging. Credentials are held only in memory for the current run.

Build it from the skill root with the project virtualenv:

```powershell
python desktop\build_desktop_exe.py --output-root D:\Users\Donson\Desktop\UserGrowth自动化上传桌面端_YYYYMMDD
```

The build produces the exe, a SHA-256 file, and a zip containing both. Do not use the unrelated `material_remix_desktop_source` build script for this skill.

Primary CLI:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py --help
```

The CLI supports `--video`, `--video-glob`, `--video-list`, `--all-videos`, `--split-by-song`, direct existing-creative-unit recovery with repeated `--existing-creative-unit-id`, Soda/Redfruit `--resume-task`, single-run JSON manifests, and multi-batch manifests with top-level `batches` plus `--concurrency`. When the user gives an explicit batch order or asks to run batches one by one, set `--concurrency 1`; the queue preserves manifest order. A normal batch/business failure is written as `failed` and the next user-requested batch is still started. Network/page recovery remains inside the current batch. Only user cancellation or manual closure of the headed browser stops the remaining queue. It fails when a requested selector does not match any video.

For Redfruit, require every file name to identify exactly one supported drama type: `动态漫`, `仿真人`, or `纯短剧` (`真人剧` and `真人实拍短剧` are aliases; the fission filename token `短剧` also means `纯短剧`). Run the blocking order/Mogong/BID preflight before upload. Never guess an unknown type. After the upload task succeeds, read CIDs from its task detail and finish all three ARLP product/platform stages in order; Redfruit skips review. Checkpoint each stage and complete after the third ARLP success without a final post-review classification pass. Read the exact products, platforms, and tags from `references/browser-flow.md`.

Redfruit's original state machine is mandatory. For a new task, use `usergrowth_upload.py`; once a task folder/checkpoint exists, recover it only with `usergrowth_upload.py --resume-task <task-folder>`. Do not use inline Python, manually constructed `UserGrowthOrderPlan` objects, `TomatoMusicTaggingClient`, or private browser-client methods to jump to upload recovery, ARLP, or classification. The runtime rejects those Redfruit internal calls outside the active formal runner. `tomato_music_tagging.py` is limited to appending the exact `bid_<BID>` tag for its supplied BID; it is not a generic custom-tag writer.

## Safety Rules

- Do not run a real UserGrowth upload, submit review, or write production Excel unless the user explicitly asks for a live run and provides the target inputs.
- Do not modify an online Feishu sheet without explicit approval and both Feishu writeback flags. Run the API path read-only first and inspect `task.json.feishu` conflicts and planned writes.
- Keep Feishu access tokens and app secrets in environment variables; never write them to manifests, logs, task files, or source code. `--feishu-oauth-bootstrap` reads the Feishu account/password only on the first cache miss; use separate `FEISHU_BOOTSTRAP_*` variables from the UserGrowth login variables and clear them after first authorization.
- Do not echo, persist, or add hard-coded credentials.
- Treat a user manually closing the headed browser as an explicit stop: do not automatically relaunch it. Network failures, page crashes, and abnormal browser exits may still use bounded checkpoint-based automatic recovery.
- A requested batch list is an execution contract: do not silently drop, reorder, merge, or terminate later batches because an earlier batch failed. Serial runs must log every planned batch and its final status; concurrent runs must collect every child result. After retry limits are exhausted, record the failed batch and continue. A final `partial_success`/`failed` summary is valid only after all non-cancelled batches have been attempted.
- While a workflow stage can still make progress, do not end it because a fixed page wait or retry count elapsed. This is strictest during file upload: blank/loading pages, missing upload controls, temporary quota `0`, navigation failures, and upload-component initialization errors stay in the upload stage and recover with bounded backoff. Only explicit business blockers or a material row that still fails after three row-scoped retries may end the batch.
- Live Soda mode writes successful orders directly to the original backfill Excel and submits review on UserGrowth. Redfruit live mode reads CIDs from the successful upload task detail and skips review.
- Keep standalone execution changes scoped to this skill unless the user asks to sync changes back into the project.
