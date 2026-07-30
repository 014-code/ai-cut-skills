from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BASE_URL = "https://adxray.dxylds.com"
LOGIN_URL = f"{BASE_URL}/login?redirect=%2F"
RANK_URL = f"{BASE_URL}/rank/distribution"

DEFAULT_CATEGORIES = ("真人AI", "沙雕漫", "2D漫", "3D漫", "解说漫", "游戏编辑器漫")
SORT_LABELS = {
    "most_exposure": ("最多曝光", "最高曝光"),
    "most_likes": ("最多点赞", "最高点赞"),
    "most_plays": ("最多播放", "最高播放"),
}
VIDEO_BUTTON_SELECTOR = ".a6de18a4-cover-video-play-btn"
VIDEO_BUTTON_SELECTORS = (
    ".a6de18a4-cover-video-play-btn",
    "[class*='cover-video-play-btn']",
    "[class*='video-play']",
    "[class*='play-btn']",
    ".anticon-play-circle",
    ".anticon-play",
)


@dataclass
class CrawlConfig:
    account: str
    password: str
    output_dir: Path
    drama_name: str | None = None
    detail_url: str | None = None
    categories: tuple[str, ...] = DEFAULT_CATEGORIES
    sorts: tuple[str, ...] = ("most_exposure", "most_likes", "most_plays")
    max_videos_per_sort: int = 0
    headless: bool = False
    dry_run: bool = False
    timeout_ms: int = 45000
    download_timeout_ms: int = 180000
    slow_mo_ms: int = 250


