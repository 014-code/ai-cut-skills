from __future__ import annotations

"""Local PKCE OAuth helper for Feishu user_access_token.

Interactive authorization stays on the loopback callback.  Optional persistent
authorization stores only OAuth token data in a Windows CurrentUser DPAPI blob;
the App Secret remains environment-only and no token enters task artifacts.
"""

from dataclasses import dataclass
import base64
import ctypes
import getpass
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
import webbrowser

from .usergrowth_feishu_sheets import FeishuApiError


DEFAULT_FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
DEFAULT_FEISHU_OAUTH_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
DEFAULT_FEISHU_OAUTH_REDIRECT_URI = "http://127.0.0.1:8765/callback"
DEFAULT_FEISHU_OAUTH_SCOPE = "wiki:node:read sheets:spreadsheet:readonly sheets:spreadsheet"
FEISHU_OFFLINE_SCOPE = "offline_access"
TOKEN_CACHE_VERSION = 1
TOKEN_EXPIRY_SKEW_SECONDS = 120
DPAPI_ENTROPY = b"aivideoeditor-usergrowth-feishu-oauth-v1"


@dataclass(frozen=True)
class FeishuOAuthConfig:
    app_id: str
    app_secret: str
    redirect_uri: str = DEFAULT_FEISHU_OAUTH_REDIRECT_URI
    scope: str = DEFAULT_FEISHU_OAUTH_SCOPE
    authorize_url: str = DEFAULT_FEISHU_AUTHORIZE_URL
    token_url: str = DEFAULT_FEISHU_OAUTH_TOKEN_URL
    timeout_seconds: float = 300.0
    open_browser: bool = True
    authorize_url_file: str = ""
    persist_token: bool = False
    token_cache_path: str = ""
    force_reauthorize: bool = False
    bootstrap_credentials: bool = False
    bootstrap_account: str = ""
    bootstrap_password: str = ""


@dataclass(frozen=True)
class FeishuOAuthTokens:
    access_token: str
    refresh_token: str = ""
    expires_in: int = 0
    refresh_expires_in: int = 0
    scope: str = ""
    token_type: str = "Bearer"


class FeishuOAuthError(FeishuApiError):
    """Raised when the interactive Feishu authorization cannot complete."""


