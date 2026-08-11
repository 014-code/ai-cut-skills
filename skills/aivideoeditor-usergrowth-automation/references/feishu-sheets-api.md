# Feishu Sheets API For Tomato Music

Use the official Feishu Wiki and Sheets OpenAPI to read song names, resolve `bookid`/`BID` values across worksheets, and optionally write BID values back to the source spreadsheet. Do not use browser clicks, clipboard paste, or grid-coordinate automation for Feishu spreadsheet data.

Keep Playwright for the separate UserGrowth/墨攻 step that searches CIDs and adds `bid_<BID>` custom tags.

## Authentication And Permissions

Provide one of these authentication modes through environment variables:

```powershell
$env:FEISHU_ACCESS_TOKEN = '<user-or-tenant-access-token>'
```

or:

```powershell
$env:FEISHU_APP_ID = '<app-id>'
$env:FEISHU_APP_SECRET = '<app-secret>'
```

`FEISHU_ACCESS_TOKEN`, `FEISHU_TENANT_ACCESS_TOKEN`, and `FEISHU_USER_ACCESS_TOKEN` are accepted. When `FEISHU_APP_ID` and `FEISHU_APP_SECRET` are supplied, the script requests a tenant token from `/auth/v3/tenant_access_token/internal`.

The app or user represented by the token must be able to access both Wiki nodes and must have spreadsheet read permission. BID writeback additionally requires spreadsheet edit permission. Do not persist or print tokens and secrets.

## API Boundary

The implementation uses these official endpoints:

- `GET /wiki/v2/spaces/get_node?token=...` to resolve a Wiki token to a spreadsheet `obj_token`.
- `GET /sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query` to list worksheets.
- `GET /sheets/v2/spreadsheets/{spreadsheet_token}/values/{range}` to read cells.
- `POST /sheets/v2/spreadsheets/{spreadsheet_token}/insert_dimension_range` to add column capacity when a new BID column falls outside the current grid.
- `POST /sheets/v2/spreadsheets/{spreadsheet_token}/values_batch_update` to write BID values.

The client uses Python's standard-library `urllib`; no `requests` dependency is required.

## Matching Contract

- Read every worksheet by default, including when the URL contains a single `?sheet=...` tab. This preserves cross-sheet lookup and source processing.
- Restrict worksheets only when repeated `--feishu-source-sheet` or `--feishu-library-sheet` arguments are explicitly supplied. Each value may be a worksheet ID or exact title.
- Detect source headers from `歌名`/`歌曲名`, optional `CID`, and optional `BID`/`bookid` aliases.
- Detect library headers only when both a song-name column and a `bookid`/`BID` column exist.
- Normalize song names by removing whitespace and comparing case-insensitively.
- Reuse the same resolved BID for repeated source song names.
- Accept duplicate library rows only when they resolve to the same BID. If one song maps to multiple BIDs, report a library conflict and do not guess.
- Preserve a non-empty source BID by default. Report a conflict when it differs from the library. Only overwrite it with `--feishu-overwrite-existing-bid`.
- Add a lowercase `bid` header when the source worksheet has no BID column. Place it after the rightmost populated source column so existing data is not overwritten.
- Form 墨攻 batches only from rows containing both a resolved BID and a valid CID.

## Read-Only Preflight

Run without Feishu writeback flags and without UserGrowth live flags:

```powershell
$env:FEISHU_ACCESS_TOKEN = '<token>'

python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --feishu-source-url 'https://donsontech.feishu.cn/wiki/W5jwwHoxei11cjkWzxZciWJWnCh?sheet=snQZ1w' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/GuUUwCVeoiJLS8k2kTxcBS9Cngh?sheet=be5f4d' `
  --customer-id 3681575 `
  --output-root 'D:\Users\Donson\Downloads\番茄音乐飞书预检输出'
```

This reads both online spreadsheets but changes neither Feishu nor UserGrowth. Inspect `task.json`:

- `feishu.summary.matched_rows`
- `feishu.unmatched_rows`
- `feishu.library_conflicts`
- `feishu.existing_bid_conflicts`
- `feishu.planned_writes`
- the top-level BID/CID batch and chunk counts

## BID Writeback

After the read-only result is reviewed, explicitly enable and confirm Feishu writeback:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --feishu-source-url 'https://donsontech.feishu.cn/wiki/W5jwwHoxei11cjkWzxZciWJWnCh?sheet=snQZ1w' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/GuUUwCVeoiJLS8k2kTxcBS9Cngh?sheet=be5f4d' `
  --customer-id 3681575 `
  --feishu-writeback `
  --confirm-feishu-writeback `
  --output-root 'D:\Users\Donson\Downloads\番茄音乐飞书回填输出'
```

The script rereads each written range and fails if the returned values do not match. Feishu writeback is independent of UserGrowth live tagging: omitting `--live --confirm-live` still performs only the requested Feishu writeback and emits a UserGrowth dry-run plan.

Use `--feishu-overwrite-existing-bid` only after conflicts have been reviewed and the user explicitly wants the source BID replaced.

## Full Feishu-To-Mogong Run

To read and optionally write Feishu, then run CID tagging in 墨攻, add UserGrowth credentials and the separate live confirmation flags:

```powershell
$env:USERGROWTH_ACCOUNT = '<account>'
$env:USERGROWTH_PASSWORD = '<password>'

python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --feishu-source-url 'https://donsontech.feishu.cn/wiki/W5jwwHoxei11cjkWzxZciWJWnCh?sheet=snQZ1w' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/GuUUwCVeoiJLS8k2kTxcBS9Cngh?sheet=be5f4d' `
  --customer-id 3681575 `
  --live `
  --confirm-live `
  --output-root 'D:\Users\Donson\Downloads\番茄音乐飞书打标输出'
```

Add `--feishu-writeback --confirm-feishu-writeback` only when the same run is also authorized to modify the source spreadsheet.

## Failure Handling

- A `99991663`-style permission error means the token's app/user lacks access or the required scope; fix sharing and app permissions instead of falling back to browser automation.
- If a Wiki node is not a spreadsheet, verify the URL and the node's `obj_type`.
- If no worksheets are selected, verify repeated sheet filters against the worksheet ID or exact title.
- If no batch is formed, inspect skipped worksheets, header aliases, unresolved songs, library conflicts, and CID validity.
- If writeback verification fails, stop. Inspect the affected range and permissions before retrying; do not assume a successful HTTP response means the cells were updated.
