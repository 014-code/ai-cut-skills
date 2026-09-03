# Validation Guide

## Before Live Upload

Prefer this order:

1. Dry-run planning with real-looking copied inputs.
2. Focused Excel/rules tests or a temporary workbook script.
3. Manual/live Playwright run only after the user explicitly confirms the real account, order ID, video folder, song Excel, and backfill Excel.

Do not submit real UserGrowth review or write production Excel as a "test" without explicit user authorization.

## Commands

For the standalone skill tool:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py --help
python -m py_compile C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py
```

For Tomato Music post-upload BID tagging, validate both a single JSON batch and the original multi-sheet Excel in dry-run mode:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py --help
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --input 'D:\Users\Donson\Downloads\番茄音乐_bid_cid_dry_run.json' `
  --customer-id 3681575 `
  --bid 7330415052329061438
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --input 'D:\Users\Donson\Downloads\番茄音乐每日打标表.xlsx' `
  --customer-id 3681575
```

Keep both commands in dry-run mode. Confirm the first plan is split into chunks of at most 50 CIDs and the Excel plan reads every eligible sheet without modifying the workbook.

For the Feishu-backed path, first run a read-only preflight against a mock API or an authorized real token. Do not pass either writeback flag during the real-token preflight:

```powershell
$env:FEISHU_ACCESS_TOKEN = '<token>'
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --feishu-source-url 'https://donsontech.feishu.cn/wiki/W5jwwHoxei11cjkWzxZciWJWnCh?sheet=snQZ1w' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/GuUUwCVeoiJLS8k2kTxcBS9Cngh?from=from_copylink' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/PSSYwIoBHi7JY5kB6jGcLpWundb?from=from_copylink&sheet=qTG951' `
  --customer-id 3681575
```

Confirm Wiki token resolution, worksheet enumeration, cross-sheet song/BID matching, repeated-song reuse, conflict reporting, rightmost-column BID insertion planning, BID/CID batch counts, and zero online writes. For mock writeback validation, cover column expansion, `values_batch_update`, and reread verification. A real writeback requires explicit authorization plus `--feishu-writeback --confirm-feishu-writeback`.

To validate the user-permission path, run the same preflight with `--feishu-user-oauth` after registering `http://127.0.0.1:8765/callback`. Confirm the browser callback completes, the output records only `feishu.auth.token_kind=user_access_token` and `token_persisted=false`, and no access token appears in `task.json`, `run.log`, `error.json`, or terminal output. Then run the one-time path with `--feishu-oauth-bootstrap` and temporary `FEISHU_BOOTSTRAP_ACCOUNT/PASSWORD` values: confirm it completes the visible domestic login, saves only a DPAPI cache, and emits no credentials. Clear those variables and run once more with only `--feishu-oauth-bootstrap`: the second run must not open the consent page or prompt for credentials. Finally test `--feishu-oauth-persist --feishu-oauth-reauthorize` and a cache hit; `task.json` may record only `token_persisted=true`, `storage=windows_dpapi`, and `bootstrap=true/false`, and neither token nor App Secret may appear in any artifact or terminal output.

Validate the skill structure:

```powershell
python -X utf8 C:\Users\Donson\.codex\skills\.system\skill-creator\scripts\quick_validate.py C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation
```

When syncing changes back to the original repo, use the repo's local venv when available:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

If desktop tests exist in the checkout, prefer targeted runs such as `tests\desktop\test_usergrowth_rules.py`; verify with `rg --files tests` before promising that target exists.

## What To Verify By Change Type

- Rules/classification/tag changes
  Verify `detect_material_type`, `extract_song_name`, `classification_path_for_material`, template rendering in `custom_tags_for_material`, planner preview values, and browser `_fill_card_defaults`.

- Redfruit drama-type/ARLP/classification changes
  Run `python -m unittest discover -s tests -p 'test_usergrowth_redfruit_three_stage.py' -v`. Verify `动态漫/仿真人/纯短剧` aliases, unknown-type blocking, pure-short-drama fixed/AI-pre-roll tags, all combined entry-time classification paths, exact three-stage ARLP products/platforms, stage-progress serialization, legacy empty-progress compatibility, and no double-counting of a completed ARLP task during resume. This is local validation only; do not claim a live UserGrowth run without explicit authorization and item-level task evidence.

- Excel changes
  Use temporary `.xlsx` fixtures. Confirm header alias detection, `歌曲名称` insertion after `CID`, no overwrite of existing CID rows, missing-song-ID remarks, and dry-run versus live `include_ready` behavior.

- Song library changes
  Verify header-row detection, link-to-track-ID resolution, duplicate song export, exact matching, blocked song skip, and missing song ID warnings.

- Runner/batch changes
  Verify cancellation, summary counts, `task.json`, `run.log`, dry-run result path, live original-Excel path, and same-path backfill lock behavior.

- Browser changes
  Use `headless=False` for diagnosis, collect `debug/` artifacts, and test against a safe order before production. Check login, work-order search, create creative unit, upload input, chameleon modal, cascaders, chameleon tag strategy (`reuse_all`, common-tags-plus-per-card-song-id, and per-card full fill), review, task polling, CID extraction, and Excel write callback. For existing creative units, cover the `已录入为素材` dialog with multiple `创意id/cid` pairs and verify the branch exits immediately with CIDs mapped by `existing_material_id`.
  For login-session changes, verify DPAPI round-trip/account isolation, invalid-cache fallback, atomic concurrent writes, shared cache resolution for Soda/Redfruit/Tomato, and a Tomato context receiving the saved `storage_state`.

- Standalone CLI changes
  Run `--help`, `py_compile`, and a dry-run using temporary `.mp4` placeholders plus temporary song/backfill workbooks. Confirm only selected videos appear in `task.json` and `result.xlsx`.

- Standalone batch CLI changes
  Run a dry-run manifest with at least two `batches`, one batch using explicit `videos`, one using `all_videos=true`, and one `--split-by-song` or `split_by_song=true` case. Confirm `batch_runs/<timestamp>/batch_summary.json`, aggregate `run.log`, and each child `task.json/run.log/result.xlsx` exist. When concurrency is omitted, confirm the recorded concurrency is at least `2` for multiple batches. Also run `--concurrency 1`: confirm ordered execution, a failed batch does not block later batches, and cancellation/manual browser close prevents later serial batches from starting.

- Tomato Music tagging changes
  Run `--help`, `py_compile`, one JSON BID dry-run, and one original multi-sheet Excel dry-run. Confirm CID normalization/de-duplication, `bid_<BID>` labels, maximum 50-CID chunks, `task.json`, `run.log`, and no workbook writes. For `--concurrency 3`, mock at least six BIDs and verify no more than three clients are active, results/checkpoints retain source order, debug directories are per BID, and one child failure does not block later BIDs. Mock an early chunk business failure and confirm later chunks/BIDs still run; mock a network/session failure and confirm the current chunk recovers. Live validation still requires explicit user confirmation and must accept a chunk only when the operation task total/success/failure counts reconcile exactly.

- Feishu Tomato Music changes
  Mock Wiki resolution for the source plus two library URLs, worksheet listing, range reads, a library song duplicated with the same BID, a conflicting song with multiple BIDs across the two tables, a source sheet without a BID column, an existing-BID conflict, column expansion, batch update, and writeback reread verification. Also run an API read-only preflight when an authorized token is available. Never replace failed API authorization with browser spreadsheet automation.

## Reporting

When tests cannot be run or live upload is intentionally skipped, say that plainly and include the residual risk. For flaky live failures, report the latest debug snapshot names and the exact step where the flow stopped.