def obtain_user_access_token(config: FeishuOAuthConfig) -> str:
    """Return a user token from DPAPI cache/refresh or a local PKCE callback."""

    app_id = str(config.app_id or "").strip()
    app_secret = str(config.app_secret or "").strip()
    redirect_uri = str(config.redirect_uri or "").strip()
    scope = " ".join(str(config.scope or "").split())
    if config.persist_token:
        scope = _scope_with_offline_access(scope)
    if not app_id or not app_secret:
        raise FeishuOAuthError(
            "飞书 OAuth 需要 FEISHU_APP_ID 和 FEISHU_APP_SECRET（建议通过环境变量提供）。"
        )
    host, port, callback_path = _parse_loopback_redirect(redirect_uri)
    if not scope:
        raise FeishuOAuthError("飞书 OAuth scope 不能为空。")
    cache_path = _resolve_token_cache_path(config, app_id) if config.persist_token else None
    if cache_path is not None and not config.force_reauthorize:
        cached = _load_cached_tokens(cache_path, app_id=app_id, required_scope=scope)
        if cached is not None:
            now = int(time.time())
            if cached["access_token"] and int(cached.get("access_expires_at") or 0) > (
                    now + TOKEN_EXPIRY_SKEW_SECONDS
            ):
                return str(cached["access_token"])
            refresh_token = str(cached.get("refresh_token") or "")
            refresh_expires_at = int(cached.get("refresh_expires_at") or 0)
            if refresh_token and (
                    not refresh_expires_at
                    or refresh_expires_at > now + TOKEN_EXPIRY_SKEW_SECONDS
            ):
                try:
                    refreshed = _refresh_user_access_token(config, refresh_token)
                except FeishuOAuthError:
                    refreshed = None
                if refreshed is not None:
                    if not refreshed.refresh_token or not refreshed.refresh_expires_in:
                        refreshed = FeishuOAuthTokens(
                            access_token=refreshed.access_token,
                            refresh_token=refreshed.refresh_token or refresh_token,
                            expires_in=refreshed.expires_in,
                            refresh_expires_in=refreshed.refresh_expires_in or max(refresh_expires_at - now, 0),
                            scope=refreshed.scope or str(cached.get("scope") or scope),
                            token_type=refreshed.token_type,
                        )
                    _save_cached_tokens(cache_path, app_id=app_id, scope=scope, tokens=refreshed)
                    return refreshed.access_token

    bootstrap_account = ""
    bootstrap_password = ""
    if config.bootstrap_credentials:
        if not config.open_browser:
            raise FeishuOAuthError(
                "首次飞书账号密码授权需要可见浏览器；请移除 --feishu-oauth-no-browser，"
                "或先在受控浏览器中完成一次普通 OAuth 授权。"
            )
        # Resolve only on a cache miss. The values stay in local variables and
        # are never included in task artifacts, logs, or the encrypted token cache.
        bootstrap_account, bootstrap_password = _resolve_bootstrap_credentials(config)

    state = secrets.token_urlsafe(32)
    code_verifier = _make_code_verifier()
    code_challenge = _make_code_challenge(code_verifier)
    callback = _OAuthCallback(state=state, path=callback_path)
    server = HTTPServer((host, port), callback.handler_type())
    callback.server = server

    query = urlencode(
        {
            "client_id": app_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "consent",
        }
    )
    authorize_url = f"{str(config.authorize_url).rstrip('?&')}?{query}"
    authorize_url_path = Path(config.authorize_url_file).resolve() if config.authorize_url_file else None
    if authorize_url_path is not None:
        authorize_url_path.parent.mkdir(parents=True, exist_ok=True)
        authorize_url_path.write_text(authorize_url, encoding="utf-8")
    if config.bootstrap_credentials:
        browser_thread = threading.Thread(
            target=_run_bootstrap_browser,
            kwargs={
                "authorize_url": authorize_url,
                "account": bootstrap_account,
                "password": bootstrap_password,
                "callback": callback,
                "timeout_seconds": float(config.timeout_seconds),
            },
            name="feishu-oauth-bootstrap",
            daemon=True,
        )
        browser_thread.start()
    elif config.open_browser:
        try:
            webbrowser.open(authorize_url, new=2, autoraise=True)
        except Exception:
            # The URL contains no secret or token.  Printing it is a safe
            # fallback when the system browser is unavailable.
            print(f"请在浏览器打开飞书授权地址：{authorize_url}", flush=True)
    else:
        print(f"请在浏览器打开飞书授权地址：{authorize_url}", flush=True)

    server.timeout = 0.5
    deadline = time.monotonic() + max(10.0, float(config.timeout_seconds))
    try:
        while time.monotonic() < deadline and not callback.done.is_set():
            server.handle_request()
    finally:
        server.server_close()
        if authorize_url_path is not None:
            try:
                authorize_url_path.unlink(missing_ok=True)
            except OSError:
                pass

    if not callback.done.is_set():
        raise FeishuOAuthError("等待飞书 OAuth 回调超时；请确认已在安全设置中配置本地回调地址。")
    if callback.error:
        raise FeishuOAuthError(f"飞书 OAuth 授权失败：{callback.error}")
    if not callback.code:
        raise FeishuOAuthError("飞书 OAuth 回调未返回授权码。")
    tokens = _exchange_authorization_code(config, callback.code, code_verifier)
    if cache_path is not None:
        if not tokens.refresh_token:
            raise FeishuOAuthError(
                "飞书 OAuth 未返回 refresh_token；请确认应用已允许 offline_access 并重新授权。"
            )
        _save_cached_tokens(cache_path, app_id=app_id, scope=scope, tokens=tokens)
    return tokens.access_token


def _resolve_bootstrap_credentials(config: FeishuOAuthConfig) -> tuple[str, str]:
    """Read first-run credentials without persisting them or putting them in argv."""

    account = str(
        config.bootstrap_account
        or os.environ.get("FEISHU_BOOTSTRAP_ACCOUNT")
        or ""
    ).strip()
    password = str(
        config.bootstrap_password
        or os.environ.get("FEISHU_BOOTSTRAP_PASSWORD")
        or ""
    )
    try:
        if not account:
            account = input("首次飞书授权账号（仅本次使用，不会保存）：").strip()
        if not password:
            password = getpass.getpass("首次飞书授权密码（仅本次使用，不会保存）：")
    except (EOFError, KeyboardInterrupt) as exc:
        raise FeishuOAuthError(
            "无法读取首次飞书账号密码；请通过 FEISHU_BOOTSTRAP_ACCOUNT 和 "
            "FEISHU_BOOTSTRAP_PASSWORD 仅在首授进程中提供。"
        ) from exc
    if not account or not password:
        raise FeishuOAuthError("首次飞书授权需要账号和密码；两者都不会写入缓存或任务产物。")
    return account, password


