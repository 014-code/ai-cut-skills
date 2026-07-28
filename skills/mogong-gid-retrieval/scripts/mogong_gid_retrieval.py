#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import json
import os
import re
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

URL_HEADER_KEYWORDS = ("url", "link", "链接", "视频链接", "视频url", "地址")
GID_HEADER_KEYWORDS = ("gid", "GID", "视频id", "视频ID", "抖音id", "抖音ID")
KEYWORD_HEADER_KEYWORDS = ("keyword", "keywords", "关键词", "关键字", "搜索词", "搜索关键词")

NEGATIVE_REPLY_KEYWORDS = (
    "找不到",
    "未找到",
    "不存在",
    "没有找到",
    "无法查询",
    "无结果",
    "暂无",
    "not found",
    "抱歉",
    "无授权",
    "状态异常",
    "不可使用",
    "无法使用",
    "换个问题",
)
PENDING_REPLY_KEYWORDS = ("正在处理", "停止生成")
ERROR_REPLY_KEYWORDS = (
    "内部错误",
    "系统错误",
    "系统异常",
    "服务异常",
    "服务器错误",
    "请求失败",
)
POSITIVE_REPLY_KEYWORDS = (
    "保存灵感",
    "保存至灵感库",
    "加入灵感库",
    "查看详情",
    "创意详情",
    "视频链接",
    "下载视频",
    "watermarked_video",
)

QUERY_FAILED_STATUSES = {"mogong_internal_error", "no_reply"}
VIDEO_AVAILABLE_STATUSES = {"downloaded", "reused"}


@dataclass
class VideoReference:
    source_url: str
    gid: Optional[str] = None
    video_url: Optional[str] = None
    error_message: Optional[str] = None
    keyword: Optional[str] = None


@dataclass
class MogongCheckResult:
    gid: str
    exists: bool
    reply: str


@dataclass
class ResultItem:
    gid: str
    video_url: str
    source_url: str
    query_status: str = "matched"
    query_error: Optional[str] = None
    mogong_reply: Optional[str] = None
    download_status: str = "skipped"
    download_path: Optional[str] = None
    download_error: Optional[str] = None


def require_import(module_name: str, install_hint: str):
    try:
        return __import__(module_name)
    except ImportError as exc:
        raise RuntimeError(f"Missing Python package '{module_name}'. Install with: {install_hint}") from exc


def cell_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def normalized_header(value: str) -> str:
    return value.lower().replace(" ", "").replace("_", "")


def detect_column(headers: list[str], keywords: tuple[str, ...]) -> Optional[int]:
    normalized_keywords = tuple(normalized_header(item) for item in keywords)
    for index, header in enumerate(headers):
        normalized = normalized_header(header)
        if any(keyword in normalized for keyword in normalized_keywords):
            return index
    return None


def resolve_requested_column(headers: list[str], requested: Optional[str], keywords: tuple[str, ...]) -> Optional[int]:
    if requested:
        requested = requested.strip()
        for index, header in enumerate(headers):
            if header == requested or normalized_header(header) == normalized_header(requested):
                return index
        if requested.isdigit():
            return max(0, int(requested) - 1)
    return detect_column(headers, keywords)


def load_table_rows(path: Path) -> list[list[object]]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        openpyxl = require_import("openpyxl", "python -m pip install openpyxl")
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheet = workbook.active
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            return [row for row in csv.reader(file)]
    raise ValueError(f"Unsupported input file extension: {path.suffix}. Use .xlsx, .xlsm, or .csv")


def read_input_values(
    path: Path,
    requested_column: Optional[str],
    header_keywords: tuple[str, ...],
) -> list[str]:
    rows = load_table_rows(path)
    if not rows:
        return []
    headers = [cell_text(value) for value in rows[0]]
    column_index = resolve_requested_column(headers, requested_column, header_keywords)
    data_rows = rows[1:] if column_index is not None else rows
    column_index = column_index if column_index is not None else 0
    values = [
        cell_text(row[column_index])
        for row in data_rows
        if column_index < len(row) and cell_text(row[column_index])
    ]
    return unique_preserve_order(values)


