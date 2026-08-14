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
TAG_STATUS_HEADERS = {"打标状态", "标签状态", "tagstatus", "status"}
TAG_STATUS_DONE = "已打标"
TAG_STATUS_PENDING = {"", "未打标"}


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
class FeishuTomatoSourceRow:
    sheet_id: str
    sheet_title: str
    row: int
    cid: str
    bid: str
    status_column: int | None = None
    status: str = ""


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
    library_urls: list[str] = field(default_factory=list)
    library_spreadsheet_tokens: list[str] = field(default_factory=list)
    source_rows: list[FeishuTomatoSourceRow] = field(default_factory=list)
    pending_status_rows: int = 0
    already_tagged_rows: int = 0

    def metadata(self) -> dict:
        return {
            "source_url": self.source_url,
            "library_url": self.library_url,
            "library_urls": self.library_urls or [self.library_url],
            "source_spreadsheet_token": self.source_spreadsheet_token,
            "library_spreadsheet_token": self.library_spreadsheet_token,
            "library_spreadsheet_tokens": self.library_spreadsheet_tokens or [self.library_spreadsheet_token],
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
                "pending_status_rows": self.pending_status_rows,
                "already_tagged_rows": self.already_tagged_rows,
            },
            "unmatched_rows": self.unmatched_rows,
            "library_conflicts": self.library_conflicts,
            "existing_bid_conflicts": self.existing_bid_conflicts,
            "skipped_sheets": self.skipped_sheets,
            "planned_writes": [asdict(item) for item in self.write_ranges],
        }


