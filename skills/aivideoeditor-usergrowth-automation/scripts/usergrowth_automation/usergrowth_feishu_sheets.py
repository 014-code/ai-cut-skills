from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import re
import time
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .usergrowth_tomato_music import (
    TomatoMusicTagBatch,
    normalise_bid,
    normalise_cids,
    tag_for_bid,
)


DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
MAX_READ_COLUMNS = 100


@dataclass
class FeishuDocumentRef:
    original: str
    token: str
    kind: str
    url_sheet_id: str = ""


@dataclass
class FeishuSheetInfo:
    sheet_id: str
    title: str
    index: int = 0
    row_count: int = 0
    column_count: int = 0


@dataclass
class FeishuValueRange:
    range: str
    values: list[list[object]]
    sheet_id: str = ""
    sheet_title: str = ""
    reason: str = ""


@dataclass
class FeishuTomatoSyncResult:
    batches: list[TomatoMusicTagBatch]
    source_url: str
    library_url: str
    source_spreadsheet_token: str
    library_spreadsheet_token: str
    source_sheets: list[str] = field(default_factory=list)
    library_sheets: list[str] = field(default_factory=list)
    write_ranges: list[FeishuValueRange] = field(default_factory=list)
    matched_rows: int = 0
    existing_bid_rows: int = 0
    unmatched_rows: list[dict] = field(default_factory=list)
    library_conflicts: list[dict] = field(default_factory=list)
    existing_bid_conflicts: list[dict] = field(default_factory=list)
    skipped_sheets: list[dict] = field(default_factory=list)
    writeback_performed: bool = False
    verified_ranges: int = 0

    def metadata(self) -> dict:
        return {
            "source_url": self.source_url,
            "library_url": self.library_url,
            "source_spreadsheet_token": self.source_spreadsheet_token,
            "library_spreadsheet_token": self.library_spreadsheet_token,
            "source_sheets": self.source_sheets,
            "library_sheets": self.library_sheets,
            "summary": {
                "matched_rows": self.matched_rows,
                "existing_bid_rows": self.existing_bid_rows,
                "unmatched_rows": len(self.unmatched_rows),
                "library_conflicts": len(self.library_conflicts),
                "existing_bid_conflicts": len(self.existing_bid_conflicts),
                "write_ranges": len(self.write_ranges),
                "writeback_performed": self.writeback_performed,
                "verified_ranges": self.verified_ranges,
            },
            "unmatched_rows": self.unmatched_rows,
            "library_conflicts": self.library_conflicts,
            "existing_bid_conflicts": self.existing_bid_conflicts,
            "skipped_sheets": self.skipped_sheets,
            "planned_writes": [asdict(item) for item in self.write_ranges],
        }


class FeishuApiError(RuntimeError):
    pass


