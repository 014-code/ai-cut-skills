# Excel And Song Matching Contract

## Song Library Loading

`load_song_records` reads all sheets and searches the first 50 rows for a header row. It accepts flexible aliases:

- Song name: `歌名`, `歌曲名`, `曲名`, `歌曲名称`, plus link-like columns when needed.
- Song ID: `标签ID`, `歌曲ID`, `ID`, `id`, `song_id`, `gq`, `gd`, or any header containing `id` as fallback.
- Link: `链接`, `歌名&链接`, `歌曲链接`, `song_link`, `url`.
- Artist: `歌手`, `歌手名`, `艺人`, `艺人名`, `演唱`, `演唱者`, `artist`, `singer`, `author`.
- Blocked: `禁投`, `是否禁投`, `备注`, `状态`, `是否制作`.

Song IDs are normalized by `normalize_song_id` to `gq_<digits>` for numeric, `gd_...`, or `gq_...` inputs. If the ID column is blank, the loader tries to extract a URL, follows redirects, and reads `track_id` or `trackId` from the final URL.

Rows missing song name or song ID after link resolution are skipped. Duplicate song names with the same normalized `gq_id`/song ID keep one usable record. Duplicate song names with different song IDs are removed from usable records. When `duplicate_output_path` is provided, ambiguous duplicates relevant to the current batch can be exported to `duplicate_songs.xlsx`.

For song-library cells, `《歌名》@汽水音乐 ...` link text is parsed as the outer title, including nested title marks inside the real song name. If the plain song-name cell has unbalanced brackets, the link-title text is used as a fallback. Embedded book-title markers inside a real song name, such as `无影（《青丘奇缘》主题曲）`, are preserved as part of the song name instead of being reduced to `青丘奇缘`.

## Song Matching

`match_song_record` uses exact matching after text normalization and material-name extraction. Text normalization handles full-width/half-width Latin letters and digits, whitespace, Chinese/English parentheses, book-title marks, dash variants, middle-dot variants, smart quotes, and zero-width characters. A single exact match returns the record. Duplicate rows with the same normalized song ID are treated as one usable record. Multiple exact matches with different song IDs return no record and a candidates list so a human can resolve ambiguity.

The filename extractor also strips common leading batch prefixes such as `闭环音乐-10-1-` before matching, so folders that encode a group name plus batch index still resolve to the real song title like `Joker`. Separator variants such as spaces around dashes, long dashes, underscores, full-width digits/letters, and zero-width characters in filenames are normalized before extraction. Chinese batch markers such as `第10批第1条`, `第10批-第1条`, `批次10-序号1`, and bracketed markers like `【10】【1】` are also stripped when they appear before the real song title.

Trailing file-processing suffixes are stripped only for explicit non-song markers such as `(1)`, `副本`, `成片`, `最终版`, `已剪`, `剪辑版`, `修正版`, `导出`, and `无水印`. Real version text such as `Live版` and `伴奏版` is preserved.

Planner behavior:

- VIP/SVIP items skip song matching and generate tags without a song ID.
- Missing or duplicate song IDs do not block upload. The item message explains that the song ID custom tag was not filled.
- A song record marked `禁投` marks the item `skipped`.
- Planning now records `song_match_message` for every item and prints one line per video: matched song ID, unmatched song ID, duplicate candidates, blocked song, or VIP/SVIP skip.

## Backfill Workbook Reading

Backfill aliases include:

- Order ID: `订单id`, `订单ID`, `订单 Id`, `order_id`, `orderId`, `订单号`.
- Material type: `素材类型`, `类型`, `功能卖点`, `分类标签`.
- Song name: `歌名`, `歌曲名`, `曲名`, `歌曲名称`.
- CID: `CID`, `cid`, `对象ID`, `对象id`, `creative_unit_id`.
- Backfill song ID: `标签ID`, `歌曲ID`, `歌曲 ID`, `song_id`, `gq`, `gd`.

`write_back_results(order_excel, output_path, items, include_ready=True)` loads the workbook, selects the sheet most likely to be the backfill template, and prepares headers.

## Backfill Writing

Current write behavior:

- If there is no existing song-name column and a CID column exists, insert `歌曲名称` immediately after `CID`.
- If the sheet is blank, create minimal headers: `素材类型`, `时间`, `CID`, `类型`.
- If any written non-VIP/SVIP item lacks `song_id`, ensure a `备注`/message column and append `未填写歌曲id自定义标签`.
- With `include_ready=True`, write `success` and `ready` items. With `include_ready=False`, write only `success`.
- Start from the first row whose CID cell is empty; never overwrite a row with an existing CID.
- Write CID, material type, type=`剪辑`, blank time, order ID, original video file name into the song-name column, song ID, file name, status, message, classification path, custom tags, and optional tags when matching columns exist.

Dry-run writes to `<task_root>/result.xlsx`. Live upload writes successful items directly back to the original backfill Excel, one successful order at a time.

## Style Repair And Locks

The loader retries with repaired workbook bytes when openpyxl cannot parse bad `.xlsx`/`.xlsm` styles. It rewrites minimal `styles.xml` in memory and strips worksheet style dependencies.

If save fails even after parsing, first check whether Excel/WPS has the workbook open or locked. The in-process lock only serializes runner threads; it cannot unlock files held by other processes.