def find_douyin_toolkit_core(explicit_path: Optional[str] = None) -> Path:
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())
    environment_path = os.getenv("DOUYIN_VIDEO_TOOLKIT_CORE")
    if environment_path:
        candidates.append(Path(environment_path).expanduser())

    skills_root = Path(__file__).resolve().parents[2]
    candidates.append(skills_root / "douyin-video-toolkit" / "scripts" / "douyin_reference_core.py")
    for home_variable, fallback in (("CODEX_HOME", ".codex"), ("WORKBUDDY_HOME", ".workbuddy")):
        configured_home = os.getenv(home_variable)
        home = Path(configured_home).expanduser() if configured_home else Path.home() / fallback
        candidates.append(home / "skills" / "douyin-video-toolkit" / "scripts" / "douyin_reference_core.py")

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(
        "douyin-video-toolkit is required but douyin_reference_core.py was not found. "
        f"Install/sync the skill or pass --douyin-toolkit-core. Searched: {searched}"
    )


def load_douyin_toolkit_core(explicit_path: Optional[str] = None):
    path = find_douyin_toolkit_core(explicit_path)
    module_name = "_ai_cut_douyin_reference_core"
    existing = sys.modules.get(module_name)
    if existing is not None and Path(existing.__file__).resolve() == path:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load douyin-video-toolkit core: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def wanbang_client_from_args(args, toolkit):
    return toolkit.WanbangClient(
        args.wanbang_key or os.getenv("WANBANG_API_KEY", ""),
        args.wanbang_secret or os.getenv("WANBANG_API_SECRET", ""),
        args.wanbang_base_url or os.getenv("WANBANG_DOUYIN_BASE_URL", ""),
        retry_count=args.wanbang_retry_count,
        retry_delay_seconds=args.wanbang_retry_delay_seconds,
    )


def convert_toolkit_references(references) -> list[VideoReference]:
    return [
        VideoReference(
            source_url=reference.source_url,
            gid=reference.gid or None,
            video_url=reference.video_url or None,
            error_message=reference.error or None,
            keyword=reference.keyword or None,
        )
        for reference in references
    ]