class FeishuSheetsClient:
    """Small urllib-based client for the official Feishu Wiki and Sheets APIs."""

    def __init__(
            self,
            *,
            access_token: str = "",
            app_id: str = "",
            app_secret: str = "",
            base_url: str = DEFAULT_FEISHU_BASE_URL,
            timeout_seconds: float = 30.0,
            max_attempts: int = 4,
    ) -> None:
        self._access_token = str(access_token or "").strip()
        self._app_id = str(app_id or "").strip()
        self._app_secret = str(app_secret or "").strip()
        self.base_url = str(base_url or DEFAULT_FEISHU_BASE_URL).rstrip("/")
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.max_attempts = max(1, int(max_attempts))

    def access_token(self) -> str:
        if self._access_token:
            return self._access_token
        if not self._app_id or not self._app_secret:
            raise FeishuApiError(
                "飞书 API 需要 FEISHU_ACCESS_TOKEN，或同时提供 FEISHU_APP_ID 和 FEISHU_APP_SECRET。"
            )
        payload = self._request_json(
            "POST",
            "/auth/v3/tenant_access_token/internal",
            json_body={"app_id": self._app_id, "app_secret": self._app_secret},
            authenticated=False,
        )
        token = str(payload.get("tenant_access_token") or "").strip()
        if not token:
            raise FeishuApiError("飞书未返回 tenant_access_token。")
        self._access_token = token
        return token

    def resolve_spreadsheet_token(self, reference: str) -> tuple[str, FeishuDocumentRef]:
        parsed = parse_feishu_document_ref(reference)
        if parsed.kind == "sheet":
            return parsed.token, parsed
        if parsed.kind != "wiki":
            raise FeishuApiError(f"不支持的飞书文档地址：{reference}")
        payload = self._request_json(
            "GET",
            "/wiki/v2/spaces/get_node",
            params={"token": parsed.token},
        )
        node = ((payload.get("data") or {}).get("node") or {})
        obj_type = str(node.get("obj_type") or "").lower()
        spreadsheet_token = str(node.get("obj_token") or "").strip()
        if obj_type != "sheet" or not spreadsheet_token:
            raise FeishuApiError(
                f"Wiki 节点不是电子表格或未返回 obj_token：obj_type={obj_type or 'unknown'}"
            )
        return spreadsheet_token, parsed

    def list_sheets(self, spreadsheet_token: str) -> list[FeishuSheetInfo]:
        payload = self._request_json(
            "GET",
            f"/sheets/v3/spreadsheets/{quote(spreadsheet_token, safe='')}/sheets/query",
        )
        rows = ((payload.get("data") or {}).get("sheets") or [])
        result: list[FeishuSheetInfo] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            properties = row.get("grid_properties") or row.get("gridProperties") or {}
            sheet_id = str(row.get("sheet_id") or row.get("sheetId") or "").strip()
            if not sheet_id:
                continue
            result.append(
                FeishuSheetInfo(
                    sheet_id=sheet_id,
                    title=str(row.get("title") or sheet_id),
                    index=int(row.get("index") or 0),
                    row_count=int(properties.get("row_count") or properties.get("rowCount") or 0),
                    column_count=int(properties.get("column_count") or properties.get("columnCount") or 0),
                )
            )
        return sorted(result, key=lambda item: (item.index, item.title))

    def read_range(self, spreadsheet_token: str, range_ref: str) -> list[list[object]]:
        encoded_range = quote(range_ref, safe="!:$")
        payload = self._request_json(
            "GET",
            f"/sheets/v2/spreadsheets/{quote(spreadsheet_token, safe='')}/values/{encoded_range}",
            params={
                "valueRenderOption": "FormattedValue",
                "dateTimeRenderOption": "FormattedString",
            },
        )
        value_range = ((payload.get("data") or {}).get("valueRange") or {})
        values = value_range.get("values") or []
        return [list(row) if isinstance(row, list) else [] for row in values]

    def ensure_column_capacity(
            self,
            spreadsheet_token: str,
            sheet: FeishuSheetInfo,
            required_column_count: int,
    ) -> None:
        current = max(0, int(sheet.column_count))
        required = max(0, int(required_column_count))
        if not current or required <= current:
            return
        self._request_json(
            "POST",
            f"/sheets/v2/spreadsheets/{quote(spreadsheet_token, safe='')}/insert_dimension_range",
            json_body={
                "dimension": {
                    "sheetId": sheet.sheet_id,
                    "majorDimension": "COLUMNS",
                    "startIndex": current,
                    "endIndex": required,
                },
                "inheritStyle": "BEFORE",
            },
        )
        sheet.column_count = required

    def write_ranges(
            self,
            spreadsheet_token: str,
            ranges: list[FeishuValueRange],
            *,
            verify: bool = True,
    ) -> int:
        verified = 0
        for group in _group_write_ranges(ranges, row_limit=5000):
            self._request_json(
                "POST",
                f"/sheets/v2/spreadsheets/{quote(spreadsheet_token, safe='')}/values_batch_update",
                json_body={
                    "valueRanges": [
                        {"range": item.range, "values": item.values}
                        for item in group
                    ]
                },
            )
            if not verify:
                continue
            for item in group:
                actual = self.read_range(spreadsheet_token, item.range)
                if not _matrix_contains_expected(actual, item.values):
                    raise FeishuApiError(f"飞书写入后校验失败：{item.range}")
                verified += 1
        return verified

    def _request_json(
            self,
            method: str,
            path: str,
            *,
            params: dict | None = None,
            json_body: dict | None = None,
            authenticated: bool = True,
    ) -> dict:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.access_token()}"
        url = f"{self.base_url}/{str(path).lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"
        encoded_body = json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else None
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            status_code = 0
            response_headers = {}
            reason = ""
            try:
                request = Request(
                    url,
                    data=encoded_body,
                    headers=headers,
                    method=method.upper(),
                )
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    status_code = int(getattr(response, "status", 200) or 200)
                    response_headers = dict(response.headers.items())
                    reason = str(getattr(response, "reason", "") or "")
                    response_body = response.read()
            except HTTPError as exc:
                status_code = int(exc.code or 0)
                response_headers = dict(exc.headers.items()) if exc.headers else {}
                reason = str(exc.reason or "")
                response_body = exc.read()
            except (URLError, OSError) as exc:
                last_error = f"网络错误：{type(exc).__name__}: {exc}"
                if attempt >= self.max_attempts:
                    break
                time.sleep(min(2 ** (attempt - 1), 8))
                continue

            try:
                payload = json.loads(response_body.decode("utf-8")) if response_body else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            code = payload.get("code") if isinstance(payload, dict) else None
            if 200 <= status_code < 300 and (code in (None, 0)):
                return payload if isinstance(payload, dict) else {}
            message = ""
            if isinstance(payload, dict):
                message = str(payload.get("msg") or payload.get("message") or "")
            last_error = f"HTTP {status_code}, code={code}, msg={message or reason}"
            retryable = status_code == 429 or status_code >= 500
            if not retryable or attempt >= self.max_attempts:
                break
            retry_after = response_headers.get("Retry-After")
            try:
                wait_seconds = float(retry_after) if retry_after else min(2 ** (attempt - 1), 8)
            except ValueError:
                wait_seconds = min(2 ** (attempt - 1), 8)
            time.sleep(max(0.2, wait_seconds))
        raise FeishuApiError(f"飞书 API 请求失败：{method.upper()} {path}；{last_error}")