def _run_bootstrap_browser(
        *,
        authorize_url: str,
        account: str,
        password: str,
        callback: _OAuthCallback,
        timeout_seconds: float,
) -> None:
    """Complete the first Feishu OAuth grant in a visible, temporary browser."""

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = _launch_bootstrap_browser(playwright)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(authorize_url, wait_until="domcontentloaded", timeout=60000)
                deadline = time.monotonic() + max(30.0, float(timeout_seconds))
                _complete_feishu_login(page, account, password, deadline)
                _click_feishu_authorize(page, deadline)
                while time.monotonic() < deadline and not callback.done.is_set():
                    page.wait_for_timeout(250)
                if not callback.done.is_set():
                    callback.error = "首次飞书登录未在规定时间内完成授权回调。"
                    callback.done.set()
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        message = str(exc) or type(exc).__name__
        for secret in (account, password):
            if secret:
                message = message.replace(secret, "<redacted>")
        if "Playwright" in message or "playwright" in message:
            message = "首次飞书授权需要 Playwright 浏览器依赖；请先安装 requirements.txt 并执行 playwright install chromium。"
        callback.error = f"首次飞书自动登录失败：{message[:300]}"
        callback.done.set()


def _launch_bootstrap_browser(playwright):
    for channel in ("msedge", "chrome"):
        try:
            return playwright.chromium.launch(channel=channel, headless=False)
        except Exception:
            continue
    return playwright.chromium.launch(headless=False)


def _visible_locator(page, selectors: tuple[str, ...]):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible():
                return locator
        except Exception:
            continue
    return None


def _visible_text(page, text: str) -> bool:
    try:
        return page.get_by_text(text, exact=True).count() > 0 and page.get_by_text(text, exact=True).first.is_visible()
    except Exception:
        return False


def _click_text_if_visible(page, text: str) -> bool:
    try:
        locator = page.get_by_text(text, exact=True).first
        if locator.count() and locator.is_visible():
            locator.click()
            return True
    except Exception:
        pass
    return False


def _click_button_if_visible(page, names: tuple[str, ...]) -> bool:
    for name in names:
        try:
            locator = page.get_by_role("button", name=name, exact=True).first
            if locator.count() and locator.is_visible() and locator.is_enabled():
                locator.click()
                return True
        except Exception:
            continue
    return False


def _has_login_challenge(page) -> bool:
    challenge_terms = ("验证码", "安全验证", "二次验证", "滑块验证", "身份验证")
    return any(_visible_text(page, term) for term in challenge_terms)


def _complete_feishu_login(page, account: str, password: str, deadline: float) -> None:
    """Fill the common domestic Feishu account/password flow, then wait for consent."""

    if _visible_text(page, "授权"):
        return
    _click_button_if_visible(page, ("使用其他账号",))
    account_filled = False
    password_filled = False
    while time.monotonic() < deadline:
        if _has_login_challenge(page):
            raise FeishuOAuthError("飞书首次登录出现验证码或二次验证，请在浏览器中完成后重新运行。")

        if not account_filled:
            is_email = "@" in account
            if is_email:
                _click_text_if_visible(page, "邮箱")
                account_locator = _visible_locator(
                    page,
                    ("input[placeholder*='邮箱']", "input[type='email']"),
                )
            else:
                _click_text_if_visible(page, "手机号")
                account_locator = _visible_locator(
                    page,
                    ("input[placeholder*='手机号']", "input[type='tel']"),
                )
            if account_locator is not None:
                account_locator.fill(account)
                checkbox = _visible_locator(page, ("input[type='checkbox']",))
                if checkbox is not None:
                    try:
                        checkbox.check()
                    except Exception:
                        pass
                else:
                    try:
                        page.get_by_role("checkbox").first.check()
                    except Exception:
                        pass
                if not _click_button_if_visible(page, ("下一步",)):
                    raise FeishuOAuthError("飞书登录表单未能提交账号；请确认账号类型和服务协议状态。")
                account_filled = True

        if not password_filled:
            _click_text_if_visible(page, "密码登录")
            password_locator = _visible_locator(
                page,
                (
                    "input[type='password']",
                    "input[placeholder*='密码']",
                    "input[autocomplete='current-password']",
                ),
            )
            if password_locator is not None:
                password_locator.fill(password)
                if not _click_button_if_visible(page, ("登录", "下一步")):
                    raise FeishuOAuthError("飞书登录表单未能提交密码。")
                password_filled = True

        if _visible_text(page, "账号或密码错误") or _visible_text(page, "密码错误"):
            raise FeishuOAuthError("飞书账号或密码错误。")
        if _visible_text(page, "授权") or _visible_text(page, "请求以下飞书账号进行授权"):
            return
        if password_filled and (_visible_text(page, "登录成功") or _visible_text(page, "首页")):
            return
        try:
            page.wait_for_timeout(300)
        except Exception:
            break
    raise FeishuOAuthError("飞书首次登录超时；若出现验证码、SSO 或二次验证，请在浏览器中完成后重新运行。")


