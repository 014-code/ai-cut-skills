# Feishu Sheets API For Tomato Music

Use the official Feishu Wiki and Sheets OpenAPI to read song names and artists, resolve `bookid`/`BID` values across worksheets and across one or more审核人员单曲查询表, and optionally write BID values back to the source spreadsheet. Do not use browser clicks, clipboard paste, or grid-coordinate automation for Feishu spreadsheet data.

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

`FEISHU_ACCESS_TOKEN`, `FEISHU_TENANT_ACCESS_TOKEN`, and `FEISHU_USER_ACCESS_TOKEN` are accepted. When `FEISHU_APP_ID` and `FEISHU_APP_SECRET` are supplied without an explicit token, the script can request a tenant token from `/auth/v3/tenant_access_token/internal`. For the user-permission path, pass `--feishu-user-oauth`: the CLI opens the domestic Feishu OAuth consent page, runs a loopback PKCE callback, and exchanges the authorization code for a `user_access_token`. The token is memory-only unless `--feishu-oauth-persist` or the one-time setup shortcut `--feishu-oauth-bootstrap` is explicitly enabled.

## User OAuth mode (recommended when tenant data scope is unavailable)

`user_access_token` is issued only after the user authorizes the app. The domestic authorization page is `https://accounts.feishu.cn/open-apis/authen/v1/authorize`; the authorization-code exchange is sent to `https://open.feishu.cn/open-apis/authen/v2/oauth/token`. It inherits the authorizing user's own document read/write permissions, so no tenant-level document data scope is needed. The app still needs the API permissions for the endpoints it calls, and the authorizing user must be able to open all three spreadsheets.

Before the first run, add the exact loopback URL below under **开发配置 → 安全设置 → 重定向 URL**:

```text
http://127.0.0.1:8765/callback
```

The default OAuth scopes are:

```text
wiki:node:read sheets:spreadsheet:readonly sheets:spreadsheet
```

The read-only scope is needed for preflight; the full spreadsheet scope is needed only when `--feishu-writeback --confirm-feishu-writeback` is explicitly enabled. If the app has a different approved scope name or a stricter policy, override it with `--feishu-oauth-scope`. With `--feishu-oauth-persist` (or `--feishu-oauth-bootstrap`), the CLI automatically appends `offline_access`, stores access/refresh tokens in a Windows CurrentUser DPAPI-encrypted cache, and refreshes them in later processes. Without one of those flags, no token is persisted.

### One-time business-user bootstrap

Use this mode when a business user owns the three Feishu documents but the app cannot be granted tenant document data scope. Set the app credentials and the Feishu login credentials only for the first process, then remove the bootstrap variables:

```powershell
$env:FEISHU_APP_ID = '<app-id>'
$env:FEISHU_APP_SECRET = '<app-secret>'
$env:FEISHU_BOOTSTRAP_ACCOUNT = '<business-feishu-account>'
$env:FEISHU_BOOTSTRAP_PASSWORD = '<business-feishu-password>'

python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --feishu-oauth-bootstrap `
  --feishu-source-url 'https://donsontech.feishu.cn/wiki/W5jwwHoxei11cjkWzxZciWJWnCh' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/GuUUwCVeoiJLS8k2kTxcBS9Cngh?from=from_copylink' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/PSSYwIoBHi7JY5kB6jGcLpWundb?from=from_copylink&sheet=qTG951' `
  --customer-id 3681575

Remove-Item Env:FEISHU_BOOTSTRAP_ACCOUNT, Env:FEISHU_BOOTSTRAP_PASSWORD
```

`--feishu-oauth-bootstrap` implies `--feishu-user-oauth --feishu-oauth-persist`. On a cache miss it launches a temporary visible Playwright browser, fills the account/password, completes the consent callback, and closes the browser. On a cache hit it performs no login and reads only the same-user DPAPI cache. The account/password are never written to the cache or task artifacts. A captcha, SSO, or MFA challenge is surfaced for manual completion/retry; it is not bypassed. The cache is tied to the Windows user profile and machine, so each business Windows account must bootstrap once. “Long-term” means automatic access-token refresh while Feishu's refresh grant remains valid; `--feishu-oauth-reauthorize` is required after revocation or refresh-token expiry.

Run a read-only preflight with OAuth (no sheet or UserGrowth writes):