def parse_feishu_document_ref(reference: str) -> FeishuDocumentRef:
    text = str(reference or "").strip()
    if not text:
        raise FeishuApiError("飞书文档地址不能为空。")
    parsed = urlsplit(text)
    if parsed.scheme and parsed.netloc:
        segments = [item for item in parsed.path.split("/") if item]
        query = parse_qs(parsed.query)
        url_sheet_id = str((query.get("sheet") or [""])[0]).strip()
        for index, segment in enumerate(segments[:-1]):
            if segment in {"wiki", "sheets"}:
                kind = "wiki" if segment == "wiki" else "sheet"
                return FeishuDocumentRef(
                    original=text,
                    token=segments[index + 1],
                    kind=kind,
                    url_sheet_id=url_sheet_id,
                )
        raise FeishuApiError(f"无法从地址解析 Wiki 或电子表格 token：{text}")
    return FeishuDocumentRef(original=text, token=text, kind="sheet")


def sync_tomato_music_from_feishu(
        client: FeishuSheetsClient,
        *,
        source_url: str,
        library_url: str,
        source_sheet_filters: Iterable[str] = (),
        library_sheet_filters: Iterable[str] = (),
        writeback: bool = False,
        overwrite_existing_bid: bool = False,
        verify_writeback: bool = True,
) -> FeishuTomatoSyncResult:
    source_token, _ = client.resolve_spreadsheet_token(source_url)
    library_token, _ = client.resolve_spreadsheet_token(library_url)
    source_sheets = _filter_sheets(client.list_sheets(source_token), source_sheet_filters)
    library_sheets = _filter_sheets(client.list_sheets(library_token), library_sheet_filters)
    if not source_sheets:
        raise FeishuApiError("源飞书电子表格中没有可读取的工作表。")
    if not library_sheets:
        raise FeishuApiError("BID 库飞书电子表格中没有可读取的工作表。")

    lookup, lookup_names, library_conflicts, library_skipped = _load_bid_library(
        client,
        library_token,
        library_sheets,
    )
    result = FeishuTomatoSyncResult(
        batches=[],
        source_url=source_url,
        library_url=library_url,
        source_spreadsheet_token=source_token,
        library_spreadsheet_token=library_token,
        source_sheets=[item.title for item in source_sheets],
        library_sheets=[item.title for item in library_sheets],
        library_conflicts=library_conflicts,
        skipped_sheets=library_skipped,
    )
    grouped: dict[str, dict[str, object]] = {}
    required_columns: dict[str, int] = {}
    for sheet in source_sheets:
        rows = client.read_range(source_token, f"{sheet.sheet_id}!A:{_column_name(MAX_READ_COLUMNS - 1)}")
        header = _find_source_header(rows)
        if header is None:
            result.skipped_sheets.append({"role": "source", "sheet": sheet.title, "reason": "未找到歌名列"})
            continue
        header_row_index, song_index, cid_index, bid_index = header
        target_bid_index = bid_index
        bid_missing = target_bid_index is None
        if target_bid_index is None:
            target_bid_index = max(
                (_last_nonempty_index(row) for row in rows),
                default=-1,
            ) + 1
        if target_bid_index >= MAX_READ_COLUMNS:
            raise FeishuApiError(f"工作表 {sheet.title} 的 BID 目标列超过 API 单次读取的 100 列限制。")
        required_columns[sheet.sheet_id] = max(
            required_columns.get(sheet.sheet_id, 0),
            target_bid_index + 1,
        )
        last_row_index = _last_nonempty_row(rows)
        if last_row_index <= header_row_index:
            continue
        column_values: list[str] = []
        changed_rows: dict[int, str] = {}
        for row_index in range(header_row_index + 1, last_row_index + 1):
            row = rows[row_index] if row_index < len(rows) else []
            song = _cell_text(row[song_index] if song_index < len(row) else "")
            song_key = _normalise_song(song)
            existing_bid = normalise_bid(
                _cell_text(row[target_bid_index] if target_bid_index < len(row) else "")
            )
            resolved_bid = existing_bid
            library_bid = lookup.get(song_key, "") if song_key else ""
            if existing_bid:
                result.existing_bid_rows += 1
                if library_bid and existing_bid != library_bid:
                    result.existing_bid_conflicts.append(
                        {
                            "sheet": sheet.title,
                            "row": row_index + 1,
                            "song": song,
                            "existing_bid": existing_bid,
                            "library_bid": library_bid,
                        }
                    )
                    if overwrite_existing_bid:
                        resolved_bid = library_bid
            elif library_bid:
                resolved_bid = library_bid
                result.matched_rows += 1
            elif song_key:
                result.unmatched_rows.append(
                    {"sheet": sheet.title, "row": row_index + 1, "song": song}
                )

            column_values.append(resolved_bid)
            current_value = existing_bid
            if resolved_bid and resolved_bid != current_value:
                changed_rows[row_index + 1] = resolved_bid

            if not resolved_bid or cid_index is None:
                continue
            cid_value = row[cid_index] if cid_index < len(row) else ""
            cids = normalise_cids([cid_value])
            if not cids:
                continue
            item = grouped.setdefault(resolved_bid, {"cids": [], "songs": []})
            for cid in cids:
                if cid not in item["cids"]:
                    item["cids"].append(cid)
            canonical_song = lookup_names.get(song_key) or song
            if canonical_song and canonical_song not in item["songs"]:
                item["songs"].append(canonical_song)

        column = _column_name(target_bid_index)
        if bid_missing:
            values = [["bid"]] + [[value] for value in column_values]
            result.write_ranges.append(
                FeishuValueRange(
                    range=f"{sheet.sheet_id}!{column}{header_row_index + 1}:{column}{last_row_index + 1}",
                    values=values,
                    sheet_id=sheet.sheet_id,
                    sheet_title=sheet.title,
                    reason="新增 BID 列并回填",
                )
            )
        else:
            result.write_ranges.extend(
                _changed_row_ranges(
                    sheet,
                    column,
                    changed_rows,
                    reason="回填空白 BID" if not overwrite_existing_bid else "回填或覆盖 BID",
                )
            )

    result.batches = [
        TomatoMusicTagBatch(
            bid=bid,
            tag=tag_for_bid(bid),
            cids=list(values["cids"]),
            song_names=list(values["songs"]),
        )
        for bid, values in grouped.items()
        if values["cids"]
    ]
    if not result.batches:
        raise FeishuApiError("飞书源表中没有形成包含有效 BID 和 CID 的打标批次。")
    if writeback and result.write_ranges:
        source_sheet_map = {item.sheet_id: item for item in source_sheets}
        for sheet_id, required_column_count in required_columns.items():
            sheet = source_sheet_map.get(sheet_id)
            if sheet is not None:
                client.ensure_column_capacity(source_token, sheet, required_column_count)
        result.verified_ranges = client.write_ranges(
            source_token,
            result.write_ranges,
            verify=verify_writeback,
        )
        result.writeback_performed = True
    return result


