#!/usr/bin/env python3
"""Authorize a Feishu user and sync the keyword policy without storing tokens."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
import webbrowser

from sync_feishu_keyword_policy import (
    DEFAULT_FEISHU_BASE_URL,
    DEFAULT_POLICY_PATH,
    DEFAULT_WIKI_URL,
    FeishuApiError,
    sync_keyword_policy,
)


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "FeishuOAuthCallback/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        if parsed.path != self.server.callback_path:  # type: ignore[attr-defined]
            self.send_error(404)
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        self.server.oauth_result = {  # type: ignore[attr-defined]
            key: values[0] if values else "" for key, values in query.items()
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<html><meta charset='utf-8'><body>飞书授权完成，可以关闭此页面。</body></html>".encode("utf-8")
        )
        threading.Thread(target=self.server.shutdown, daemon=True).start()  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        return


def _exchange_code(*, base_url: str, app_id: str, app_secret: str, code: str, redirect_uri: str) -> str:
    url = f"{base_url.rstrip('/')}/authen/v2/oauth/token"
    body = json.dumps(
        {
            "grant_type": "authorization_code",
            "client_id": app_id,
            "client_secret": app_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeishuApiError(f"飞书 OAuth 换取用户 Token 失败：{exc}") from exc
    if payload.get("code") not in (None, 0):
        raise FeishuApiError(
            f"飞书 OAuth 换取用户 Token 失败：code={payload.get('code')}, msg={payload.get('msg', '')}"
        )
    token = str(payload.get("access_token") or payload.get("data", {}).get("access_token") or "").strip()
    if not token:
        raise FeishuApiError("飞书 OAuth 响应中没有 access_token。")
    return token


def _authorize_and_capture(*, app_id: str, redirect_uri: str, scope: str, state: str) -> dict[str, str]:
    parsed = urlsplit(redirect_uri)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FeishuApiError("--redirect-uri 必须是带主机名的 http(s) 地址。")
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise FeishuApiError("本地回调只支持 http://127.0.0.1 或 http://localhost。")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise FeishuApiError("--redirect-uri 端口无效。") from exc
    callback_path = parsed.path or "/callback"
    server = HTTPServer((parsed.hostname, port), _CallbackHandler)
    server.callback_path = callback_path  # type: ignore[attr-defined]
    server.oauth_result = {}  # type: ignore[attr-defined]
    query = {"app_id": app_id, "redirect_uri": redirect_uri, "state": state}
    if scope.strip():
        query["scope"] = scope.strip()
    authorize_url = "https://open.feishu.cn/open-apis/authen/v1/authorize?" + urlencode(query)
    print("请在浏览器中完成飞书登录和授权：")
    print(authorize_url)
    try:
        webbrowser.open(authorize_url)
    except Exception:
        pass
    print("等待本机 OAuth 回调……")
    server.serve_forever()
    result = dict(server.oauth_result)  # type: ignore[attr-defined]
    if result.get("state") != state:
        raise FeishuApiError("飞书 OAuth state 校验失败。")
    if result.get("error"):
        raise FeishuApiError(f"飞书 OAuth 授权失败：{result.get('error_description') or result['error']}")
    if not result.get("code"):
        raise FeishuApiError("飞书 OAuth 回调没有返回授权码。")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="通过飞书用户 OAuth 读取外部 Wiki 词库。")
    parser.add_argument("--wiki-url", default=os.environ.get("FEISHU_KEYWORD_POLICY_URL") or DEFAULT_WIKI_URL)
    parser.add_argument("--output", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--app-id", default=os.environ.get("FEISHU_APP_ID", ""))
    parser.add_argument("--app-secret", default=os.environ.get("FEISHU_APP_SECRET", ""))
    parser.add_argument("--redirect-uri", default="http://127.0.0.1:8765/callback")
    parser.add_argument(
        "--scope",
        default="wiki:wiki:readonly docx:document:readonly",
        help="User OAuth scopes separated by spaces.",
    )
    parser.add_argument("--api-base-url", default=os.environ.get("FEISHU_API_BASE_URL") or DEFAULT_FEISHU_BASE_URL)
    args = parser.parse_args()
    if not args.app_id or not args.app_secret:
        raise FeishuApiError("需要 FEISHU_APP_ID 和 FEISHU_APP_SECRET，或传入 --app-id/--app-secret。")
    state = secrets.token_urlsafe(24)
    callback = _authorize_and_capture(
        app_id=args.app_id,
        redirect_uri=args.redirect_uri,
        scope=args.scope,
        state=state,
    )
    access_token = _exchange_code(
        base_url=args.api_base_url,
        app_id=args.app_id,
        app_secret=args.app_secret,
        code=callback["code"],
        redirect_uri=args.redirect_uri,
    )
    result = sync_keyword_policy(
        wiki_url=args.wiki_url,
        output_path=args.output,
        access_token=access_token,
        base_url=args.api_base_url,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