class MogongCreativeAssistantClient:
    def __init__(
        self,
        account: str,
        password: str,
        customer_id: str,
        *,
        headless: bool = True,
        timeout_ms: int = 45000,
        debug_dir: Optional[Path] = None,
    ) -> None:
        self.account = account
        self.password = password
        self.customer_id = customer_id
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.debug_dir = debug_dir

    async def check_gids(self, gids: Iterable[str]) -> list[MogongCheckResult]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright is required for Mogong querying. Install with: "
                "python -m pip install playwright && python -m playwright install chromium"
            ) from exc

        results: list[MogongCheckResult] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self.headless, args=self._launch_args())
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
            page = await context.new_page()
            page.set_default_timeout(self.timeout_ms)
            try:
                await self._open_customer(page)
                await self._snapshot(page, "01_open_customer")
                await self._ensure_login(page)
                await self._snapshot(page, "02_after_login")
                await self._select_customer(page)
                await self._snapshot(page, "03_after_select_customer")
                await self._open_creative_assistant(page)
                await self._snapshot(page, "04_after_open_assistant")
                await self._select_gid_ability(page)
                await self._snapshot(page, "05_after_select_gid")
                for gid in gids:
                    reply = await self._ask_gid(page, gid)
                    results.append(MogongCheckResult(gid=gid, exists=self._classify_exists(reply), reply=reply))
            finally:
                try:
                    await context.close()
                finally:
                    await browser.close()
        return results

    async def _open_customer(self, page) -> None:
        await self._safe_goto(page, "https://usergrowth.com.cn/open/customer")

    async def _ensure_login(self, page) -> None:
        if not self.account or not self.password:
            raise RuntimeError("Mogong account/password are required")
        text = await page.locator("body").inner_text(timeout=5000)
        if "登录" not in text and "密码" not in text:
            return

        captcha_solver = None
        for _ in range(5):
            await page.locator("input[placeholder='请输入注册邮箱']").fill(self.account)
            await page.locator("input[type='password']").first.fill(self.password)
            captcha_input = page.locator("input[placeholder='请输入图片验证码']").first
            if await captcha_input.count():
                if captcha_solver is None:
                    captcha_solver = self._create_captcha_solver()
                captcha_image = page.locator("img").nth(3)
                captcha = captcha_solver(await captcha_image.screenshot())
                await captcha_input.fill(captcha)
            await page.locator("button").first.click()
            await page.wait_for_timeout(4000)
            await self._safe_goto(page, "https://usergrowth.com.cn/open/customer")
            await page.wait_for_timeout(1500)
            text = await page.locator("body").inner_text(timeout=5000)
            if self.customer_id in text or "客户列表" in text:
                return
            if "登录" not in text and "密码" not in text and "验证码错误" not in text:
                return
        raise RuntimeError("Mogong login failed: captcha could not be solved")

    @staticmethod
    def _create_captcha_solver():
        try:
            import ddddocr
        except ImportError as exc:
            raise RuntimeError("ddddocr is required when Mogong login shows an image captcha") from exc
        ocr = ddddocr.DdddOcr(show_ad=False)
        return lambda image_bytes: ocr.classification(image_bytes).strip()

    async def _select_customer(self, page) -> None:
        await self._safe_goto(page, "https://usergrowth.com.cn/open/customer")
        await page.wait_for_timeout(2000)
        if await page.locator("input").count():
            search_input = page.locator("input").first
            await search_input.fill(self.customer_id)
            await search_input.press("Enter")
            await page.wait_for_timeout(2000)

        body_text = ""
        for _ in range(10):
            body_text = await page.locator("body").inner_text()
            if self.customer_id in body_text:
                break
            await page.wait_for_timeout(1000)
        if self.customer_id not in body_text:
            await self._snapshot(page, "customer_not_found")
            raise RuntimeError(f"Mogong customer id not found: {self.customer_id}")

        for locator in (
            page.locator("button:has-text('进 入')").first,
            page.locator("button:has-text('进入')").first,
            page.get_by_role("button", name="进 入").first,
            page.get_by_role("button", name="进入").first,
            page.get_by_text("进 入", exact=True).first,
            page.get_by_text("进入", exact=True).first,
        ):
            try:
                if await locator.count():
                    await locator.click(force=True)
                    if await self._wait_customer_entered(page, seconds=5):
                        return
                    box = await locator.bounding_box()
                    if box:
                        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        if await self._wait_customer_entered(page, seconds=5):
                            return
            except Exception:
                continue

        await page.evaluate(
            """() => {
                const normalize = (value) => (value || '').replace(/\\s/g, '');
                const candidates = [...document.querySelectorAll('button, div, span')]
                    .filter((element) => {
                        const rect = element.getBoundingClientRect();
                        return normalize(element.innerText) === '进入'
                            && rect.width > 30
                            && rect.height > 12
                            && rect.bottom > 0
                            && rect.right > 0;
                    })
                    .sort((left, right) => {
                        const a = left.getBoundingClientRect();
                        const b = right.getBoundingClientRect();
                        return (a.width * a.height) - (b.width * b.height);
                    });
                const target = candidates[0];
                if (target) {
                    const clickable = target.closest('button') || target;
                    clickable.click();
                }
            }"""
        )
        if await self._wait_customer_entered(page, seconds=15):
            return
        await self._snapshot(page, "customer_enter_timeout")
        raise RuntimeError(f"Mogong customer enter timeout: url={page.url}")

    async def _wait_customer_entered(self, page, *, seconds: int) -> bool:
        for _ in range(seconds):
            if "/home" in page.url or "/aigc" in page.url:
                return True
            await page.wait_for_timeout(1000)
        return False

    async def _open_creative_assistant(self, page) -> None:
        try:
            await self._click_first_text(page, ("墨攻AI", "墨攻 AI"))
        except RuntimeError:
            await self._safe_goto(page, "https://usergrowth.com.cn/aigc")

        header_icons = page.locator("div[class*='task-icon']")
        icon_count = 0
        last_body_text = ""
        for _ in range(60):
            try:
                icon_count = await header_icons.count()
            except Exception:
                await page.wait_for_timeout(1000)
                continue
            if icon_count:
                break
            try:
                last_body_text = await page.locator("body").inner_text(timeout=2000)
            except Exception:
                last_body_text = ""
            if "/open/customer" in page.url or (self.customer_id in last_body_text and "客户列表" in last_body_text):
                await self._select_customer(page)
                try:
                    await self._click_first_text(page, ("墨攻AI", "墨攻 AI"))
                except RuntimeError:
                    await self._safe_goto(page, "https://usergrowth.com.cn/aigc")
                await page.wait_for_timeout(2000)
                continue
            await page.wait_for_timeout(1000)
        if icon_count == 0:
            await self._snapshot(page, "assistant_icon_not_found")
            raise RuntimeError(
                "Mogong creative assistant icon not found"
                f" url={page.url} body={last_body_text[:500]}"
            )
        await header_icons.nth(icon_count - 1).click()
        for _ in range(30):
            body_text = await page.locator("body").inner_text(timeout=3000)
            if "创意助手" in body_text and "助手能力" in body_text:
                return
            await page.wait_for_timeout(1000)
        await self._snapshot(page, "assistant_drawer_not_open")
        raise RuntimeError("Mogong creative assistant drawer did not open")

    async def _select_gid_ability(self, page) -> None:
        await self._click_first_text(page, ("助手能力", "能力"))
        await page.wait_for_timeout(500)
        await self._click_first_text(page, ("GID查询", "GID 查询"))
        for _ in range(15):
            body_text = await page.locator("body").inner_text(timeout=3000)
            if "助手能力 : GID查询" in body_text or "抖音视频GID查询" in body_text:
                return
            await page.wait_for_timeout(1000)
        await self._snapshot(page, "gid_ability_not_selected")
        raise RuntimeError("Mogong GID ability was not selected")

    async def _ask_gid(self, page, gid: str) -> str:
        input_box = await self._find_chat_input(page)
        await input_box.fill(gid)
        await self._snapshot(page, f"query_{gid}_filled")
        await input_box.press("Enter")
        await page.wait_for_timeout(1500)
        await self._snapshot(page, f"query_{gid}_sent")
        after_text = ""
        reply = ""
        got_final_reply = False
        for _ in range(90):
            await page.wait_for_timeout(1000)
            after_text = await page.locator("body").inner_text(timeout=3000)
            reply = self._extract_latest_reply(after_text, gid)
            if self._looks_like_assistant_reply(reply):
                await self._snapshot(page, f"query_{gid}_reply")
                got_final_reply = True
                break
        if not reply:
            await self._snapshot(page, f"query_{gid}_no_reply")
        elif not got_final_reply:
            await self._snapshot(page, f"query_{gid}_reply_timeout")
        return reply or after_text[-1200:].strip()

    async def _click_first_text(self, page, texts: tuple[str, ...]) -> None:
        for text in texts:
            locator = page.get_by_text(text, exact=False).first
            try:
                if await locator.count():
                    await locator.click()
                    return
            except Exception:
                continue
        raise RuntimeError(f"Could not find clickable text: {', '.join(texts)}")

    async def _safe_goto(self, page, url: str) -> None:
        last_error: Optional[Exception] = None
        for _ in range(3):
            try:
                response = await page.goto(url, wait_until="commit", timeout=self.timeout_ms)
                if await self._wait_for_meaningful_dom(page, timeout_ms=min(self.timeout_ms, 20000)):
                    return
                status = response.status if response else "no-response"
                await self._snapshot(page, "empty_dom_after_navigation")
                raise RuntimeError(
                    "Mogong page committed but did not render DOM"
                    f" url={page.url} status={status}"
                )
            except Exception as exc:
                last_error = exc
                await page.wait_for_timeout(1500)
        raise RuntimeError(f"Mogong page navigation failed: {url}: {last_error}")

    @staticmethod
    def _launch_args() -> list[str]:
        return ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"]

    async def _wait_for_meaningful_dom(self, page, *, timeout_ms: int) -> bool:
        elapsed = 0
        step = 500
        while elapsed <= timeout_ms:
            try:
                ready = await page.evaluate(
                    """() => {
                        const body = document.body;
                        const bodyText = (body?.innerText || '').trim();
                        const controls = document.querySelectorAll(
                            'input, button, textarea, [contenteditable="true"]'
                        ).length;
                        return Boolean(body && (bodyText.length > 0 || controls > 0));
                    }"""
                )
                if ready:
                    return True
            except Exception:
                pass
            await page.wait_for_timeout(step)
            elapsed += step
        return False

    async def _find_chat_input(self, page):
        placeholder_input = page.get_by_placeholder("请输入内容", exact=False).first
        if await placeholder_input.count() and await placeholder_input.is_visible():
            return placeholder_input

        textareas = page.locator("textarea")
        for index in range(await textareas.count() - 1, -1, -1):
            textarea = textareas.nth(index)
            try:
                if await textarea.is_visible() and await textarea.is_enabled():
                    return textarea
            except Exception:
                continue

        editables = page.locator("[contenteditable='true']")
        for index in range(await editables.count() - 1, -1, -1):
            editable = editables.nth(index)
            try:
                if await editable.is_visible():
                    return editable
            except Exception:
                continue

        await self._snapshot(page, "chat_input_not_found")
        raise RuntimeError("Mogong chat input not found")

    async def _snapshot(self, page, name: str) -> None:
        if not self.debug_dir:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(char if char.isalnum() or char in ("_", "-") else "_" for char in name)
        try:
            metadata = await page.evaluate(
                """() => ({
                    url: location.href,
                    title: document.title || '',
                    readyState: document.readyState,
                    bodyTextLength: (document.body?.innerText || '').trim().length,
                    htmlLength: document.documentElement?.outerHTML?.length || 0,
                    inputCount: document.querySelectorAll('input').length,
                    buttonCount: document.querySelectorAll('button').length,
                    textareaCount: document.querySelectorAll('textarea').length,
                })"""
            )
        except Exception as exc:
            metadata = {"error": str(exc)}
        try:
            body_text = await page.locator("body").inner_text(timeout=3000)
        except Exception as exc:
            body_text = f"<failed to read body: {exc}>"
        (self.debug_dir / f"{safe_name}.txt").write_text(
            f"URL: {page.url}\nMETADATA: {metadata}\n\n{body_text}",
            encoding="utf-8",
        )
        try:
            await page.screenshot(path=str(self.debug_dir / f"{safe_name}.png"), full_page=True)
        except Exception:
            pass

    @staticmethod
    def _classify_exists(reply: str) -> bool:
        normalized = reply.lower()
        if any(keyword in normalized for keyword in NEGATIVE_REPLY_KEYWORDS):
            return False
        if any(keyword in normalized for keyword in POSITIVE_REPLY_KEYWORDS):
            return True
        return False

    @staticmethod
    def _extract_latest_reply(body_text: str, gid: str) -> str:
        gid_index = body_text.rfind(gid)
        if gid_index < 0:
            return ""
        tail = body_text[gid_index + len(gid) :].strip()
        for delimiter in ("\n助手能力 :", "\n创意助手"):
            delimiter_index = tail.find(delimiter)
            if delimiter_index >= 0:
                tail = tail[:delimiter_index].strip()
        lines = []
        for line in tail.splitlines():
            text = line.strip()
            if not text:
                continue
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(:\d{2})?", text):
                continue
            lines.append(text)
        return "\n".join(lines).strip()

    @staticmethod
    def _looks_like_assistant_reply(reply: str) -> bool:
        normalized = reply.lower()
        has_final_negative = any(keyword in normalized for keyword in NEGATIVE_REPLY_KEYWORDS)
        has_final_positive = any(keyword in normalized for keyword in POSITIVE_REPLY_KEYWORDS)
        has_final_error = any(keyword in normalized for keyword in ERROR_REPLY_KEYWORDS)
        if any(keyword in normalized for keyword in PENDING_REPLY_KEYWORDS):
            return has_final_negative or has_final_positive or has_final_error
        return has_final_negative or has_final_positive or has_final_error