def _load_bid_library(
        client: FeishuSheetsClient,
        spreadsheet_token: str,
        sheets: list[FeishuSheetInfo],
) -> tuple[dict[str, str], dict[str, str], list[dict], list[dict]]:
    candidates: dict[str, list[tuple[str, str, str, int]]] = {}
    skipped: list[dict] = []
    for sheet in sheets:
        rows = client.read_range(spreadsheet_token, f"{sheet.sheet_id}!A:{_column_name(MAX_READ_COLUMNS - 1)}")
        header = _find_library_header(rows)
        if header is None:
            skipped.append({"role": "library", "sheet": sheet.title, "reason": "未同时找到歌名和 bookid/bid 列"})
            continue
        header_row_index, song_index, bid_index = header
        for row_index in range(header_row_index + 1, len(rows)):
            row = rows[row_index]
            song = _cell_text(row[song_index] if song_index < len(row) else "")
            bid = normalise_bid(_cell_text(row[bid_index] if bid_index < len(row) else ""))
            song_key = _normalise_song(song)
            if not song_key or not bid:
                continue
            candidate = (bid, song, sheet.title, row_index + 1)
            if candidate not in candidates.setdefault(song_key, []):
                candidates[song_key].append(candidate)

    lookup: dict[str, str] = {}
    names: dict[str, str] = {}
    conflicts: list[dict] = []
    for song_key, rows in candidates.items():
        bids = []
        for bid, _, _, _ in rows:
            if bid not in bids:
                bids.append(bid)
        if len(bids) != 1:
            conflicts.append(
                {
                    "song": rows[0][1],
                    "bids": bids,
                    "locations": [
                        {"sheet": sheet, "row": row, "bid": bid}
                        for bid, _, sheet, row in rows
                    ],
                }
            )
            continue
        lookup[song_key] = bids[0]
        names[song_key] = rows[0][1]
    return lookup, names, conflicts, skipped