def _click_feishu_authorize(page, deadline: float) -> None:
    while time.monotonic() < deadline:
        if _click_button_if_visible(page, ("授权", "同意")):
            return
        if _has_login_challenge(page):
            raise FeishuOAuthError("飞书授权出现验证码或二次验证，请在浏览器中完成后重新运行。")
        try:
            page.wait_for_timeout(300)
        except Exception:
            break
    raise FeishuOAuthError("飞书授权页面未出现“授权”按钮。")


def _parse_loopback_redirect(redirect_uri: str) -> tuple[str, int, str]:
    parsed = urlsplit(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise FeishuOAuthError(
            "为避免授权码暴露，当前实现只接受 http://127.0.0.1 或 localhost 的本地回调地址。"
        )
    if parsed.query or parsed.fragment or not parsed.path:
        raise FeishuOAuthError("OAuth 回调地址不能包含 query/fragment，且必须包含路径。")
    try:
        port = int(parsed.port or 80)
    except ValueError as exc:
        raise FeishuOAuthError("OAuth 回调端口无效。") from exc
    if not 1 <= port <= 65535:
        raise FeishuOAuthError("OAuth 回调端口必须在 1-65535 范围内。")
    host = parsed.hostname or "127.0.0.1"
    return host, port, parsed.path


def _make_code_verifier() -> str:
    # RFC 7636 allows 43-128 URL-safe characters.
    return secrets.token_urlsafe(64)[:96]


def _make_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _OAuthCallback:
    def __init__(self, *, state: str, path: str) -> None:
        self.state = state
        self.path = path
        self.server: HTTPServer | None = None
        self.done = threading.Event()
        self.code = ""
        self.error = ""

    def handler_type(self):
        callback = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urlsplit(self.path)
                if parsed.path != callback.path:
                    self.send_error(404)
                    return
                query = parse_qs(parsed.query)
                returned_state = str((query.get("state") or [""])[0])
                if not secrets.compare_digest(returned_state, callback.state):
                    callback.error = "state 校验失败。"
                elif query.get("error"):
                    callback.error = str((query.get("error_description") or query.get("error") or ["unknown"])[0])
                else:
                    callback.code = str((query.get("code") or [""])[0]).strip()
                    if not callback.code:
                        callback.error = "回调中没有 code。"
                body = (
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>飞书授权完成</title>"
                    "<p>飞书授权已完成，可以返回终端继续运行。此窗口可以关闭。</p>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                callback.done.set()

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

        return Handler


def _exchange_authorization_code(
        config: FeishuOAuthConfig,
        code: str,
        code_verifier: str,
) -> FeishuOAuthTokens:
    payload = {
        "grant_type": "authorization_code",
        "client_id": str(config.app_id).strip(),
        "client_secret": str(config.app_secret).strip(),
        "code": code,
        "redirect_uri": str(config.redirect_uri).strip(),
        "code_verifier": code_verifier,
    }
    return _request_oauth_tokens(config, payload, operation="换取 Token")


def _refresh_user_access_token(
        config: FeishuOAuthConfig,
        refresh_token: str,
) -> FeishuOAuthTokens:
    payload = {
        "grant_type": "refresh_token",
        "client_id": str(config.app_id).strip(),
        "client_secret": str(config.app_secret).strip(),
        "refresh_token": str(refresh_token).strip(),
    }
    return _request_oauth_tokens(config, payload, operation="刷新 Token")


def _request_oauth_tokens(
        config: FeishuOAuthConfig,
        payload: dict,
        *,
        operation: str,
) -> FeishuOAuthTokens:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        str(config.token_url),
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(5.0, float(config.timeout_seconds))) as response:
            response_body = response.read()
    except HTTPError as exc:
        response_body = exc.read()
        detail = response_body.decode("utf-8", errors="replace")[:300]
        raise FeishuOAuthError(
            f"飞书 OAuth {operation}失败（HTTP {exc.code}）：{detail}"
        ) from exc
    except (URLError, OSError) as exc:
        raise FeishuOAuthError(f"飞书 OAuth {operation}网络错误：{type(exc).__name__}。") from exc
    try:
        result = json.loads(response_body.decode("utf-8")) if response_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeishuOAuthError(f"飞书 OAuth {operation}返回了无效 JSON。") from exc
    if not isinstance(result, dict):
        raise FeishuOAuthError(f"飞书 OAuth {operation}返回格式无效。")
    if result.get("code") not in (None, 0):
        raise FeishuOAuthError(f"飞书 OAuth {operation}失败：{result.get('msg') or 'unknown error'}")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    access_token = str(
        result.get("access_token")
        or result.get("user_access_token")
        or data.get("access_token")
        or data.get("user_access_token")
        or ""
    ).strip()
    if not access_token:
        raise FeishuOAuthError("飞书 OAuth 响应中没有 user_access_token。")
    return FeishuOAuthTokens(
        access_token=access_token,
        refresh_token=str(result.get("refresh_token") or data.get("refresh_token") or "").strip(),
        expires_in=_safe_int(result.get("expires_in") or data.get("expires_in")),
        refresh_expires_in=_safe_int(
            result.get("refresh_expires_in") or data.get("refresh_expires_in")
        ),
        scope=str(result.get("scope") or data.get("scope") or "").strip(),
        token_type=str(result.get("token_type") or data.get("token_type") or "Bearer").strip(),
    )


def _scope_with_offline_access(scope: str) -> str:
    values = list(dict.fromkeys(str(scope or "").split()))
    if FEISHU_OFFLINE_SCOPE not in values:
        values.append(FEISHU_OFFLINE_SCOPE)
    return " ".join(values)


def _resolve_token_cache_path(config: FeishuOAuthConfig, app_id: str) -> Path:
    if config.token_cache_path:
        return Path(config.token_cache_path).expanduser().resolve()
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if not local_app_data:
        raise FeishuOAuthError("长期飞书授权需要 Windows LOCALAPPDATA 目录。")
    app_hash = hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:12]
    return (
        Path(local_app_data)
        / "Donson"
        / "AIVideoEditor"
        / "oauth"
        / f"feishu_user_{app_hash}.dpapi"
    ).resolve()