def is_mogong_internal_error_reply(reply: str) -> bool:
    normalized = reply.lower()
    return any(keyword in normalized for keyword in ERROR_REPLY_KEYWORDS)


def query_status_from_check(check: Optional[MogongCheckResult]) -> tuple[str, Optional[str]]:
    if not check:
        return "no_reply", "墨攻未返回查询结果"
    if check.exists:
        return "matched", None
    if is_mogong_internal_error_reply(check.reply):
        return "mogong_internal_error", "墨攻返回内部错误"
    if not check.reply.strip() or "正在处理" in check.reply:
        return "no_reply", "墨攻查询超时未返回最终结果"
    return "not_found", None


def build_result_items(references: list[VideoReference], checks: list[MogongCheckResult]) -> list[ResultItem]:
    check_map = {item.gid: item for item in checks}
    items: list[ResultItem] = []
    for reference in references:
        if not reference.gid or not reference.video_url:
            continue
        check = check_map.get(reference.gid)
        query_status, query_error = query_status_from_check(check)
        items.append(
            ResultItem(
                gid=reference.gid,
                video_url=reference.video_url,
                source_url=reference.source_url,
                query_status=query_status,
                query_error=query_error,
                mogong_reply=check.reply if check else None,
                download_status="pending" if query_status == "matched" else "skipped",
            )
        )
    return items