def _find_library_header(rows: list[list[object]]) -> tuple[int, int, int] | None:
    song_headers = {"歌名", "歌曲名", "song", "songname"}
    bid_headers = {"bid", "bookid", "书籍id", "小说id"}
    for row_index, row in enumerate(rows[:30]):
        compact = [_compact_header(value) for value in row]
        song_index = next((index for index, value in enumerate(compact) if value in song_headers), None)
        bid_index = next((index for index, value in enumerate(compact) if value in bid_headers), None)
        if song_index is not None and bid_index is not None:
            return row_index, song_index, bid_index
    return None


def _find_source_header(
        rows: list[list[object]],
) -> tuple[int, int, int | None, int | None] | None:
    song_headers = {"歌名", "歌曲名", "song", "songname"}
    cid_headers = {"cid", "素材cid", "creativeid", "素材id"}
    bid_headers = {"bid", "bookid", "书籍id", "小说id"}
    for row_index, row in enumerate(rows[:30]):
        compact = [_compact_header(value) for value in row]
        song_index = next((index for index, value in enumerate(compact) if value in song_headers), None)
        if song_index is None:
            continue
        cid_index = next((index for index, value in enumerate(compact) if value in cid_headers), None)
        bid_index = next((index for index, value in enumerate(compact) if value in bid_headers), None)
        return row_index, song_index, cid_index, bid_index
    return None


