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

- Excel changes
  Use temporary `.xlsx` fixtures. Confirm header alias detection, `歌曲名称` insertion after `CID`, no overwrite of existing CID rows, missing-song-ID remarks, and dry-run versus live `include_ready` behavior.

- Song library changes
  Verify header-row detection, link-to-track-ID resolution, duplicate song export, exact matching, blocked song skip, and missing song ID warnings.

- Runner/batch changes
  Verify cancellation, summary counts, `task.json`, `run.log`, dry-run result path, live original-Excel path, and same-path backfill lock behavior.

- Browser changes
  Use `headless=False` for diagnosis, collect `debug/` artifacts, and test against a safe order before production. Check login, work-order search, create creative unit, upload input, chameleon modal, cascaders, chameleon tag strategy (`reuse_all`, common-tags-plus-per-card-song-id, and per-card full fill), review, task polling, CID extraction, and Excel write callback.

- Standalone CLI changes
  Run `--help`, `py_compile`, and a dry-run using temporary `.mp4` placeholders plus temporary song/backfill workbooks. Confirm only selected videos appear in `task.json` and `result.xlsx`.

- Standalone batch CLI changes
  Run a dry-run manifest with at least two `batches`, one batch using explicit `videos`, one using `all_videos=true`, and one `--split-by-song` or `split_by_song=true` case. Confirm `batch_runs/<timestamp>/batch_summary.json`, aggregate `run.log`, and each child `task.json/run.log/result.xlsx` exist. Also confirm the recorded concurrency is at least `2` when there are multiple batches.

## Reporting

When tests cannot be run or live upload is intentionally skipped, say that plainly and include the residual risk. For flaky live failures, report the latest debug snapshot names and the exact step where the flow stopped.
