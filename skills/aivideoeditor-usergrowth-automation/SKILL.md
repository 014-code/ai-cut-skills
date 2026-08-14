---
name: aivideoeditor-usergrowth-automation
description: "Standalone UserGrowth automation skill for AIVideoEditor desktop upload and post-upload tagging workflows. Use when Codex needs to select specific videos or run multi-batch concurrent or explicit serial UserGrowth 自动上传 by skill scripts only, including Soda Music upload/CID backfill and Redfruit short-drama upload with 动态漫/仿真人/纯短剧 preflight, three-stage ARLP, classification tags, checkpoint resume, dry-run planning, batch manifests/concurrency, song-library matching, 回填 Excel, CID 后歌曲名称列, Playwright upload/录入变色龙/送审/CID 回填, 素材分类标签/自定义标签, 飞书 Wiki/Sheets OpenAPI 跨 Sheet 歌名到 bookid/BID 匹配与回填, 番茄音乐按 CID 批量追加 bid_BID 标签, ddddocr 验证码, task.json/run.log/debug snapshots, or debugging the copied automation implementation."
---

# AIVideoEditor UserGrowth Automation

## Overview

Use this skill to run the UserGrowth automation as a standalone skill. The runnable implementation lives in `scripts/`: it vendors the UserGrowth automation package and provides a CLI that can select exact videos, auto-split a folder into song batches, do dry-run planning, or perform live browser upload after explicit confirmation.

The original repo can still be inspected for comparison, but execution should use this skill's scripts first.

This skill intentionally excludes PyInstaller/exe packaging and release tasks unless the user explicitly asks for packaging again.

## Before Acting

1. For any run or "指定视频上传" request, read `references/standalone-cli.md` and use `scripts/usergrowth_upload.py`.
2. Prefer dry-run first. Live upload requires an explicit user request and the CLI flags `--live --confirm-live`.
3. Do not store or echo credentials. Prefer `USERGROWTH_ACCOUNT` and `USERGROWTH_PASSWORD`.
4. If modifying the standalone implementation, edit files under `scripts/usergrowth_automation/` and then run the validation guidance.

For the post-upload Tomato Music workflow (按 CID 批量给素材追加 `bid_<BID>` 自定义标签), use `scripts/tomato_music_tagging.py`. Prefer the official Feishu Wiki + Sheets OpenAPI for online song/BID lookup and BID writeback; repeat `--feishu-library-url` for every审核人员单曲查询表. Do not automate Feishu grid reads or edits through browser clicks. Keep Playwright only for the UserGrowth/墨攻 material-management steps. When tenant data scope is not configured, use `--feishu-user-oauth` to obtain a domestic Feishu `user_access_token` with PKCE; it inherits the authorizing user's document permissions. Add `--feishu-oauth-persist` for long-lived authorization: the access/refresh tokens are stored only in a Windows CurrentUser DPAPI-encrypted cache and refreshed automatically, while the App Secret stays environment-only. For a business user's one-time setup, use `--feishu-oauth-bootstrap`: on a cache miss it reads `FEISHU_BOOTSTRAP_ACCOUNT` and `FEISHU_BOOTSTRAP_PASSWORD` (or prompts securely), completes the visible domestic Feishu login/consent, and implies persistent caching; on later runs it skips both credentials and the browser. Use `--feishu-oauth-reauthorize` only to replace an expired/revoked cache through a fresh consent flow. Require `--feishu-writeback --confirm-feishu-writeback` for online sheet changes and `--live --confirm-live` for UserGrowth tag changes.

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
- Browser upload, login, order search, 录入变色龙, review, CID scraping, Soda/Redfruit retry and checkpoint recovery, and Redfruit `动态漫/仿真人/纯短剧` preflight, three-stage ARLP, and post-review classification: read `references/browser-flow.md`.
- Errors, flaky selectors, missing dependencies, locked Excel, debug screenshots/logs: read `references/failure-playbook.md`.
- Test selection and verification expectations: read `references/validation.md`.

## Script Entry

Primary CLI:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py --help
```

The CLI supports `--video`, `--video-glob`, `--video-list`, `--all-videos`, `--split-by-song`, direct existing-creative-unit recovery with repeated `--existing-creative-unit-id`, Soda/Redfruit `--resume-task`, single-run JSON manifests, and multi-batch manifests with top-level `batches` plus `--concurrency`. When the user gives an explicit batch order or asks to run batches one by one, set `--concurrency 1`; the queue preserves manifest order. A normal batch/business failure is written as `failed` and the next user-requested batch is still started. Network/page recovery remains inside the current batch. Only user cancellation or manual closure of the headed browser stops the remaining queue. It fails when a requested selector does not match any video.

For Redfruit, require every file name to identify exactly one supported drama type: `动态漫`, `仿真人`, or `纯短剧` (`真人剧` and `真人实拍短剧` are aliases). Run the blocking order/Mogong/BID preflight before upload. Never guess an unknown type. After review, finish all three ARLP product/platform stages in order, checkpoint each stage, then apply the drama-type-specific post-review classifications. Read the exact products, platforms, tags, and classification paths from `references/browser-flow.md`.

## Safety Rules

- Do not run a real UserGrowth upload, submit review, or write production Excel unless the user explicitly asks for a live run and provides the target inputs.
- Do not modify an online Feishu sheet without explicit approval and both Feishu writeback flags. Run the API path read-only first and inspect `task.json.feishu` conflicts and planned writes.
- Keep Feishu access tokens and app secrets in environment variables; never write them to manifests, logs, task files, or source code. `--feishu-oauth-bootstrap` reads the Feishu account/password only on the first cache miss; use separate `FEISHU_BOOTSTRAP_*` variables from the UserGrowth login variables and clear them after first authorization.
- Do not echo, persist, or add hard-coded credentials.
- Treat a user manually closing the headed browser as an explicit stop: do not automatically relaunch it. Network failures, page crashes, and abnormal browser exits may still use bounded checkpoint-based automatic recovery.
- A requested batch list is an execution contract: do not silently drop, reorder, merge, or terminate later batches because an earlier batch failed. Serial runs must log every planned batch and its final status; concurrent runs must collect every child result. After retry limits are exhausted, record the failed batch and continue. A final `partial_success`/`failed` summary is valid only after all non-cancelled batches have been attempted.
- While a workflow stage can still make progress, do not end it because a fixed page wait or retry count elapsed. This is strictest during file upload: blank/loading pages, missing upload controls, temporary quota `0`, navigation failures, and upload-component initialization errors stay in the upload stage and recover with bounded backoff. Only explicit business blockers or a material row that still fails after three row-scoped retries may end the batch.
- Live mode writes successful orders directly to the original backfill Excel and submits review on UserGrowth.
- Keep standalone execution changes scoped to this skill unless the user asks to sync changes back into the project.