def _save_cached_tokens(
        path: Path,
        *,
        app_id: str,
        scope: str,
        tokens: FeishuOAuthTokens,
) -> None:
    now = int(time.time())
    cached_scope = " ".join(
        dict.fromkeys(
            [
                *str(scope or "").split(),
                *str(tokens.scope or "").split(),
            ]
        )
    )
    payload = {
        "version": TOKEN_CACHE_VERSION,
        "app_id": app_id,
        "scope": cached_scope,
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "access_expires_at": now + max(int(tokens.expires_in or 0), 0),
        "refresh_expires_at": now + max(int(tokens.refresh_expires_in or 0), 0),
        "token_type": tokens.token_type,
    }
    protected = _dpapi_protect(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(protected)
    temporary.replace(path)


def _load_cached_tokens(
        path: Path,
        *,
        app_id: str,
        required_scope: str,
) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(_dpapi_unprotect(path.read_bytes()).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, FeishuOAuthError):
        return None
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != TOKEN_CACHE_VERSION:
        return None
    if not secrets.compare_digest(str(payload.get("app_id") or ""), app_id):
        return None
    cached_scope = set(str(payload.get("scope") or "").split())
    if not set(required_scope.split()).issubset(cached_scope):
        return None
    return payload


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    return _DataBlob(len(data), pointer), buffer


def _dpapi_protect(data: bytes) -> bytes:
    return _dpapi_transform(data, protect=True)


def _dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi_transform(data, protect=False)


def _dpapi_transform(data: bytes, *, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise FeishuOAuthError("长期飞书授权缓存仅支持 Windows DPAPI。")
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(DPAPI_ENTROPY)
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_wchar_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = list(crypt32.CryptProtectData.argtypes)
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        success = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
    else:
        success = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
    _ = input_buffer, entropy_buffer
    if not success:
        raise FeishuOAuthError(f"Windows DPAPI 处理失败：{ctypes.get_last_error()}")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