class AdxrayPlayletCrawler:
    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self.debug_dir = config.output_dir / "debug"
        self.network_urls: list[str] = []

    async def run(self) -> dict[str, Any]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("需要先安装 playwright，并执行 playwright install chromium") from exc

        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self._log("run start")

        async with async_playwright() as playwright:
            browser = await self._launch_browser(playwright)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                accept_downloads=True,
                locale="zh-CN",
            )
            page = await context.new_page()
            page.set_default_timeout(self.config.timeout_ms)
            page.on("response", lambda response: self._record_network_url(response.url))

            try:
                await self._login(page)
                detail_info = await self._open_target_playlet(page)
                detail_page = detail_info["page"]
                drama_title = await self._read_drama_title(detail_page, detail_info)
                manifest = await self._crawl_detail(detail_page, context, detail_info, drama_title)
                write_json(self.config.output_dir / "manifest.json", manifest)
                self._log(f"manifest written: {self.config.output_dir / 'manifest.json'}")
                return manifest
            except Exception as exc:
                await self._snapshot_error(page, "crawler_failed", exc)
                raise
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def _launch_browser(self, playwright):
        last_error: Exception | None = None
        for channel in ("msedge", "chrome", None):
            try:
                if channel:
                    return await playwright.chromium.launch(
                        channel=channel,
                        headless=self.config.headless,
                        slow_mo=self.config.slow_mo_ms,
                    )
                return await playwright.chromium.launch(
                    headless=self.config.headless,
                    slow_mo=self.config.slow_mo_ms,
                )
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"启动浏览器失败: {last_error}")

    async def _login(self, page) -> None:
        await self._safe_goto(page, LOGIN_URL)
        if await self._wait_login_ready(page, timeout_ms=5000):
            self._log("already logged in")
            return

        for attempt in range(1, 4):
            self._log(f"login attempt {attempt}")
            await self._fill_first(
                page,
                (
                    "input[name='username']",
                    "input[placeholder='请输入用户名']",
                    "input[type='text']",
                ),
                self.config.account,
            )
            await self._fill_first(
                page,
                (
                    "input[name='password']",
                    "input[placeholder='请输入密码']",
                    "input[type='password']",
                ),
                self.config.password,
            )
            await self._fill_captcha_if_present(page)
            await self._accept_agreement_if_present(page)
            await self._click_login_button(page)
            if await self._wait_login_ready(page, timeout_ms=15000):
                self._log("login success")
                return
            await self._snapshot_error(page, f"login_attempt_{attempt}")

        raise RuntimeError("登录失败，未进入 AdXRay 首页")

    async def _open_target_playlet(self, page) -> dict[str, Any]:
        if self.config.detail_url:
            detail_url = absolute_url(self.config.detail_url, base_url=BASE_URL)
            await self._safe_goto(page, detail_url)
            await self._wait_for_page_text(page, ("素材筛选", "热门文案", "投放趋势"), timeout_ms=30000)
            return {
                "page": page,
                "source": "detail_url",
                "href": detail_url,
                "rank_entry": None,
                "selected_categories": list(self.config.categories),
            }

        await self._open_rank_page(page)
        await self._apply_categories(page)
        if self.config.drama_name:
            await self._search_drama(page, self.config.drama_name)
        rank_entries = await self._extract_rank_entries(page)
        if not rank_entries:
            await self._snapshot_error(page, "no_rank_entries")
            raise RuntimeError("未找到热播榜剧名链接")

        selected = select_rank_entry(rank_entries, self.config.drama_name)
        self._log(f"selected playlet: {selected.get('title')} -> {selected.get('href')}")
        await self._safe_goto(page, selected["href"])
        await self._wait_for_page_text(page, ("素材筛选", "热门文案", "投放趋势"), timeout_ms=30000)
        return {
            "page": page,
            "source": "rank_distribution",
            "href": selected["href"],
            "rank_entry": selected,
            "selected_categories": list(self.config.categories),
        }

    async def _open_rank_page(self, page) -> None:
        await self._safe_goto(page, RANK_URL)
        if await self._page_has_text(page, ("抖音热播榜", "短剧标签", "剧名"), timeout_ms=15000):
            return
        if await self._click_text_optional(page, "抖音热播榜"):
            await page.wait_for_timeout(1500)
        if not await self._page_has_text(page, ("抖音热播榜", "短剧标签", "剧名"), timeout_ms=15000):
            await self._snapshot_error(page, "rank_page_not_ready")
            raise RuntimeError("未进入抖音热播榜页面")

    async def _apply_categories(self, page) -> None:
        if not self.config.categories:
            return
        await self._expand_until_texts_visible(page, self.config.categories, max_rounds=3)
        clicked: list[str] = []
        missing: list[str] = []
        for category in self.config.categories:
            if await self._click_exact_visible_text(page, category):
                clicked.append(category)
                await page.wait_for_timeout(250)
            else:
                missing.append(category)
        self._log(f"category clicked={clicked} missing={missing}")
        if clicked:
            if not await self._click_text_optional(page, "确定"):
                await self._click_text_optional(page, "确认")
            await page.wait_for_timeout(2500)
        await self._snapshot(page, "rank_after_categories", screenshot=True)

    async def _search_drama(self, page, drama_name: str) -> None:
        locator = await self._first_existing(
            page,
            (
                "input[placeholder*='搜索产品']",
                "input[placeholder*='短剧']",
                "input[placeholder*='剧名']",
                "input[type='text']",
            ),
        )
        if not locator:
            self._log("drama search input not found; will choose from current rank entries")
            return
        await locator.fill(drama_name, timeout=5000)
        await locator.press("Enter")
        await page.wait_for_timeout(800)
        await self._click_text_optional(page, "搜索")
        await page.wait_for_timeout(2500)
        await self._snapshot(page, "rank_after_search", screenshot=True)

    async def _extract_rank_entries(self, page) -> list[dict[str, Any]]:
        items = await page.evaluate(
            """
            () => {
              const out = [];
              const seen = new Set();
              for (const a of Array.from(document.querySelectorAll('a'))) {
                const href = a.href || a.getAttribute('href') || '';
                const text = (a.innerText || a.textContent || '').trim();
                if (!href.includes('/playlet/') || !text) continue;
                if (seen.has(href)) continue;
                seen.add(href);
                let row = a.closest('tr,[role=row],.a6de18a4-table-row,.css-1lrq4bq,div');
                out.push({
                  title: text,
                  href,
                  row_text: row ? (row.innerText || row.textContent || '').trim() : text
                });
              }
              return out;
            }
            """
        )
        return items

    async def _read_drama_title(self, page, detail_info: dict[str, Any]) -> str:
        rank_entry = detail_info.get("rank_entry") or {}
        title = clean_text(rank_entry.get("title") or "")
        if title:
            return title
        body = await self._body_text(page, timeout_ms=3000)
        match = re.search(r"《[^》]{2,80}》", body)
        if match:
            return match.group(0)
        parsed = urlparse(detail_info.get("href") or page.url)
        return f"playlet_{Path(parsed.path).name or 'detail'}"

    async def _crawl_detail(self, page, context, detail_info: dict[str, Any], drama_title: str) -> dict[str, Any]:
        await self._ensure_material_tab(page)
        slug = safe_stem(drama_title, fallback="playlet")
        manifest: dict[str, Any] = {
            "source": "adxray_douyin_hot_playlet",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "dry_run": self.config.dry_run,
            "detail_url": detail_info.get("href") or page.url,
            "drama_title": drama_title,
            "rank_entry": detail_info.get("rank_entry"),
            "selected_categories": detail_info.get("selected_categories", []),
            "sorts": [],
            "items": [],
        }

        for sort_key in self.config.sorts:
            labels = SORT_LABELS.get(sort_key)
            if not labels:
                self._log(f"skip unsupported sort: {sort_key}")
                continue
            sort_result = {
                "sort": sort_key,
                "labels": list(labels),
                "status": "pending",
                "items": [],
            }
            manifest["sorts"].append(sort_result)
            try:
                clicked_label = await self._select_sort(page, labels)
                sort_result["clicked_label"] = clicked_label
                await self._snapshot(page, f"detail_after_sort_{sort_key}", screenshot=True)
                items = await self._collect_sort_videos(page, context, drama_title, slug, sort_key)
                sort_result["status"] = "ok"
                sort_result["count"] = len(items)
                sort_result["items"] = items
                manifest["items"].extend(items)
            except Exception as exc:
                sort_result["status"] = "failed"
                sort_result["error"] = str(exc)
                await self._snapshot_error(page, f"sort_failed_{sort_key}", exc)
            finally:
                await self._close_modal(page)

        return manifest

    async def _ensure_material_tab(self, page) -> None:
        if "/playlet/" not in page.url:
            raise RuntimeError(f"当前不在短剧详情页，拒绝继续抓取素材: {page.url}")
        await page.wait_for_timeout(1200)
        await self._wait_for_page_text(page, ("素材筛选", "AI相似去重", "排序方式"), timeout_ms=30000)

    async def _select_sort(self, page, labels: tuple[str, ...]) -> str:
        for _ in range(3):
            for label in labels:
                if await self._click_exact_visible_text(page, label):
                    self._log(f"sort clicked: {label}")
                    await page.wait_for_timeout(2500)
                    if "/playlet/" not in page.url:
                        raise RuntimeError(f"排序点击后离开了短剧详情页: {page.url}")
                    return label
            await self._expand_more_buttons(page, selector=".a6de18a4-selector-operation-button")
        for label in labels:
            if await self._click_text_optional(page, label):
                self._log(f"sort clicked by fallback: {label}")
                await page.wait_for_timeout(2500)
                if "/playlet/" not in page.url:
                    raise RuntimeError(f"排序点击后离开了短剧详情页: {page.url}")
                return label
        raise RuntimeError(f"未找到排序方式: {'/'.join(labels)}")

    async def _collect_sort_videos(self, page, context, drama_title: str, slug: str, sort_key: str) -> list[dict[str, Any]]:
        await self._wait_for_video_buttons(page)
        buttons = await self._video_buttons(page)
        total = len(buttons)
        if total <= 0:
            raise RuntimeError("当前排序下没有找到素材播放按钮")
        limit = total if self.config.max_videos_per_sort <= 0 else min(total, self.config.max_videos_per_sort)
        self._log(f"{sort_key}: video buttons total={total} limit={limit}")
        output: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        for index in range(limit):
            await self._close_modal(page)
            buttons = await self._video_buttons(page)
            if index >= len(buttons):
                break
            button = buttons[index]
            card_text = await self._card_text_for_button(button)
            row: dict[str, Any] = {
                "sort": sort_key,
                "index": index + 1,
                "drama_title": drama_title,
                "card_text": card_text,
            }
            try:
                if not await self._click_locator_center(page, button):
                    await button.click(force=True, timeout=5000)
                url = await self._wait_video_src(page)
                row["url"] = url
                if url in seen_urls:
                    row["status"] = "duplicate_in_sort"
                    output.append(row)
                    continue
                seen_urls.add(url)
                filename = f"{slug}_{sort_key}_{index + 1:02d}.mp4"
                target = self.config.output_dir / filename
                row["file"] = str(target)
                if self.config.dry_run:
                    row["status"] = "ready"
                else:
                    await self._download_url(context, url, target, referer=page.url, row=row)
                output.append(row)
            except Exception as exc:
                row["status"] = "failed"
                row["error"] = str(exc)
                output.append(row)
                await self._snapshot_error(page, f"video_failed_{sort_key}_{index + 1}", exc)
            finally:
                await self._close_modal(page)
        return output

    async def _wait_for_video_buttons(self, page) -> None:
        deadline = asyncio.get_running_loop().time() + self.config.timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self._video_buttons(page):
                    return
            except Exception:
                pass
            await page.wait_for_timeout(800)
        raise RuntimeError("等待素材播放按钮超时")

    async def _fill_captcha_if_present(self, page) -> None:
        image = page.locator("img[alt='code']").first
        try:
            if not await image.count() or not await image.is_visible():
                return
            data = await image.screenshot(timeout=5000)
        except Exception:
            return
        try:
            import ddddocr

            recognizer = ddddocr.DdddOcr(beta=False, show_ad=False)
            code = recognizer.classification(data)
        except Exception as exc:
            self._log(f"captcha OCR failed: {exc}")
            return
        code = re.sub(r"[^0-9A-Za-z]", "", code or "")
        if not code:
            self._log("captcha OCR returned empty text")
            return
        text_inputs = await self._visible_locators(page.locator("input[type='text']"), limit=10)
        if len(text_inputs) < 2:
            return
        await text_inputs[-1].fill(code[:8], timeout=5000)
        self._log("captcha filled by OCR")

    async def _accept_agreement_if_present(self, page) -> None:
        checkbox = page.locator("input[type='checkbox']").first
        try:
            if not await checkbox.count() or not await checkbox.is_visible():
                return
            if await checkbox.is_checked():
                return
            await checkbox.check(force=True, timeout=5000)
        except Exception:
            try:
                box = await checkbox.bounding_box(timeout=1000)
                if box:
                    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            except Exception:
                pass

    async def _click_login_button(self, page) -> None:
        locators = (
            page.locator("button.btn-login").first,
            page.locator("button.login").first,
            page.locator("button:has-text('登录')").first,
            page.locator("button:has-text('登 录')").first,
        )
        for locator in locators:
            if await self._click_locator_center(page, locator):
                return
            if await self._click_locator(locator):
                return
        if await self._click_text_optional(page, "登 录"):
            return
        await self._click_text(page, "登录")

    async def _video_buttons(self, page) -> list[Any]:
        buttons: list[Any] = []
        seen: set[str] = set()
        for selector in VIDEO_BUTTON_SELECTORS:
            for item in await self._visible_locators(page.locator(selector), limit=80):
                key = await self._locator_key(item)
                if key in seen:
                    continue
                seen.add(key)
                buttons.append(item)
        if buttons:
            return buttons

        play_like = page.locator("div,span,button,i").filter(has_text=re.compile(r"^$"))
        for item in await self._visible_locators(play_like, limit=200):
            try:
                box = await item.bounding_box(timeout=1000)
                if not box or box["width"] < 18 or box["height"] < 18:
                    continue
                classes = await item.evaluate("(el) => el.className || ''")
                style = await item.evaluate("(el) => getComputedStyle(el).cssText || ''")
                if "play" not in str(classes).lower() and "triangle" not in str(style).lower():
                    continue
                key = await self._locator_key(item)
                if key in seen:
                    continue
                seen.add(key)
                buttons.append(item)
            except Exception:
                continue
        return buttons

    async def _locator_key(self, locator) -> str:
        try:
            return await locator.evaluate(
                """
                (el) => {
                  const rect = el.getBoundingClientRect();
                  return [
                    Math.round(rect.left),
                    Math.round(rect.top),
                    Math.round(rect.width),
                    Math.round(rect.height),
                    el.className || '',
                    el.tagName || ''
                  ].join('|');
                }
                """
            )
        except Exception:
            return str(id(locator))

    async def _card_text_for_button(self, button) -> str:
        try:
            text = await button.evaluate(
                """
                (el) => {
                  let node = el;
                  for (let i = 0; i < 8 && node; i++) {
                    const text = (node.innerText || node.textContent || '').trim();
                    if (text.length > 40) return text.replace(/\\s+/g, ' ').slice(0, 800);
                    node = node.parentElement;
                  }
                  return '';
                }
                """
            )
            return clean_text(text)
        except Exception:
            return ""

    async def _wait_video_src(self, page) -> str:
        start_seen = len(self.network_urls)
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            urls = await page.evaluate(
                """
                () => Array.from(document.querySelectorAll('video'))
                  .map(v => v.currentSrc || v.src || v.getAttribute('src') || '')
                  .filter(Boolean)
                """
            )
            for url in urls:
                if is_video_url(url):
                    return url
            for url in reversed(self.network_urls[start_seen:]):
                if is_video_url(url):
                    return url
            await page.wait_for_timeout(500)
        raise RuntimeError("播放弹窗未出现视频地址")

    async def _download_url(self, context, url: str, target: Path, *, referer: str, row: dict[str, Any]) -> None:
        self._log(f"downloading: {url}")
        response = await context.request.get(
            url,
            headers={"Referer": referer},
            timeout=self.config.download_timeout_ms,
        )
        if not response.ok:
            row["status"] = "failed"
            row["error"] = f"HTTP {response.status}"
            return
        body = await response.body()
        target.write_bytes(body)
        row["status"] = "downloaded"
        row["bytes"] = target.stat().st_size
        try:
            row["ffprobe"] = media_info(target)
        except Exception as exc:
            row["ffprobe_error"] = str(exc)

    async def _expand_until_texts_visible(self, page, texts: tuple[str, ...], *, max_rounds: int) -> None:
        for _ in range(max_rounds):
            body = await self._body_text(page, timeout_ms=2000)
            if all(text in body for text in texts):
                return
            await self._expand_more_buttons(page, selector=".a6de18a4-selector-operation-button")
            await page.wait_for_timeout(800)

    async def _expand_more_buttons(self, page, *, selector: str = "button") -> int:
        locator = page.locator(f"{selector}:has-text('更多')")
        count = min(await locator.count(), 10)
        clicked = 0
        for index in range(count):
            item = locator.nth(index)
            if await self._click_locator(item):
                clicked += 1
                await page.wait_for_timeout(250)
        if clicked:
            self._log(f"clicked 更多 buttons: {clicked}")
        return clicked

    async def _close_modal(self, page) -> None:
        close_locators = (
            page.locator(".a6de18a4-modal-close").last,
            page.locator("button:has-text('关闭')").last,
            page.locator("button:has-text('取消')").last,
        )
        for locator in close_locators:
            if await self._click_locator(locator):
                await page.wait_for_timeout(400)
                return
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            pass

    async def _wait_login_ready(self, page, *, timeout_ms: int) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            if await self._looks_logged_in(page):
                return True
            await page.wait_for_timeout(500)
        return False

    async def _looks_logged_in(self, page) -> bool:
        body = await self._body_text(page, timeout_ms=1500)
        if "请输入用户名" in body and "请输入密码" in body:
            return False
        return any(
            signal in body
            for signal in (
                "抖音热播榜",
                "工作台",
                "我的素材库",
                "应用中心",
            )
        )

    async def _safe_goto(self, page, url: str) -> None:
        last_error: Exception | None = None
        for _ in range(3):
            try:
                await page.goto(url, wait_until="domcontentloaded")
                return
            except Exception as exc:
                last_error = exc
                await page.wait_for_timeout(2000)
        raise RuntimeError(f"页面打开失败: {url}: {last_error}")

    async def _click_text(self, page, text: str) -> None:
        if await self._click_text_optional(page, text):
            return
        raise RuntimeError(f"未找到可点击文本: {text}")

    async def _click_text_optional(self, page, text: str) -> bool:
        locators = (
            page.get_by_text(text, exact=True).first,
            page.get_by_text(text, exact=False).first,
            page.locator(f"button:has-text('{css_text(text)}')").first,
            page.locator(f"a:has-text('{css_text(text)}')").first,
            page.locator(f"span:has-text('{css_text(text)}')").first,
        )
        for locator in locators:
            if await self._click_locator(locator):
                return True
        return False

    async def _click_exact_visible_text(self, page, text: str) -> bool:
        locators = (
            page.get_by_text(text, exact=True),
            page.locator(f"button:has-text('{css_text(text)}')"),
            page.locator(f"span:has-text('{css_text(text)}')"),
            page.locator(f"div:has-text('{css_text(text)}')"),
        )
        for locator in locators:
            visible = await self._visible_locators(locator, limit=30)
            for item in visible:
                item_text = clean_text(await self._locator_text(item, timeout_ms=1500))
                if item_text == text or item_text.startswith(f"{text} "):
                    if await self._click_locator_center(page, item):
                        return True
                    if await self._click_locator(item):
                        return True
        return False

    async def _click_locator(self, locator) -> bool:
        try:
            if not await locator.count() or not await locator.is_visible():
                return False
            await locator.scroll_into_view_if_needed(timeout=3000)
            await locator.click(force=True, timeout=5000)
            return True
        except Exception:
            return False

    async def _click_locator_center(self, page, locator) -> bool:
        try:
            if not await locator.count() or not await locator.is_visible():
                return False
            await locator.scroll_into_view_if_needed(timeout=3000)
            box = await locator.bounding_box(timeout=3000)
            if not box:
                return False
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            return True
        except Exception:
            return False

    async def _fill_first(self, page, selectors: tuple[str, ...], value: str) -> None:
        locator = await self._first_existing(page, selectors)
        if not locator:
            raise RuntimeError(f"未找到输入框: {selectors}")
        await locator.fill(value, timeout=5000)

    async def _first_existing(self, page, selectors: tuple[str, ...]):
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    async def _visible_locators(self, locator, *, limit: int = 40) -> list[Any]:
        try:
            count = min(await locator.count(), limit)
        except Exception:
            return []
        output = []
        for index in range(count):
            item = locator.nth(index)
            try:
                if await item.is_visible():
                    output.append(item)
            except Exception:
                continue
        return output

    async def _locator_text(self, locator, timeout_ms: int = 3000) -> str:
        try:
            return await locator.inner_text(timeout=timeout_ms)
        except Exception:
            return ""

    async def _body_text(self, page, timeout_ms: int = 3000) -> str:
        try:
            return await page.locator("body").inner_text(timeout=timeout_ms)
        except Exception:
            return ""

    async def _wait_for_page_text(self, page, texts: tuple[str, ...], *, timeout_ms: int = 30000) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            body = await self._body_text(page, timeout_ms=2000)
            if any(text in body for text in texts):
                return
            await page.wait_for_timeout(800)
        raise RuntimeError(f"页面未出现预期内容: {', '.join(texts)}")

    async def _page_has_text(self, page, texts: tuple[str, ...], *, timeout_ms: int = 3000) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            body = await self._body_text(page, timeout_ms=1500)
            if any(text in body for text in texts):
                return True
            await page.wait_for_timeout(500)
        return False

    async def _snapshot(self, page, name: str, *, screenshot: bool = False) -> None:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        safe_name = safe_stem(name, fallback="snapshot")
        try:
            body = await page.locator("body").inner_text(timeout=3000)
        except Exception as body_exc:
            body = f"<read body failed: {body_exc}>"
        (self.debug_dir / f"{safe_name}.txt").write_text(
            f"URL: {getattr(page, 'url', '')}\n\n{body}",
            encoding="utf-8",
        )
        if screenshot:
            try:
                await page.screenshot(path=str(self.debug_dir / f"{safe_name}.png"), full_page=True)
            except Exception:
                pass

    async def _snapshot_error(self, page, name: str, exc: BaseException | None = None) -> None:
        await self._snapshot(page, name, screenshot=True)
        if exc is not None:
            self._log(
                f"ERROR snapshot={safe_stem(name, fallback='snapshot')}\n"
                f"type={type(exc).__name__}\n"
                f"message={exc}\n"
                + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            )

    def _record_network_url(self, url: str) -> None:
        lower = url.lower()
        if "adxray" in lower or "adxvideo" in lower or ".mp4" in lower or ".m3u8" in lower:
            self.network_urls.append(url)

    def _log(self, message: str) -> None:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().isoformat(timespec="seconds")
        with (self.debug_dir / "run.log").open("a", encoding="utf-8") as fp:
            fp.write(f"[{timestamp}] {message}\n")
        print(message, flush=True)


