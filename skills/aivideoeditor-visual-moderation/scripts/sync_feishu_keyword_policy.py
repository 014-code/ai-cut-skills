#!/usr/bin/env python3
"""Synchronize the moderation keyword policy through official Feishu APIs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, Iterable, List, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_WIKI_URL = "https://donsontech.feishu.cn/wiki/MQi5w9llgi2J3UkcOR5c3Ukvn8g"
DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "references" / "feishu_keyword_policy.json"
CHINA_TZ = timezone(timedelta(hours=8))
EXPECTED_GROUPS = ("色情低俗", "封建迷信", "涉军涉政", "竞品及私域导流")
GROUP_ALIASES = {
    "色情低俗": "色情低俗",
    "封建迷信": "封建迷信",
    "涉军涉政": "涉军涉政",
    "竞品及私域导流": "竞品及私域导流",
    "竞品及私域导流违禁词": "竞品及私域导流",
}
DEFAULT_LOCAL_SUPPLEMENTS = {
    "色情低俗": ["装13"],
    "涉军涉政": ["省政府", "市政府", "县政府", "区政府", "政府"],
}
RICH_TEXT_KEYS = (
    "page",
    "text",
    "heading1",
    "heading2",
    "heading3",
    "heading4",
    "heading5",
    "heading6",
    "heading7",
    "heading8",
    "heading9",
    "bullet",
    "ordered",
    "code",
    "quote",
    "todo",
    "callout",
)


class FeishuApiError(RuntimeError):
    pass


class FeishuDocxClient:
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
                "飞书开放文档 API 需要 FEISHU_ACCESS_TOKEN，或同时提供 FEISHU_APP_ID 和 FEISHU_APP_SECRET。"
            )
        payload = self.request_json(
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

    def resolve_wiki_docx(self, wiki_url: str) -> Dict[str, Any]:
        wiki_token = parse_wiki_token(wiki_url)
        payload = self.request_json("GET", "/wiki/v2/spaces/get_node", params={"token": wiki_token})
        node = ((payload.get("data") or {}).get("node") or {})
        obj_type = str(node.get("obj_type") or "").strip().lower()
        document_id = str(node.get("obj_token") or "").strip()
        if obj_type != "docx" or not document_id:
            raise FeishuApiError(
                f"Wiki 节点不是新版云文档或未返回 obj_token：obj_type={obj_type or 'unknown'}"
            )
        return {
            "wiki_token": wiki_token,
            "document_id": document_id,
            "title": str(node.get("title") or "素材尺度规范").strip(),
            "obj_type": obj_type,
            "obj_edit_time": node.get("obj_edit_time"),
            "node": node,
        }

    def list_document_blocks(self, document_id: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_token = ""
        while True:
            params: Dict[str, Any] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            payload = self.request_json(
                "GET",
                f"/docx/v1/documents/{quote(document_id, safe='')}/blocks",
                params=params,
            )
            data = payload.get("data") or {}
            page_items = data.get("items") or []
            items.extend(item for item in page_items if isinstance(item, dict))
            if not data.get("has_more"):
                break
            next_token = str(data.get("page_token") or "").strip()
            if not next_token or next_token == page_token:
                raise FeishuApiError("飞书文档分页返回 has_more，但没有有效的下一页 page_token。")
            page_token = next_token
        return items

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        json_body: Dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.access_token()}"
        url = f"{self.base_url}/{str(path).lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else None
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                request = Request(url, data=body, headers=headers, method=method.upper())
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    status = int(getattr(response, "status", 200) or 200)
                    response_headers = dict(response.headers.items())
                    response_body = response.read()
                    reason = str(getattr(response, "reason", "") or "")
            except HTTPError as exc:
                status = int(exc.code or 0)
                response_headers = dict(exc.headers.items()) if exc.headers else {}
                response_body = exc.read()
                reason = str(exc.reason or "")
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
            if 200 <= status < 300 and code in (None, 0):
                return payload if isinstance(payload, dict) else {}
            message = str(payload.get("msg") or payload.get("message") or "") if isinstance(payload, dict) else ""
            last_error = f"HTTP {status}, code={code}, msg={message or reason}"
            if not (status == 429 or status >= 500) or attempt >= self.max_attempts:
                break
            retry_after = response_headers.get("Retry-After")
            try:
                wait_seconds = float(retry_after) if retry_after else min(2 ** (attempt - 1), 8)
            except ValueError:
                wait_seconds = min(2 ** (attempt - 1), 8)
            time.sleep(max(0.2, wait_seconds))
        raise FeishuApiError(f"飞书 API 请求失败：{method.upper()} {path}；{last_error}")


def parse_wiki_token(reference: str) -> str:
    text = str(reference or "").strip()
    if not text:
        raise FeishuApiError("飞书 Wiki 地址不能为空。")
    parsed = urlsplit(text)
    if parsed.scheme and parsed.netloc:
        segments = [item for item in parsed.path.split("/") if item]
        for index, segment in enumerate(segments[:-1]):
            if segment == "wiki":
                return segments[index + 1]
        raise FeishuApiError(f"无法从地址解析 Wiki token：{text}")
    return text


def _rich_text(elements: Sequence[Any]) -> str:
    output: List[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        text_run = element.get("text_run") or {}
        equation = element.get("equation") or {}
        mention_doc = element.get("mention_doc") or {}
        content = text_run.get("content") or equation.get("content") or mention_doc.get("title") or ""
        if content:
            output.append(str(content))
    return "".join(output).strip()


def block_text(block: Dict[str, Any]) -> str:
    for key in RICH_TEXT_KEYS:
        value = block.get(key)
        if isinstance(value, dict):
            text = _rich_text(value.get("elements") or [])
            if text:
                return text
    return ""


def document_lines(blocks: Iterable[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for block in blocks:
        text = re.sub(r"[\t\r ]+", " ", block_text(block)).strip()
        if text:
            lines.append(text)
    return lines


def _category_from_line(line: str) -> str:
    compact = re.sub(r"\s+", "", line)
    compact = re.sub(r"^[一二三四五六七八九十\d]+[、.．)]", "", compact)
    for alias, canonical in sorted(GROUP_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if compact.startswith(alias):
            return canonical
    return ""


def _terms_from_text(text: str) -> List[str]:
    values: List[str] = []
    for value in re.split(r"[，,、；;\n]+", text):
        term = re.sub(r"^[-*•\s]+", "", value).strip().strip("。.;；,，、")
        if term:
            values.append(term)
    return values


def parse_keyword_groups(lines: Sequence[str]) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {name: [] for name in EXPECTED_GROUPS}
    current = ""
    for line in lines:
        compact = re.sub(r"\s+", "", line)
        if "100%拒审画面" in compact:
            break
        category = _category_from_line(line)
        if category:
            current = category
            continue
        if not current:
            continue
        groups[current].extend(_terms_from_text(line))

    missing = [name for name in EXPECTED_GROUPS if not groups[name]]
    if missing:
        raise FeishuApiError(f"飞书正文解析不完整，以下词组为空：{', '.join(missing)}")
    return {name: _dedupe(groups[name]) for name in EXPECTED_GROUPS}


def _dedupe(values: Iterable[str]) -> List[str]:
    output: List[str] = []
    seen = set()
    for value in values:
        term = str(value or "").strip()
        normalized = re.sub(r"\s+", "", term).casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(term)
    return output


def _modified_time(value: Any, fallback: datetime) -> datetime:
    try:
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=CHINA_TZ)
    except (TypeError, ValueError, OSError, OverflowError):
        return fallback


def _load_local_supplements(path: Path) -> Dict[str, List[str]]:
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            supplements = payload.get("local_supplements") if isinstance(payload, dict) else None
            if isinstance(supplements, dict):
                return {
                    str(category): _dedupe(values)
                    for category, values in supplements.items()
                    if isinstance(values, list)
                }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    return {category: list(values) for category, values in DEFAULT_LOCAL_SUPPLEMENTS.items()}


def sync_keyword_policy(
    *,
    wiki_url: str = DEFAULT_WIKI_URL,
    output_path: Path = DEFAULT_POLICY_PATH,
    access_token: str = "",
    app_id: str = "",
    app_secret: str = "",
    base_url: str = DEFAULT_FEISHU_BASE_URL,
    timeout_seconds: float = 30.0,
) -> Dict[str, Any]:
    client = FeishuDocxClient(
        access_token=access_token or os.environ.get("FEISHU_ACCESS_TOKEN", ""),
        app_id=app_id or os.environ.get("FEISHU_APP_ID", ""),
        app_secret=app_secret or os.environ.get("FEISHU_APP_SECRET", ""),
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    node = client.resolve_wiki_docx(wiki_url)
    blocks = client.list_document_blocks(node["document_id"])
    lines = document_lines(blocks)
    groups = parse_keyword_groups(lines)
    now = datetime.now(tz=CHINA_TZ)
    modified = _modified_time(node.get("obj_edit_time"), now)
    target = Path(output_path).expanduser().resolve()
    payload = {
        "version": f"feishu-{node['wiki_token']}-{modified.strftime('%Y-%m-%d-%H%M')}",
        "synced_at": now.isoformat(timespec="seconds"),
        "source": {
            "type": "feishu_open_api",
            "url": wiki_url,
            "title": node["title"],
            "last_modified": modified.isoformat(timespec="minutes"),
            "wiki_token": node["wiki_token"],
            "document_type": node["obj_type"],
            "api": "wiki/v2/spaces/get_node + docx/v1/documents/{document_id}/blocks",
            "block_count": len(blocks),
        },
        "groups": groups,
        "local_supplements": _load_local_supplements(target),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return {
        "output": str(target),
        "version": payload["version"],
        "source": payload["source"],
        "group_counts": {category: len(values) for category, values in groups.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read 素材尺度规范 through official Feishu Wiki/Docx APIs.")
    parser.add_argument("--wiki-url", default=os.environ.get("FEISHU_KEYWORD_POLICY_URL") or DEFAULT_WIKI_URL)
    parser.add_argument("--output", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--api-base-url", default=os.environ.get("FEISHU_API_BASE_URL") or DEFAULT_FEISHU_BASE_URL)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    result = sync_keyword_policy(
        wiki_url=args.wiki_url,
        output_path=args.output,
        base_url=args.api_base_url,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