def build_unchecked_items(references: list[VideoReference]) -> list[ResultItem]:
    items: list[ResultItem] = []
    for reference in references:
        if not reference.gid or not reference.video_url:
            continue
        items.append(
            ResultItem(
                gid=reference.gid,
                video_url=reference.video_url,
                source_url=reference.source_url,
                query_status="unchecked",
                query_error="墨攻查询已跳过",
                download_status="skipped",
            )
        )
    return items


def write_results_xlsx(items: list[ResultItem], output_path: Path, *, matched_only: bool = False) -> Path:
    openpyxl = require_import("openpyxl", "python -m pip install openpyxl")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "过墨攻视频URL" if matched_only else "查询结果"
    if matched_only:
        sheet.append(["GID", "视频URL", "原始URL", "墨攻回复"])
        for item in items:
            if item.query_status != "matched":
                continue
            sheet.append([item.gid, item.video_url, item.source_url, item.mogong_reply or ""])
    else:
        sheet.append(["GID", "视频URL", "原始URL", "查询状态", "查询错误", "墨攻回复", "下载状态", "本地文件", "下载错误"])
        for item in items:
            sheet.append(
                [
                    item.gid,
                    item.video_url,
                    item.source_url,
                    item.query_status,
                    item.query_error or "",
                    item.mogong_reply or "",
                    item.download_status,
                    item.download_path or "",
                    item.download_error or "",
                ]
            )
    workbook.save(output_path)
    return output_path