def select_rank_entry(entries: list[dict[str, Any]], drama_name: str | None) -> dict[str, Any]:
    if not drama_name:
        return entries[0]
    normalized = normalize_title(drama_name)
    for entry in entries:
        title = normalize_title(entry.get("title") or "")
        row_text = normalize_title(entry.get("row_text") or "")
        if normalized == title or normalized in title or normalized in row_text:
            return entry
    return entries[0]


def is_video_url(url: str) -> bool:
    lower = url.lower()
    return (".mp4" in lower or ".m3u8" in lower) and "adxvideo" in lower


def absolute_url(url: str, *, base_url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return base_url.rstrip("/") + url
    return base_url.rstrip("/") + "/" + url


def safe_stem(value: str, *, fallback: str = "item") -> str:
    value = normalize_title(value)
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", value)
    value = re.sub(r"\s+", "_", value).strip("._ ")
    return value[:80] or fallback


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_title(value: str) -> str:
    value = clean_text(value)
    value = value.replace("《", "").replace("》", "")
    return value


def css_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def media_info(path: Path) -> dict[str, Any]:
    ffprobe = find_ffprobe()
    if not ffprobe:
        return {"available": False, "reason": "ffprobe not found"}
    cmd = [
        str(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"ffprobe failed with {completed.returncode}")
    info = json.loads(completed.stdout or "{}")
    info["available"] = True
    return info


def find_ffprobe() -> Path | None:
    path_value = shutil.which("ffprobe")
    if path_value:
        return Path(path_value)
    candidates = (
        Path.cwd() / "material_remix_desktop_source" / "bin" / "ffprobe.exe",
        Path.cwd() / "bin" / "ffprobe.exe",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def parse_multi(values: list[str] | None, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        return default
    output: list[str] = []
    for value in values:
        for part in re.split(r"[,/，、]+", value):
            item = part.strip()
            if item:
                output.append(item)
    return tuple(output)


def normalize_sort_key(value: str) -> str:
    aliases = {
        "highest_exposure": "most_exposure",
        "highest_likes": "most_likes",
        "highest_plays": "most_plays",
        "最多曝光": "most_exposure",
        "最高曝光": "most_exposure",
        "最多点赞": "most_likes",
        "最高点赞": "most_likes",
        "最多播放": "most_plays",
        "最高播放": "most_plays",
    }
    return aliases.get(value, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl and download AdXRay Douyin hot playlet videos.")
    parser.add_argument("--account", default=os.getenv("ADXRAY_ACCOUNT"), help="AdXRay account. Defaults to ADXRAY_ACCOUNT.")
    parser.add_argument("--password", default=os.getenv("ADXRAY_PASSWORD"), help="AdXRay password. Defaults to ADXRAY_PASSWORD.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory for videos, manifest.json, and debug snapshots.")
    parser.add_argument("--drama-name", help="Optional playlet name to search; omitted means rank #1.")
    parser.add_argument("--detail-url", help="Open a known /playlet/<id> detail URL directly.")
    parser.add_argument("--categories", nargs="*", help="Short-drama categories. Defaults to 真人AI/沙雕漫/2D漫/3D漫/解说漫/游戏编辑器漫.")
    parser.add_argument("--sorts", nargs="*", help="Sort keys or labels. Defaults to most_exposure most_likes most_plays.")
    parser.add_argument("--max-videos-per-sort", type=int, default=0, help="0 means all visible first-page videos.")
    parser.add_argument("--dry-run", action="store_true", help="Extract video URLs and write manifest without downloading bytes.")
    parser.add_argument("--headless", action="store_true", help="Run browser headlessly after selectors are validated.")
    parser.add_argument("--timeout-ms", type=int, default=45000)
    parser.add_argument("--download-timeout-ms", type=int, default=180000)
    parser.add_argument("--slow-mo-ms", type=int, default=250)
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> CrawlConfig:
    if not args.account or not args.password:
        raise SystemExit("缺少账号密码，请传 --account/--password 或设置 ADXRAY_ACCOUNT/ADXRAY_PASSWORD")
    sorts = tuple(normalize_sort_key(item) for item in parse_multi(args.sorts, default=("most_exposure", "most_likes", "most_plays")))
    categories = parse_multi(args.categories, default=DEFAULT_CATEGORIES)
    return CrawlConfig(
        account=args.account,
        password=args.password,
        output_dir=args.output_dir,
        drama_name=args.drama_name,
        detail_url=args.detail_url,
        categories=categories,
        sorts=sorts,
        max_videos_per_sort=args.max_videos_per_sort,
        dry_run=args.dry_run,
        headless=args.headless,
        timeout_ms=args.timeout_ms,
        download_timeout_ms=args.download_timeout_ms,
        slow_mo_ms=args.slow_mo_ms,
    )


async def async_main() -> int:
    config = build_config(parse_args())
    crawler = AdxrayPlayletCrawler(config)
    manifest = await crawler.run()
    downloaded = sum(1 for item in manifest.get("items", []) if item.get("status") == "downloaded")
    ready = sum(1 for item in manifest.get("items", []) if item.get("status") == "ready")
    failed = sum(1 for item in manifest.get("items", []) if item.get("status") == "failed")
    print(f"done downloaded={downloaded} ready={ready} failed={failed}")
    return 1 if failed else 0


def main() -> None:
    if sys.platform.startswith("win"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