def write_tomato_tag_status(
        client: "FeishuSheetsClient",
        result: FeishuTomatoSyncResult,
        cids: Iterable[str],
        *,
        status: str = TAG_STATUS_DONE,
        verify: bool = True,
) -> int:
    wanted = set(normalise_cids(cids))
    if not wanted:
        return 0
    rows = [
        row for row in result.source_rows
        if row.cid in wanted and row.status_column is not None and row.status != status
    ]
    missing = wanted - {row.cid for row in rows} - {
        row.cid for row in result.source_rows if row.cid in wanted and row.status == status
    }
    if missing:
        raise FeishuApiError(f"飞书源表中未找到可更新打标状态的 CID：{','.join(sorted(missing))}")
    ranges = [
        FeishuValueRange(
            range=(
                f"{row.sheet_id}!{_column_name(int(row.status_column))}{row.row}:"
                f"{_column_name(int(row.status_column))}{row.row}"
            ),
            values=[[status]],
            sheet_id=row.sheet_id,
            sheet_title=row.sheet_title,
            reason="墨攻打标成功后更新打标状态",
        )
        for row in rows
    ]
    if ranges:
        client.write_ranges(result.source_spreadsheet_token, ranges, verify=verify)
        for row in rows:
            row.status = status
    return len(rows)


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
            token_kind: str = "",
            base_url: str = DEFAULT_FEISHU_BASE_URL,
            timeout_seconds: float = 30.0,
            max_attempts: int = 4,
    ) -> None:
        self._access_token = str(access_token or "").strip()
        self._app_id = str(app_id or "").strip()
        self._app_secret = str(app_secret or "").strip()
        self._token_kind = str(token_kind or "").strip().lower()
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

    @property
    def token_kind(self) -> str:
        """Return the credential mode without exposing the credential value."""
        if self._token_kind:
            return self._token_kind
        if self._access_token:
            return "access_token"
        if self._app_id and self._app_secret:
            return "tenant_access_token"
        return "none"

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
        library_url: str | None = None,
        library_urls: Iterable[str] = (),
        source_sheet_filters: Iterable[str] = (),
        library_sheet_filters: Iterable[str] = (),
        writeback: bool = False,
        overwrite_existing_bid: bool = False,
        verify_writeback: bool = True,
        bid_filters: Iterable[str] = (),
) -> FeishuTomatoSyncResult:
    wanted_bids = {
        normalized
        for value in bid_filters
        if (normalized := normalise_bid(value))
    }
    source_token, _ = client.resolve_spreadsheet_token(source_url)
    requested_library_urls: list[str] = []
    raw_library_urls = [library_urls] if isinstance(library_urls, str) else list(library_urls)
    for value in ([library_url] if library_url else []) + raw_library_urls:
        normalized = str(value or "").strip()
        if normalized and normalized not in requested_library_urls:
            requested_library_urls.append(normalized)
    if not requested_library_urls:
        raise FeishuApiError("至少需要提供一个 BID 库飞书电子表格地址。")
    source_sheets = _filter_sheets(client.list_sheets(source_token), source_sheet_filters)
    if not source_sheets:
        raise FeishuApiError("源飞书电子表格中没有可读取的工作表。")

    library_tokens: list[str] = []
    library_sheets: list[FeishuSheetInfo] = []
    lookup: dict[str, str] = {}
    lookup_names: dict[str, str] = {}
    conflicted_track_keys: set[str] = set()
    library_conflicts: list[dict] = []
    library_skipped: list[dict] = []
    for library_url_item in requested_library_urls:
        library_token, _ = client.resolve_spreadsheet_token(library_url_item)
        library_tokens.append(library_token)
        selected_sheets = _filter_sheets(client.list_sheets(library_token), library_sheet_filters)
        if not selected_sheets:
            raise FeishuApiError(f"BID 库飞书电子表格中没有可读取的工作表：{library_url_item}")
        library_sheets.extend(selected_sheets)
        (
            current_lookup,
            current_names,
            current_conflicts,
            current_skipped,
        ) = _load_bid_library(client, library_token, selected_sheets)
        library_skipped.extend(current_skipped)
        for conflict in current_conflicts:
            conflict_track_key = _normalise_track(
                conflict.get("song", ""),
                conflict.get("artist", ""),
            )
            if all(conflict_track_key):
                conflicted_track_keys.add(_track_lookup_key(*conflict_track_key))
            library_conflicts.append(conflict)
        for track_key, bid in current_lookup.items():
            if track_key in conflicted_track_keys:
                continue
            existing_bid = lookup.get(track_key)
            if existing_bid and existing_bid != bid:
                locations = [
                    {"source": "library_lookup", "bid": existing_bid},
                    {"source": library_url_item, "bid": bid},
                ]
                song, artist = _split_track_lookup_key(track_key)
                library_conflicts.append(
                    {
                        "song": current_names.get(track_key) or song,
                        "artist": artist,
                        "bids": [existing_bid, bid],
                        "locations": locations,
                    }
                )
                conflicted_track_keys.add(track_key)
                lookup.pop(track_key, None)
                lookup_names.pop(track_key, None)
                continue
            if track_key not in lookup:
                lookup[track_key] = bid
                lookup_names[track_key] = current_names.get(track_key) or track_key
    if not library_sheets:
        raise FeishuApiError("BID 库飞书电子表格中没有可读取的工作表。")

    # 先缓存源表，并按“歌名+歌手”汇总已存在的 BID。审核库暂时缺少
    # 该歌曲时，只复用源表中唯一一致的历史 BID；多个历史 BID 仍视为冲突。
    source_payloads: list[tuple[FeishuSheetInfo, list[list[object]], tuple | None]] = []
    source_bid_candidates: dict[str, list[tuple[str, str, str, str, int]]] = {}
    for sheet in source_sheets:
        rows = client.read_range(source_token, f"{sheet.sheet_id}!A:{_column_name(MAX_READ_COLUMNS - 1)}")
        header = _find_source_header(rows)
        source_payloads.append((sheet, rows, header))
        if header is None:
            continue
        header_row_index, song_index, artist_index, _, bid_index, _ = header
        if bid_index is None:
            continue
        last_row_index = _last_nonempty_row(rows)
        for row_index in range(header_row_index + 1, last_row_index + 1):
            row = rows[row_index] if row_index < len(rows) else []
            song = _cell_text(row[song_index] if song_index < len(row) else "")
            artist = _cell_text(row[artist_index] if artist_index < len(row) else "")
            bid = normalise_bid(_cell_text(row[bid_index] if bid_index < len(row) else ""))
            normalised_track = _normalise_track(song, artist)
            if not bid or not all(normalised_track):
                continue
            track_key = _track_lookup_key(*normalised_track)
            candidate = (bid, song, artist, sheet.title, row_index + 1)
            if candidate not in source_bid_candidates.setdefault(track_key, []):
                source_bid_candidates[track_key].append(candidate)

    source_bid_lookup: dict[str, str] = {}
    source_bid_names: dict[str, str] = {}
    source_conflicted_track_keys: set[str] = set()
    source_bid_conflicts: list[dict] = []
    for track_key, candidates in source_bid_candidates.items():
        bids = list(dict.fromkeys(item[0] for item in candidates))
        if len(bids) != 1:
            source_conflicted_track_keys.add(track_key)
            source_bid_conflicts.append(
                {
                    "song": candidates[0][1],
                    "artist": candidates[0][2],
                    "source_bids": bids,
                    "locations": [
                        {"sheet": sheet_title, "row": row, "bid": bid}
                        for bid, _, _, sheet_title, row in candidates
                    ],
                    "reason": "源表同一歌名和歌手存在多个 BID，禁止自动复用",
                }
            )
            continue
        source_bid_lookup[track_key] = bids[0]
        source_bid_names[track_key] = candidates[0][1]

    result = FeishuTomatoSyncResult(
        batches=[],
        source_url=source_url,
        library_url=requested_library_urls[0],
        source_spreadsheet_token=source_token,
        library_spreadsheet_token=library_tokens[0],
        source_sheets=[item.title for item in source_sheets],
        library_sheets=[item.title for item in library_sheets],
        library_conflicts=library_conflicts,
        existing_bid_conflicts=source_bid_conflicts,
        skipped_sheets=library_skipped,
        library_urls=requested_library_urls,
        library_spreadsheet_tokens=library_tokens,
    )
    grouped: dict[str, dict[str, object]] = {}
    required_columns: dict[str, int] = {}
    for sheet, rows, header in source_payloads:
        if header is None:
            result.skipped_sheets.append({"role": "source", "sheet": sheet.title, "reason": "未同时找到歌名和歌手列"})
            continue
        header_row_index, song_index, artist_index, cid_index, bid_index, status_index = header
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
        target_status_index = status_index
        if target_status_index is None:
            target_status_index = max(target_bid_index, max((_last_nonempty_index(row) for row in rows), default=-1)) + 1
        if target_status_index >= MAX_READ_COLUMNS:
            raise FeishuApiError(f"工作表 {sheet.title} 的打标状态目标列超过 API 单次读取的 100 列限制。")
        required_columns[sheet.sheet_id] = max(
            required_columns.get(sheet.sheet_id, 0),
            target_status_index + 1,
        )
        last_row_index = _last_nonempty_row(rows)
        if last_row_index <= header_row_index:
            continue
        column_values: list[str] = []
        changed_rows: dict[int, str] = {}
        for row_index in range(header_row_index + 1, last_row_index + 1):
            row = rows[row_index] if row_index < len(rows) else []
            song = _cell_text(row[song_index] if song_index < len(row) else "")
            artist = _cell_text(row[artist_index] if artist_index < len(row) else "")
            normalised_track = _normalise_track(song, artist)
            track_key = _track_lookup_key(*normalised_track) if all(normalised_track) else ""
            existing_bid = normalise_bid(
                _cell_text(row[target_bid_index] if target_bid_index < len(row) else "")
            )
            resolved_bid = existing_bid
            library_bid = lookup.get(track_key, "") if track_key else ""
            source_bid = (
                source_bid_lookup.get(track_key, "")
                if track_key
                and track_key not in conflicted_track_keys
                and track_key not in source_conflicted_track_keys
                else ""
            )
            eligible_for_tagging = False
            if existing_bid:
                result.existing_bid_rows += 1
                if library_bid and existing_bid != library_bid:
                    result.existing_bid_conflicts.append(
                        {
                            "sheet": sheet.title,
                            "row": row_index + 1,
                            "song": song,
                            "artist": artist,
                            "existing_bid": existing_bid,
                            "library_bid": library_bid,
                        }
                    )
                    if overwrite_existing_bid:
                        resolved_bid = library_bid
                        eligible_for_tagging = True
                elif library_bid == existing_bid:
                    eligible_for_tagging = True
                elif source_bid == existing_bid:
                    eligible_for_tagging = True
                else:
                    result.unmatched_rows.append(
                        {
                            "sheet": sheet.title,
                            "row": row_index + 1,
                            "song": song,
                            "artist": artist,
                            "existing_bid": existing_bid,
                            "reason": "未找到歌名和歌手同时一致的 BID 库记录",
                        }
                    )
            elif library_bid:
                resolved_bid = library_bid
                result.matched_rows += 1
                eligible_for_tagging = True
            elif source_bid:
                resolved_bid = source_bid
                result.matched_rows += 1
                eligible_for_tagging = True
            elif track_key:
                reason = ""
                if track_key in conflicted_track_keys:
                    reason = "审核库同一歌名和歌手存在多个 BID"
                elif track_key in source_conflicted_track_keys:
                    reason = "源表同一歌名和歌手存在多个 BID"
                result.unmatched_rows.append(
                    {
                        "sheet": sheet.title,
                        "row": row_index + 1,
                        "song": song,
                        "artist": artist,
                        **({"reason": reason} if reason else {}),
                    }
                )
            elif song or artist:
                result.unmatched_rows.append(
                    {
                        "sheet": sheet.title,
                        "row": row_index + 1,
                        "song": song,
                        "artist": artist,
                        "reason": "歌名或歌手为空，无法双字段匹配",
                    }
                )

            column_values.append(resolved_bid)
            current_value = existing_bid
            selected_bid = not wanted_bids or resolved_bid in wanted_bids
            if selected_bid and resolved_bid and resolved_bid != current_value:
                changed_rows[row_index + 1] = resolved_bid

            if not selected_bid or not resolved_bid or cid_index is None:
                continue
            cid_value = row[cid_index] if cid_index < len(row) else ""
            cids = normalise_cids([cid_value])
            if not cids:
                continue
            if not eligible_for_tagging:
                continue
            current_status = _cell_text(row[target_status_index] if target_status_index < len(row) else "")
            for cid in cids:
                result.source_rows.append(
                    FeishuTomatoSourceRow(
                        sheet_id=sheet.sheet_id,
                        sheet_title=sheet.title,
                        row=row_index + 1,
                        cid=cid,
                        bid=resolved_bid,
                        status_column=target_status_index,
                        status=current_status,
                    )
                )
            if current_status == TAG_STATUS_DONE:
                result.already_tagged_rows += len(cids)
                continue
            if current_status not in TAG_STATUS_PENDING:
                result.skipped_sheets.append(
                    {
                        "role": "source_row",
                        "sheet": sheet.title,
                        "row": row_index + 1,
                        "reason": f"未知打标状态：{current_status}",
                    }
                )
                continue
            result.pending_status_rows += len(cids)
            item = grouped.setdefault(resolved_bid, {"cids": [], "songs": [], "tracks": []})
            for cid in cids:
                if cid not in item["cids"]:
                    item["cids"].append(cid)
            canonical_song = lookup_names.get(track_key) or source_bid_names.get(track_key) or song
            if canonical_song and canonical_song not in item["songs"]:
                item["songs"].append(canonical_song)
            track = {"song": song, "artist": artist}
            if track not in item["tracks"]:
                item["tracks"].append(track)

        column = _column_name(target_bid_index)
        if bid_missing and not wanted_bids:
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
            if bid_missing:
                header_cell = f"{column}{header_row_index + 1}"
                result.write_ranges.append(
                    FeishuValueRange(
                        range=f"{sheet.sheet_id}!{header_cell}:{header_cell}",
                        values=[["bid"]],
                        sheet_id=sheet.sheet_id,
                        sheet_title=sheet.title,
                        reason="新增 BID 表头",
                    )
                )
            result.write_ranges.extend(
                _changed_row_ranges(
                    sheet,
                    column,
                    changed_rows,
                    reason="回填空白 BID" if not overwrite_existing_bid else "回填或覆盖 BID",
                )
            )
        if status_index is None:
            status_column = _column_name(target_status_index)
            status_header_cell = f"{status_column}{header_row_index + 1}"
            result.write_ranges.append(
                FeishuValueRange(
                    range=f"{sheet.sheet_id}!{status_header_cell}:{status_header_cell}",
                    values=[["打标状态"]],
                    sheet_id=sheet.sheet_id,
                    sheet_title=sheet.title,
                    reason="新增打标状态表头",
                )
            )

    result.batches = [
        TomatoMusicTagBatch(
            bid=bid,
            tag=tag_for_bid(bid),
            cids=list(values["cids"]),
            song_names=list(values["songs"]),
            tracks=list(values["tracks"]),
        )
        for bid, values in grouped.items()
        if values["cids"]
    ]
    if not result.batches:
        raise FeishuApiError("飞书源表中没有形成包含有效 BID 和 CID 的打标批次。")
    if writeback:
        source_sheet_map = {item.sheet_id: item for item in source_sheets}
        for sheet_id, required_column_count in required_columns.items():
            sheet = source_sheet_map.get(sheet_id)
            if sheet is not None:
                client.ensure_column_capacity(source_token, sheet, required_column_count)
    if writeback and result.write_ranges:
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
    candidates: dict[str, list[tuple[str, str, str, str, int]]] = {}
    skipped: list[dict] = []
    for sheet in sheets:
        rows = client.read_range(spreadsheet_token, f"{sheet.sheet_id}!A:{_column_name(MAX_READ_COLUMNS - 1)}")
        header = _find_library_header(rows)
        if header is None:
            skipped.append({"role": "library", "sheet": sheet.title, "reason": "未同时找到歌名、歌手和 bookid/bid 列"})
            continue
        header_row_index, song_index, artist_index, bid_index = header
        for row_index in range(header_row_index + 1, len(rows)):
            row = rows[row_index]
            song = _cell_text(row[song_index] if song_index < len(row) else "")
            artist = _cell_text(row[artist_index] if artist_index < len(row) else "")
            bid = normalise_bid(_cell_text(row[bid_index] if bid_index < len(row) else ""))
            normalised_track = _normalise_track(song, artist)
            if not all(normalised_track) or not bid:
                continue
            track_key = _track_lookup_key(*normalised_track)
            candidate = (bid, song, artist, sheet.title, row_index + 1)
            if candidate not in candidates.setdefault(track_key, []):
                candidates[track_key].append(candidate)

    lookup: dict[str, str] = {}
    names: dict[str, str] = {}
    conflicts: list[dict] = []
    for track_key, rows in candidates.items():
        bids = []
        for bid, _, _, _, _ in rows:
            if bid not in bids:
                bids.append(bid)
        if len(bids) != 1:
            conflicts.append(
                {
                    "song": rows[0][1],
                    "artist": rows[0][2],
                    "bids": bids,
                    "locations": [
                        {"sheet": sheet, "row": row, "bid": bid}
                        for bid, _, _, sheet, row in rows
                    ],
                }
            )
            continue
        lookup[track_key] = bids[0]
        names[track_key] = rows[0][1]
    return lookup, names, conflicts, skipped