```powershell
$env:FEISHU_APP_ID = '<app-id>'
$env:FEISHU_APP_SECRET = '<app-secret>'

python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --feishu-user-oauth `
  --feishu-oauth-persist `
  --feishu-source-url 'https://donsontech.feishu.cn/wiki/W5jwwHoxei11cjkWzxZciWJWnCh' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/GuUUwCVeoiJLS8k2kTxcBS9Cngh?from=from_copylink' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/PSSYwIoBHi7JY5kB6jGcLpWundb?from=from_copylink&sheet=qTG951' `
  --customer-id 3681575 `
  --output-root 'D:\Users\Donson\Downloads\番茄音乐飞书 OAuth 预检输出'
```

The browser is used only for the first OAuth consent screen (or the first `--feishu-oauth-bootstrap` setup). Later runs with persistence reuse or refresh the encrypted cache without reopening consent. The default cache is under the current user's `LOCALAPPDATA`; `--feishu-oauth-cache` overrides it, and `--feishu-oauth-reauthorize` deliberately replaces it through a fresh grant. The cache contains no App Secret and can only be decrypted by the same Windows user. Never paste the resulting token into `task.json`, `run.log`, a manifest, or source code.

Use `--feishu-oauth-no-browser` when an automation controller needs to open the printed authorization URL in a specific controlled browser. The URL contains the temporary OAuth state and PKCE challenge, but no App Secret or access token. Do not combine it with `--feishu-oauth-bootstrap` on a cache miss; a cached bootstrap token can still be reused without a browser.

The app or user represented by the token must be able to access both Wiki nodes and must have spreadsheet read permission. BID writeback additionally requires spreadsheet edit permission. Do not print tokens or secrets, and do not persist tokens outside the optional DPAPI-encrypted cache.

## API Boundary

The implementation uses these official endpoints:

- `GET /wiki/v2/spaces/get_node?token=...` to resolve a Wiki token to a spreadsheet `obj_token`.
- `GET /sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query` to list worksheets.
- `GET /sheets/v2/spreadsheets/{spreadsheet_token}/values/{range}` to read cells.
- `POST /sheets/v2/spreadsheets/{spreadsheet_token}/insert_dimension_range` to add column capacity when a new BID column falls outside the current grid.
- `POST /sheets/v2/spreadsheets/{spreadsheet_token}/values_batch_update` to write BID values.

The client uses Python's standard-library `urllib`; no `requests` dependency is required.

## Matching Contract

- Read every worksheet in every supplied library URL by default, including when a URL contains a single `?sheet=...` tab. This preserves cross-sheet and cross-table lookup.
- Repeat `--feishu-library-url` once per审核人员单曲查询表. The old single-library invocation remains valid.
- Restrict worksheets only when repeated `--feishu-source-sheet` or `--feishu-library-sheet` arguments are explicitly supplied. Each value may be a worksheet ID or exact title; library filters apply to every supplied library URL.
- Detect source headers from `歌名`/`歌曲名` and `歌手`/`艺人`/`artist` aliases, plus optional `CID` and `BID`/`bookid` aliases.
- Detect library headers only when song-name, artist, and `bookid`/`BID` columns all exist.
- Match only when both normalized song name and normalized artist are equal. Normalize both by removing whitespace and comparing case-insensitively; never fall back to song-name-only matching.
- Reuse the same resolved BID for repeated source rows with the same song-and-artist pair.
- Treat a missing song or artist as unresolved. Accept duplicate library rows only when the same song-and-artist pair resolves to one BID. If that pair maps to multiple BIDs, report a library conflict and do not guess.
- Preserve a non-empty source BID by default. Report a conflict when it differs from the library. Only overwrite it with `--feishu-overwrite-existing-bid`.
- Add a lowercase `bid` header when the source worksheet has no BID column. Place it after the rightmost populated source column so existing data is not overwritten.
- Form 墨攻 batches only from rows containing both a resolved BID and a valid CID.
- When `--bid` is supplied, apply that filter inside the Feishu sync so BID writeback, browser batches, source-row status tracking, and final `已打标` updates all stay within the same BID. A live/writeback run using `--max-batches` must also supply `--bid`; do not let a browser-only batch limit write unrelated BID rows discovered during the preceding sheet scan.
- If the source has a `打标状态`/`标签状态` column, exclude rows already marked `已打标` before BID grouping and browser launch. Treat blank or `未打标` as pending; skip and report unknown non-empty statuses. In a live Feishu-backed run, update a chunk's matching rows to `已打标` only after its 墨攻 operation task passes the exact total/success/failure gate, then reread the status cells for verification.

