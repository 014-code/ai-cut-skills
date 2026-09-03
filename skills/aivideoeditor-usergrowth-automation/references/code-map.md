# UserGrowth Code Map

The standalone implementation in this skill is authoritative. Edit the files below, then sync the skill runtime; do not modify a guessed desktop mirror.

## Entrypoints

- `scripts/usergrowth_upload.py`
  Soda Music and Redfruit upload CLI: selected files, manifests, automatic song splitting, batch concurrency, checkpoints, and `--resume-task`. It accepts only `soda_music` and `redfruit_short_drama`; Tomato Music is deliberately rejected here.

- `scripts/tomato_music_tagging.py`
  Tomato Music CID-to-`bid_<BID>` tagging CLI, including Feishu lookup/writeback and independent Tomato checkpoints. It does not upload videos or run Redfruit stages.

- `scripts/requirements.txt`
  Runtime dependencies for the standalone tool.

## Core Package

- `scripts/usergrowth_automation/usergrowth_models.py`
  Shared dataclasses, status carriers, and supported video suffixes.

- `scripts/usergrowth_automation/usergrowth_planner.py`
  Soda song-batch planning and Redfruit file-to-metadata planning.

- `scripts/usergrowth_automation/usergrowth_rules.py`
  Soda material detection, song-name extraction, classification paths, and template-related tag rules.

- `scripts/usergrowth_automation/usergrowth_redfruit.py`
  Redfruit filename metadata, drama type, preflight normalization, combined entry-time classification paths, and three ARLP stages.

- `scripts/usergrowth_automation/usergrowth_browser.py`
  Shared Playwright login, upload, Chameleon, review, CID, Redfruit state machine, ARLP, classification, retries, diagnostics, and session recovery.

- `scripts/usergrowth_automation/usergrowth_tomato_music.py`
  Tomato material management, CID search/chunking, and strict `bid_<BID>` custom-tag application.

- `scripts/usergrowth_automation/usergrowth_excel.py`
  Soda song/backfill workbook loading and writeback, including the `CID`-adjacent `歌曲名称` column.

- `scripts/usergrowth_automation/usergrowth_feishu_sheets.py` and `feishu_oauth.py`
  Official Feishu Wiki/Sheets API, PKCE/bootstrap authorization, and optional writeback.

- `scripts/usergrowth_automation/usergrowth_session_cache.py`
  Account-scoped Windows DPAPI UserGrowth login cache shared by all three workflows.

- `scripts/usergrowth_automation/usergrowth_runner.py`, `usergrowth_captcha.py`, and `usergrowth_tag_templates.py`
  Desktop-compatible batch runner, captcha OCR wrapper, and Soda custom-tag template handling.

## Legacy UI Reference

`D:\linan\pro\aivideoeditor-backend\material_remix_desktop_source\app\tk_ui.py` remains only as a legacy desktop UI reference. Its former sibling `app/usergrowth_*.py` modules are no longer present there; do not treat those paths as editable or sync targets.

## Usual Edit Points

- Change Soda filename/material/tag rules in `usergrowth_rules.py`; verify planner and browser tag fill together.
- Change Redfruit preflight, ARLP, or classifications in `usergrowth_redfruit.py` and `usergrowth_browser.py`; preserve the state machine and checkpoints.
- Change song matching or Excel backfill in `usergrowth_excel.py`; test temporary workbooks.
- Change browser selectors or live platform behavior in `usergrowth_browser.py`; use debug snapshots and avoid live upload unless explicitly authorized.