def _find_library_header(rows: list[list[object]]) -> tuple[int, int, int, int] | None:
    song_headers = {"歌名", "歌曲名", "song", "songname"}
    artist_headers = {"歌手", "歌手名", "歌手名称", "艺人", "艺人名", "artist", "artistname", "singer"}
    bid_headers = {"bid", "bookid", "书籍id", "小说id"}
    for row_index, row in enumerate(rows[:30]):
        compact = [_compact_header(value) for value in row]
        song_index = next((index for index, value in enumerate(compact) if value in song_headers), None)
        artist_index = next((index for index, value in enumerate(compact) if value in artist_headers), None)
        bid_index = next((index for index, value in enumerate(compact) if value in bid_headers), None)
        if song_index is not None and artist_index is not None and bid_index is not None:
            return row_index, song_index, artist_index, bid_index
    return None


def _find_source_header(
        rows: list[list[object]],
) -> tuple[int, int, int, int | None, int | None, int | None] | None:
    song_headers = {"歌名", "歌曲名", "song", "songname"}
    artist_headers = {"歌手", "歌手名", "歌手名称", "艺人", "艺人名", "artist", "artistname", "singer"}
    cid_headers = {"cid", "素材cid", "creativeid", "素材id"}
    bid_headers = {"bid", "bookid", "书籍id", "小说id"}
    for row_index, row in enumerate(rows[:30]):
        compact = [_compact_header(value) for value in row]
        song_index = next((index for index, value in enumerate(compact) if value in song_headers), None)
        artist_index = next((index for index, value in enumerate(compact) if value in artist_headers), None)
        if song_index is None or artist_index is None:
            continue
        cid_index = next((index for index, value in enumerate(compact) if value in cid_headers), None)
        bid_index = next((index for index, value in enumerate(compact) if value in bid_headers), None)
        status_index = next((index for index, value in enumerate(compact) if value in TAG_STATUS_HEADERS), None)
        if status_index is None and cid_index is not None:
            candidate = cid_index + 1
            column_values = [
                _cell_text(row_item[candidate] if candidate < len(row_item) else "")
                for row_item in rows[row_index + 1:row_index + 21]
            ]
            if any(value in {TAG_STATUS_DONE, "未打标"} for value in column_values):
                status_index = candidate
        return row_index, song_index, artist_index, cid_index, bid_index, status_index
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


def _normalise_artist(value: object) -> str:
    return re.sub(r"\s+", "", _cell_text(value)).casefold()


def _normalise_track(song: object, artist: object) -> tuple[str, str]:
    return _normalise_song(song), _normalise_artist(artist)


def _track_lookup_key(song_key: str, artist_key: str) -> str:
    return f"{song_key}\x1f{artist_key}"


def _split_track_lookup_key(track_key: str) -> tuple[str, str]:
    song, separator, artist = str(track_key).partition("\x1f")
    return song, artist if separator else ""


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
