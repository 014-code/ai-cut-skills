---
name: aivideoeditor-usergrowth-automation
description: "Standalone UserGrowth automation skill for AIVideoEditor desktop upload and post-upload tagging workflows. Use when Codex needs to select specific videos or run multi-batch concurrent or explicit serial UserGrowth 自动上传 by skill scripts only, including dry-run planning, batch manifests/concurrency, song-library matching, 回填 Excel, CID 后歌曲名称列, Playwright upload/录入变色龙/送审/CID 回填, 素材分类标签/自定义标签, 飞书 Wiki/Sheets OpenAPI 跨 Sheet 歌名到 bookid/BID 匹配与回填, 番茄音乐按 CID 批量追加 bid_BID 标签, ddddocr 验证码, task.json/run.log/debug snapshots, or debugging the copied automation implementation."
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

For the post-upload Tomato Music workflow (按 CID 批量给素材追加 `bid_<BID>` 自定义标签), use `scripts/tomato_music_tagging.py`. Prefer the official Feishu Wiki + Sheets OpenAPI for online song/BID lookup and BID writeback; do not automate Feishu grid reads or edits through browser clicks. Keep Playwright only for the UserGrowth/墨攻 material-management steps. Require `--feishu-writeback --confirm-feishu-writeback` for online sheet changes and `--live --confirm-live` for UserGrowth tag changes.

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
- Browser upload, login, order search, 录入变色龙, review, CID scraping, Soda/Redfruit retry and checkpoint recovery, redfruit ARLP completion/retry, and post-review classification completion/retry: read `references/browser-flow.md`.
- Errors, flaky selectors, missing dependencies, locked Excel, debug screenshots/logs: read `references/failure-playbook.md`.
- Test selection and verification expectations: read `references/validation.md`.

## Script Entry

Primary CLI:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py --help
```

The CLI supports `--video`, `--video-glob`, `--video-list`, `--all-videos`, `--split-by-song`, direct existing-creative-unit recovery with repeated `--existing-creative-unit-id`, Soda/Redfruit `--resume-task`, single-run JSON manifests, and multi-batch manifests with top-level `batches` plus `--concurrency`. Omit concurrency for the existing parallel default; explicitly set `--concurrency 1` for ordered serial execution where a retry-exhausted failed batch is recorded and skipped before the next batch. It fails when a requested selector does not match any video.

## Safety Rules

- Do not run a real UserGrowth upload, submit review, or write production Excel unless the user explicitly asks for a live run and provides the target inputs.
- Do not modify an online Feishu sheet without explicit approval and both Feishu writeback flags. Run the API path read-only first and inspect `task.json.feishu` conflicts and planned writes.
- Keep Feishu access tokens and app secrets in environment variables; never write them to manifests, logs, task files, or source code.
- Do not echo, persist, or add hard-coded credentials.
- Treat a user manually closing the headed browser as an explicit stop: do not automatically relaunch it. Network failures, page crashes, and abnormal browser exits may still use bounded checkpoint-based automatic recovery.
- Live mode writes successful orders directly to the original backfill Excel and submits review on UserGrowth.
- Keep standalone execution changes scoped to this skill unless the user asks to sync changes back into the project.
