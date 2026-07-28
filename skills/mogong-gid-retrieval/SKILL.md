---
name: mogong-gid-retrieval
description: Query normalized Douyin GIDs through the Mogong creative assistant, apply Mogong-specific business filtering, and export matched/unmatched Excel and JSON results. Use for 魔工 GID 查询、业务过滤、结果导出，or compatibility URL/GID/keyword inputs whose generic resolution, Wanbang search, and optional downloads must be delegated to the required douyin-video-toolkit Skill.
---

# Mogong GID Retrieval

## Purpose

Use this skill for Mogong querying, business classification, recovery logs, and result export.

**REQUIRED SUB-SKILL:** Use `douyin-video-toolkit`.

This Skill does not implement short-link resolution, GID extraction, Wanbang search, direct-URL lookup, or video download. Its compatibility URL/GID/keyword inputs call `douyin-video-toolkit/scripts/douyin_reference_core.py`, then keep Mogong state in separate result files.

Main script: `scripts/mogong_gid_retrieval.py`

Dependency file: `scripts/requirements.txt`

## Setup

Install dependencies in the active Python environment:

```bash
python -m pip install -r scripts/requirements.txt
python -m playwright install chromium
```

For Mogong querying, provide credentials by flags or environment variables:

```bash
MOGONG_ACCOUNT=...
MOGONG_PASSWORD=...
MOGONG_CUSTOMER_ID=...
```

For keyword search or delegated video download, also provide Wanbang credentials:

```bash
WANBANG_API_KEY=...
WANBANG_API_SECRET=...
WANBANG_DOUYIN_BASE_URL=...
```

Do not write credentials into generated files. Pass them as environment variables or command arguments for the current run only.

## Quick Start

Generate an input template:

```bash
python scripts/mogong_gid_retrieval.py template --mode url --output input_urls.xlsx
python scripts/mogong_gid_retrieval.py template --mode gid --output input_gids.xlsx
python scripts/mogong_gid_retrieval.py template --mode keyword --output input_keywords.xlsx
```

Run URL mode with Mogong filtering:

```bash
python scripts/mogong_gid_retrieval.py run \
  --mode url \
  --input input_urls.xlsx \
  --output-dir out/mogong_gid \
  --mogong-account "$MOGONG_ACCOUNT" \
  --mogong-password "$MOGONG_PASSWORD" \
  --mogong-customer-id "$MOGONG_CUSTOMER_ID"
```

Run raw GID mode:

```bash
python scripts/mogong_gid_retrieval.py run \
  --mode gid \
  --input input_gids.xlsx \
  --output-dir out/mogong_gid
```

Run keyword mode, then filter found videos through Mogong:

```bash
python scripts/mogong_gid_retrieval.py run \
  --mode keyword \
  --input input_keywords.xlsx \
  --output-dir out/mogong_gid \
  --max-videos-per-keyword 12
```

Add `--download` to send matched GIDs to `douyin-video-toolkit` for atomic download and validation. Use `--download-scope all` only when Mogong was skipped and the user explicitly wants unfiltered downloads.

## Workflow

1. Confirm `douyin-video-toolkit` is installed beside this Skill. Use `--douyin-toolkit-core <path>` only for a nonstandard location.
2. Decide input mode:
   - `url`: Excel/CSV contains Douyin URLs, short links, or cells containing GIDs.
   - `gid`: Excel/CSV contains raw GID values.
   - `keyword`: Excel/CSV contains search keywords; the Douyin Toolkit calls Wanbang to find GIDs.
3. The script delegates normalization/search to the Toolkit and converts its reference contract into `parsed_references.json`.
4. Run the Mogong browser query, classify replies, and optionally delegate matched downloads back to the Toolkit.
5. Inspect outputs in `--output-dir`:
   - `all_results.xlsx`: every parsed GID with query status, Mogong reply, and download status.
   - `matched_urls.xlsx`: only rows whose Mogong query status is `matched`.
   - `summary.json`: counts and output paths.
   - `run_events.jsonl`: JSON-lines stage log for every `run` execution.
   - `failure.json`: only present after an uncaught failure or keyboard interruption.
   - `parsed_references.json`: parsed URL/GID/search references, written before Mogong querying.
   - `partial_results.json`: latest query/download results, useful when the final Excel files were not written.
   - `parse_errors.json`: only present when some inputs cannot be parsed.
   - `debug/`: Mogong browser snapshots and text dumps when querying Mogong.
6. If a Mogong run fails, check `debug/*.txt` first. Failures usually come from missing Toolkit installation, login captcha, customer ID mismatch, page structure changes, or the GID ability not being selected.

## Failure Logs

When a task fails or is interrupted, read logs in this order:

1. `run_events.jsonl`: inspect the last JSON line to see the last completed or failed stage. Key events include `build_references_started`, `build_references_finished`, `mogong_query_started`, `mogong_query_finished`, `download_item_finished`, `download_finished`, `completed`, `failed`, and `interrupted`.
2. `failure.json`: read `status`, `error_type`, `error`, and `traceback`. This file is written for uncaught exceptions and Ctrl-C interruptions after the `run` command has parsed `--output-dir`.
3. `debug/*.txt` and `debug/*.png`: use these for Mogong page-level failures. Filenames identify the stage, such as `customer_not_found`, `assistant_icon_not_found`, `gid_ability_not_selected`, `query_<gid>_no_reply`, or `query_<gid>_reply_timeout`.
4. `parsed_references.json` and `parse_errors.json`: use these when input rows, short links, or keyword search results did not become valid GIDs.
5. `partial_results.json`: use this if failure happens after Mogong starts or during video download; it preserves the latest per-GID query/download state.

If no output directory exists, the failure happened before the `run` command created `--output-dir`, usually argument parsing, an invalid command, or inability to start Python. In that case, use the terminal stderr output.

## Behavior

The script classifies Mogong replies as:

- `matched`: reply includes positive evidence such as saved inspiration, details, video link, or download video wording.
- `not_found`: reply includes negative evidence such as not found, no result, unavailable, unauthorized, or abnormal status wording.
- `mogong_internal_error`: reply includes internal/system/service error wording.
- `no_reply`: Mogong returns no final reply or stays processing until timeout.
- `unchecked`: `--skip-mogong` was used.

Short links and GIDs are resolved by the Douyin Toolkit. Keyword search and delegated download require Wanbang credentials. Mogong querying requires Playwright and may require `ddddocr` when the login page shows an image captcha.

## Integration Notes

When asked to add this functionality to another codebase, keep the Douyin reference contract and Mogong business results separate. Do not copy Toolkit parsing or download code back into the Mogong implementation.

Keep this Skill as the source of truth only for:

- Mogong navigation and reply classification.
- Excel result shape.
- Mogong recovery logs and business statuses.

Keep `douyin-video-toolkit` as the source of truth for URL/GID normalization, Wanbang request parsing, and video downloads.