def write_template(output_path: Path, mode: str) -> Path:
    openpyxl = require_import("openpyxl", "python -m pip install openpyxl")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    if mode == "keyword":
        sheet.title = "关键词检索"
        rows = [
            ["关键词", "备注"],
            ["美甲", "示例：按关键词搜索抖音视频，再走墨攻 GID 查询过滤"],
            ["菜谱", "每行一个关键词"],
        ]
        widths = {"A": 24, "B": 56}
    elif mode == "gid":
        sheet.title = "GID检索"
        rows = [
            ["GID", "备注"],
            ["7380000000000000001", "示例：替换为需要查询的抖音视频 GID"],
            ["7380000000000000002", "每行一个 GID"],
        ]
        widths = {"A": 28, "B": 44}
    else:
        sheet.title = "URL检索"
        rows = [
            ["视频URL", "备注"],
            ["https://www.douyin.com/video/7380000000000000001", "示例：替换为需要查询的抖音视频链接"],
            ["https://v.douyin.com/iExampleDemo/", "短链接也支持"],
        ]
        widths = {"A": 58, "B": 44}
    for row in rows:
        sheet.append(row)
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    workbook.save(output_path)
    return output_path


def write_summary_json(summary: dict, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def event_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def append_run_event(output_dir: Path, event: str, **fields: object) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    record = {"ts": event_timestamp(), "event": event}
    record.update(fields)
    with (output_dir / "run_events.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_failure_artifacts(output_dir: Path, exc: BaseException, *, interrupted: bool = False) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": event_timestamp(),
        "status": "interrupted" if interrupted else "failed",
        "error_type": type(exc).__name__,
        "error": str(exc),
        "traceback": "" if interrupted else traceback.format_exc(),
    }
    write_summary_json(payload, output_dir / "failure.json")
    append_run_event(
        output_dir,
        "interrupted" if interrupted else "failed",
        error_type=payload["error_type"],
        error=payload["error"],
    )


def write_json_dataclasses(items: Iterable[object], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path


async def build_references(args) -> tuple[list[VideoReference], int]:
    input_path = Path(args.input)
    column_by_mode = {
        "url": (args.url_column, URL_HEADER_KEYWORDS),
        "gid": (args.gid_column, GID_HEADER_KEYWORDS),
        "keyword": (args.keyword_column, KEYWORD_HEADER_KEYWORDS),
    }
    requested_column, header_keywords = column_by_mode[args.mode]
    values = read_input_values(input_path, requested_column, header_keywords)
    if not values:
        raise RuntimeError(f"No {args.mode} values found in input")

    toolkit = load_douyin_toolkit_core(args.douyin_toolkit_core)
    client = wanbang_client_from_args(args, toolkit) if args.mode == "keyword" else None
    resolve_kwargs = {
        "urls": values if args.mode == "url" else (),
        "gids": values if args.mode == "gid" else (),
        "keywords": values if args.mode == "keyword" else (),
        "client": client,
        "page": args.wanbang_page,
        "max_per_keyword": args.max_videos_per_keyword,
        "short_url_timeout": args.short_url_timeout,
    }
    toolkit_references = await asyncio.to_thread(toolkit.resolve_references, **resolve_kwargs)
    return convert_toolkit_references(toolkit_references), len(values)


async def maybe_query_mogong(args, references: list[VideoReference]) -> list[ResultItem]:
    valid_references = [item for item in references if item.gid and item.video_url]
    if args.skip_mogong:
        return build_unchecked_items(valid_references)

    account = args.mogong_account or os.getenv("MOGONG_ACCOUNT", "")
    password = args.mogong_password or os.getenv("MOGONG_PASSWORD", "")
    customer_id = args.mogong_customer_id or os.getenv("MOGONG_CUSTOMER_ID", "")
    if not account or not password or not customer_id:
        raise RuntimeError(
            "Mogong credentials are required. Provide --mogong-account, --mogong-password, "
            "--mogong-customer-id or set MOGONG_ACCOUNT, MOGONG_PASSWORD, MOGONG_CUSTOMER_ID."
        )

    debug_dir = Path(args.debug_dir) if args.debug_dir else Path(args.output_dir) / "debug"
    client = MogongCreativeAssistantClient(
        account,
        password,
        customer_id,
        headless=not args.headed,
        timeout_ms=args.page_timeout_ms,
        debug_dir=debug_dir,
    )
    checks = await asyncio.wait_for(
        client.check_gids([item.gid for item in valid_references if item.gid]),
        timeout=args.mogong_timeout_sec,
    )
    return build_result_items(valid_references, checks)


async def maybe_download(args, items: list[ResultItem]) -> None:
    if not args.download:
        return
    output_dir = Path(args.output_dir)
    toolkit = load_douyin_toolkit_core(args.douyin_toolkit_core)
    client = wanbang_client_from_args(args, toolkit)
    video_dir = Path(args.output_dir) / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    targets = [item for item in items if item.query_status == "matched"]
    if args.download_scope == "all":
        targets = [item for item in items if item.query_status in {"matched", "unchecked"}]
    for index, item in enumerate(targets, start=1):
        item.download_status = "downloading"
        try:
            target_path = video_dir / f"{item.gid}.mp4"
            if args.skip_existing_downloads and toolkit.validate_mp4_file(target_path):
                item.download_status = "reused"
            else:
                direct_url = await asyncio.to_thread(client.video_download_url, item.gid)
                await asyncio.to_thread(toolkit.download_file, direct_url, target_path)
                item.download_status = "downloaded"
            item.download_path = str(target_path)
            item.download_error = None
        except Exception as exc:
            item.download_status = "failed"
            item.download_path = None
            item.download_error = str(exc)
        print(f"download {index}/{len(targets)} {item.gid}: {item.download_status}", flush=True)
        append_run_event(
            output_dir,
            "download_item_finished",
            index=index,
            total=len(targets),
            gid=item.gid,
            status=item.download_status,
            error=item.download_error,
        )
        write_json_dataclasses(items, output_dir / "partial_results.json")


def summarize(
    *,
    mode: str,
    input_count: int,
    references: list[VideoReference],
    items: list[ResultItem],
    output_dir: Path,
) -> dict:
    return {
        "mode": mode,
        "input_count": input_count,
        "parsed_count": sum(1 for item in references if item.gid and item.video_url),
        "parse_failed_count": sum(1 for item in references if not item.gid or not item.video_url),
        "result_count": len(items),
        "matched_count": sum(1 for item in items if item.query_status == "matched"),
        "not_found_count": sum(1 for item in items if item.query_status == "not_found"),
        "query_failed_count": sum(1 for item in items if item.query_status in QUERY_FAILED_STATUSES),
        "downloaded_count": sum(1 for item in items if item.download_status in VIDEO_AVAILABLE_STATUSES),
        "failed_download_count": sum(1 for item in items if item.download_status == "failed"),
        "output_dir": str(output_dir),
        "all_results_xlsx": str(output_dir / "all_results.xlsx"),
        "matched_urls_xlsx": str(output_dir / "matched_urls.xlsx"),
        "summary_json": str(output_dir / "summary.json"),
    }


async def run_command(args) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    append_run_event(
        output_dir,
        "started",
        mode=args.mode,
        input=args.input,
        skip_mogong=args.skip_mogong,
        download=args.download,
    )
    append_run_event(output_dir, "build_references_started")
    references, input_count = await build_references(args)
    write_json_dataclasses(references, output_dir / "parsed_references.json")
    append_run_event(
        output_dir,
        "build_references_finished",
        input_count=input_count,
        reference_count=len(references),
        parsed_count=sum(1 for item in references if item.gid and item.video_url),
    )
    print(f"parsed references: {sum(1 for item in references if item.gid)}/{input_count}", flush=True)

    parse_errors = [asdict(item) for item in references if item.error_message]
    if parse_errors:
        write_summary_json({"parse_errors": parse_errors}, output_dir / "parse_errors.json")
        append_run_event(output_dir, "parse_errors_written", count=len(parse_errors))

    append_run_event(output_dir, "mogong_query_started", skipped=args.skip_mogong)
    items = await maybe_query_mogong(args, references)
    write_json_dataclasses(items, output_dir / "partial_results.json")
    append_run_event(
        output_dir,
        "mogong_query_finished",
        result_count=len(items),
        matched_count=sum(1 for item in items if item.query_status == "matched"),
        query_failed_count=sum(1 for item in items if item.query_status in QUERY_FAILED_STATUSES),
    )
    append_run_event(output_dir, "download_started", enabled=args.download)
    await maybe_download(args, items)
    append_run_event(
        output_dir,
        "download_finished",
        downloaded_count=sum(1 for item in items if item.download_status in VIDEO_AVAILABLE_STATUSES),
        failed_download_count=sum(1 for item in items if item.download_status == "failed"),
    )

    write_results_xlsx(items, output_dir / "all_results.xlsx", matched_only=False)
    write_results_xlsx(items, output_dir / "matched_urls.xlsx", matched_only=True)
    summary = summarize(mode=args.mode, input_count=input_count, references=references, items=items, output_dir=output_dir)
    write_summary_json(summary, output_dir / "summary.json")
    append_run_event(output_dir, "completed", summary_json=str(output_dir / "summary.json"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def template_command(args) -> int:
    path = write_template(Path(args.output), args.mode)
    print(str(path))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mogong GID querying, business filtering, and export using douyin-video-toolkit references."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    template = subparsers.add_parser("template", help="Write an input Excel template")
    template.add_argument("--mode", choices=("url", "gid", "keyword"), default="url")
    template.add_argument("--output", required=True)
    template.set_defaults(func=template_command)

    run = subparsers.add_parser("run", help="Use Douyin Toolkit references, run Mogong filtering, and export results")
    run.add_argument("--input", required=True, help="Input .xlsx, .xlsm, or .csv file")
    run.add_argument("--output-dir", required=True, help="Directory for all_results.xlsx, matched_urls.xlsx, and debug logs")
    run.add_argument("--mode", choices=("url", "gid", "keyword"), default="url")
    run.add_argument("--url-column", default=None, help="URL column header or 1-based column number")
    run.add_argument("--gid-column", default=None, help="GID column header or 1-based column number")
    run.add_argument("--keyword-column", default=None, help="Keyword column header or 1-based column number")
    run.add_argument("--max-videos-per-keyword", type=int, default=12)
    run.add_argument("--concurrency", type=int, default=8, help=argparse.SUPPRESS)
    run.add_argument("--douyin-toolkit-core", default=None, help="Explicit path to douyin_reference_core.py")
    run.add_argument("--short-url-timeout", type=int, default=20)
    run.add_argument("--skip-mogong", action="store_true", help="Parse/search only; mark rows as unchecked")
    run.add_argument("--mogong-account", default=None)
    run.add_argument("--mogong-password", default=None)
    run.add_argument("--mogong-customer-id", default=None)
    run.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless")
    run.add_argument("--page-timeout-ms", type=int, default=45000)
    run.add_argument("--mogong-timeout-sec", type=int, default=600)
    run.add_argument("--debug-dir", default=None)
    run.add_argument("--wanbang-key", default=None)
    run.add_argument("--wanbang-secret", default=None)
    run.add_argument("--wanbang-base-url", default=None)
    run.add_argument("--wanbang-page", type=int, default=1)
    run.add_argument("--wanbang-retry-count", type=int, default=2)
    run.add_argument("--wanbang-retry-delay-seconds", type=float, default=1.0)
    run.add_argument("--download", action="store_true", help="Delegate matched-video downloads to douyin-video-toolkit")
    run.add_argument("--download-scope", choices=("matched", "all"), default="matched")
    run.add_argument("--skip-existing-downloads", action="store_true")
    run.set_defaults(func=lambda args: asyncio.run(run_command(args)))
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt as exc:
        if hasattr(args, "output_dir"):
            write_failure_artifacts(Path(args.output_dir), exc, interrupted=True)
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        if hasattr(args, "output_dir"):
            write_failure_artifacts(Path(args.output_dir), exc, interrupted=False)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