def _filter_sheets(
        sheets: list[FeishuSheetInfo],
        filters: Iterable[str],
) -> list[FeishuSheetInfo]:
    wanted = {str(value or "").strip().casefold() for value in filters if str(value or "").strip()}
    if not wanted:
        return sheets
    return [
        sheet for sheet in sheets
        if sheet.sheet_id.casefold() in wanted or sheet.title.casefold() in wanted
    ]


def _changed_row_ranges(
        sheet: FeishuSheetInfo,
        column: str,
        changed_rows: dict[int, str],
        *,
        reason: str,
) -> list[FeishuValueRange]:
    if not changed_rows:
        return []
    ordered = sorted(changed_rows)
    groups: list[list[int]] = []
    current: list[int] = []
    for row in ordered:
        if current and row != current[-1] + 1:
            groups.append(current)
            current = []
        current.append(row)
    if current:
        groups.append(current)
    return [
        FeishuValueRange(
            range=f"{sheet.sheet_id}!{column}{rows[0]}:{column}{rows[-1]}",
            values=[[changed_rows[row]] for row in rows],
            sheet_id=sheet.sheet_id,
            sheet_title=sheet.title,
            reason=reason,
        )
        for rows in groups
    ]


def _group_write_ranges(
        ranges: list[FeishuValueRange],
        *,
        row_limit: int,
) -> list[list[FeishuValueRange]]:
    result: list[list[FeishuValueRange]] = []
    current: list[FeishuValueRange] = []
    current_rows = 0
    for item in ranges:
        rows = max(1, len(item.values))
        if current and current_rows + rows > row_limit:
            result.append(current)
            current = []
            current_rows = 0
        current.append(item)
        current_rows += rows
    if current:
        result.append(current)
    return result


def _compact_header(value: object) -> str:
    return re.sub(r"[\s_\-（）()【】\[\]]+", "", _cell_text(value).lower())


def _normalise_song(value: object) -> str:
    return re.sub(r"\s+", "", _cell_text(value)).casefold()


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _last_nonempty_index(row: list[object]) -> int:
    for index in range(len(row) - 1, -1, -1):
        if _cell_text(row[index]):
            return index
    return -1


def _last_nonempty_row(rows: list[list[object]]) -> int:
    for index in range(len(rows) - 1, -1, -1):
        if any(_cell_text(value) for value in rows[index]):
            return index
    return -1


def _column_name(index: int) -> str:
    value = int(index) + 1
    if value <= 0:
        raise ValueError("Column index must be non-negative.")
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _normalise_matrix(values: list[list[object]]) -> list[list[str]]:
    return [[_cell_text(value) for value in row] for row in values]


def _matrix_contains_expected(
        actual_values: list[list[object]],
        expected_values: list[list[object]],
) -> bool:
    actual = _normalise_matrix(actual_values)
    expected = _normalise_matrix(expected_values)
    for row_index, expected_row in enumerate(expected):
        actual_row = actual[row_index] if row_index < len(actual) else []
        for column_index, expected_value in enumerate(expected_row):
            actual_value = actual_row[column_index] if column_index < len(actual_row) else ""
            if actual_value != expected_value:
                return False
    return True