## Read-Only Preflight

Run without Feishu writeback flags and without UserGrowth live flags:

```powershell
$env:FEISHU_ACCESS_TOKEN = '<token>'

python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --feishu-source-url 'https://donsontech.feishu.cn/wiki/W5jwwHoxei11cjkWzxZciWJWnCh?sheet=snQZ1w' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/GuUUwCVeoiJLS8k2kTxcBS9Cngh?from=from_copylink' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/PSSYwIoBHi7JY5kB6jGcLpWundb?from=from_copylink&sheet=qTG951' `
  --customer-id 3681575 `
  --output-root 'D:\Users\Donson\Downloads\番茄音乐飞书预检输出'
```

This reads both online spreadsheets but changes neither Feishu nor UserGrowth. Inspect `task.json`:

- `feishu.summary.matched_rows`
- `feishu.unmatched_rows`
- `feishu.library_conflicts`
- `feishu.existing_bid_conflicts`
- `feishu.library_urls`
- `feishu.planned_writes`
- the top-level BID/CID batch and chunk counts

## BID Writeback

After the read-only result is reviewed, explicitly enable and confirm Feishu writeback:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --feishu-source-url 'https://donsontech.feishu.cn/wiki/W5jwwHoxei11cjkWzxZciWJWnCh?sheet=snQZ1w' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/GuUUwCVeoiJLS8k2kTxcBS9Cngh?from=from_copylink' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/PSSYwIoBHi7JY5kB6jGcLpWundb?from=from_copylink&sheet=qTG951' `
  --customer-id 3681575 `
  --feishu-writeback `
  --confirm-feishu-writeback `
  --output-root 'D:\Users\Donson\Downloads\番茄音乐飞书回填输出'
```

The script rereads each written range and fails if the returned values do not match. Feishu writeback is independent of UserGrowth live tagging: omitting `--live --confirm-live` still performs only the requested Feishu writeback and emits a UserGrowth dry-run plan.

When `--live` uses `--feishu-source-url`, pass both Feishu writeback confirmation flags. They authorize the post-success `打标状态=已打标` update; skipped, partial, or failed CID chunks are never marked complete.

Use `--feishu-overwrite-existing-bid` only after conflicts have been reviewed and the user explicitly wants the source BID replaced.

## Full Feishu-To-Mogong Run

To read and optionally write Feishu, then run CID tagging in 墨攻, add UserGrowth credentials and the separate live confirmation flags:

```powershell
$env:USERGROWTH_ACCOUNT = '<account>'
$env:USERGROWTH_PASSWORD = '<password>'

python C:\Users\Donson\.codex\skills\aivideoeditor-usergrowth-automation\scripts\tomato_music_tagging.py `
  --feishu-source-url 'https://donsontech.feishu.cn/wiki/W5jwwHoxei11cjkWzxZciWJWnCh?sheet=snQZ1w' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/GuUUwCVeoiJLS8k2kTxcBS9Cngh?from=from_copylink' `
  --feishu-library-url 'https://donsontech.feishu.cn/wiki/PSSYwIoBHi7JY5kB6jGcLpWundb?from=from_copylink&sheet=qTG951' `
  --customer-id 3681575 `
  --live `
  --confirm-live `
  --output-root 'D:\Users\Donson\Downloads\番茄音乐飞书打标输出'
```

Add `--feishu-writeback --confirm-feishu-writeback` only when the same run is also authorized to modify the source spreadsheet.

## Failure Handling

- A `99991663`-style permission error means the token's app/user lacks access or the required scope; fix sharing and app permissions instead of falling back to browser automation. In OAuth mode, re-authorize after changing scopes so the new consent is reflected in the user token.
- If a Wiki node is not a spreadsheet, verify the URL and the node's `obj_type`.
- If no worksheets are selected, verify repeated sheet filters against the worksheet ID or exact title.
- If no batch is formed, inspect skipped worksheets, header aliases, unresolved songs, library conflicts, and CID validity.
- If writeback verification fails, stop. Inspect the affected range and permissions before retrying; do not assume a successful HTTP response means the cells were updated.
