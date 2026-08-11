from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from openpyxl import load_workbook

from .usergrowth_browser import HOME_URL, UserGrowthBrowserClient


ProgressCallback = Callable[[str], None]
CheckpointCallback = Callable[[dict], None]
CID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
MAX_SEARCH_CHUNK_SIZE = 50


@dataclass
class TomatoMusicTagBatch:
    bid: str
    tag: str
    cids: list[str]
    song_names: list[str] = field(default_factory=list)


@dataclass
class TomatoMusicChunkResult:
    bid: str
    tag: str
    chunk_index: int
    requested_cids: list[str]
    matched_count: int = 0
    task_id: str = ""
    total: int = 0
    success: int = 0
    failed: int = 0
    status: str = "pending"
    message: str = ""


def normalise_bid(value: object) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("bid_"):
        text = text[4:]
    return text


def tag_for_bid(value: object) -> str:
    bid = normalise_bid(value)
    return f"bid_{bid}" if bid else ""


def normalise_cids(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for token in re.split(r"[\s,，;；]+", str(value or "").strip()):
            cid = token.strip().lower()
            if not CID_PATTERN.fullmatch(cid) or cid in seen:
                continue
            seen.add(cid)
            result.append(cid)
    return result


def load_tomato_music_batches(path: Path) -> list[TomatoMusicTagBatch]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _load_json_batches(path)
    if suffix in {".xlsx", ".xlsm"}:
        return _load_excel_batches(path)
    raise RuntimeError(f"不支持的番茄音乐打标输入文件：{path.suffix}")


def _load_json_batches(path: Path) -> list[TomatoMusicTagBatch]:
    with path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    rows = payload.get("batches") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise RuntimeError("JSON 必须是 batches 数组或包含 batches 的对象。")
    batches: list[TomatoMusicTagBatch] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bid = normalise_bid(row.get("bid") or row.get("bookid"))
        cids = normalise_cids(row.get("cids") or [row.get("cid")])
        if not bid or not cids:
            continue
        song_names = [
            str(item).strip()
            for item in (row.get("songNames") or row.get("song_names") or [])
            if str(item).strip()
        ]
        batches.append(
            TomatoMusicTagBatch(
                bid=bid,
                tag=str(row.get("tag") or tag_for_bid(bid)).strip(),
                cids=cids,
                song_names=song_names,
            )
        )
    if not batches:
        raise RuntimeError("输入 JSON 中没有有效的 BID/CID 批次。")
    return batches


def _compact_header(value: object) -> str:
    return re.sub(r"[\s_\-（）()【】\[\]]+", "", str(value or "").strip().lower())


def _load_excel_batches(path: Path) -> list[TomatoMusicTagBatch]:
    # Some exported Tomato Music workbooks expose an incomplete dimension in
    # their XML.  openpyxl's read-only iterator then yields only column A even
    # though the remaining cells are present.  Normal mode reads the actual
    # cell records and still keeps this path read-only from the user's point of
    # view (we never save the workbook).
    workbook = load_workbook(path, read_only=False, data_only=True)
    grouped: dict[str, dict[str, object]] = {}
    bid_headers = {"bid", "bookid", "书籍id", "小说id"}
    cid_headers = {"cid", "素材cid", "creativeid", "素材id"}
    song_headers = {"歌名", "歌曲名", "songname", "song"}
    try:
        for sheet in workbook.worksheets:
            header_row = None
            header_map: dict[str, int] = {}
            for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if row_index > 30:
                    break
                compacted = [_compact_header(value) for value in row]
                bid_index = next((i for i, value in enumerate(compacted) if value in bid_headers), None)
                cid_index = next((i for i, value in enumerate(compacted) if value in cid_headers), None)
                if bid_index is None or cid_index is None:
                    continue
                header_row = row_index
                header_map = {"bid": bid_index, "cid": cid_index}
                song_index = next((i for i, value in enumerate(compacted) if value in song_headers), None)
                if song_index is not None:
                    header_map["song"] = song_index
                break
            if header_row is None:
                continue

            last_bid = ""
            for row in sheet.iter_rows(min_row=header_row + 1, values_only=True):
                bid_value = row[header_map["bid"]] if header_map["bid"] < len(row) else ""
                bid = normalise_bid(bid_value) or last_bid
                cid_value = row[header_map["cid"]] if header_map["cid"] < len(row) else ""
                cids = normalise_cids([cid_value])
                if not bid or not cids:
                    continue
                last_bid = bid
                item = grouped.setdefault(bid, {"cids": [], "songs": []})
                for cid in cids:
                    if cid not in item["cids"]:
                        item["cids"].append(cid)
                song_index = header_map.get("song")
                if song_index is not None and song_index < len(row):
                    song = str(row[song_index] or "").strip()
                    if song and song not in item["songs"]:
                        item["songs"].append(song)
    finally:
        workbook.close()

    batches = [
        TomatoMusicTagBatch(
            bid=bid,
            tag=tag_for_bid(bid),
            cids=list(values["cids"]),
            song_names=list(values["songs"]),
        )
        for bid, values in grouped.items()
        if values["cids"]
    ]
    if not batches:
        raise RuntimeError("Excel 的所有 sheet 中都没有同时找到有效的 BID 和 CID 列。")
    return batches


def split_cids(cids: list[str], chunk_size: int) -> list[list[str]]:
    size = max(1, min(int(chunk_size or MAX_SEARCH_CHUNK_SIZE), MAX_SEARCH_CHUNK_SIZE))
    return [cids[index:index + size] for index in range(0, len(cids), size)]


def serialise_results(results: list[TomatoMusicChunkResult]) -> list[dict]:
    return [asdict(item) for item in results]


class TomatoMusicTaggingClient(UserGrowthBrowserClient):
    """复用 UserGrowth 登录、全选和操作任务验收能力，为番茄音乐 CID 批量追加 BID 标签。"""

    async def run_tagging(
            self,
            batches: list[TomatoMusicTagBatch],
            *,
            customer_id: str = "",
            material_url: str = "",
            chunk_size: int = MAX_SEARCH_CHUNK_SIZE,
            progress: ProgressCallback | None = None,
            checkpoint: CheckpointCallback | None = None,
    ) -> list[TomatoMusicChunkResult]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("需要先安装 playwright，并执行 playwright install chromium") from exc

        results: list[TomatoMusicChunkResult] = []
        async with async_playwright() as playwright:
            browser = await self._launch_browser(playwright)
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            page = await context.new_page()
            self._wrap_page_speed(page)
            page.set_default_timeout(self.timeout_ms)
            page.set_default_navigation_timeout(self.timeout_ms)
            try:
                await self._login(page, progress)
                await self._enable_post_login_resource_blocking(context, progress)
                page = await self._open_tomato_material_page(
                    page,
                    customer_id=customer_id,
                    material_url=material_url,
                    progress=progress,
                )
                base_url = self._material_search_base_url(page.url)
                for batch_index, batch in enumerate(batches, start=1):
                    chunks = split_cids(batch.cids, chunk_size)
                    self._emit(
                        progress,
                        f"番茄音乐 BID {batch.bid}：{len(batch.cids)} 个 CID，拆成 {len(chunks)} 组搜索",
                    )
                    for chunk_index, cids in enumerate(chunks, start=1):
                        self._raise_if_cancelled()
                        result = TomatoMusicChunkResult(
                            bid=batch.bid,
                            tag=batch.tag or tag_for_bid(batch.bid),
                            chunk_index=chunk_index,
                            requested_cids=list(cids),
                        )
                        results.append(result)
                        try:
                            page = await self._tag_one_cid_chunk(
                                page,
                                base_url=base_url,
                                cids=cids,
                                tag=result.tag,
                                progress=progress,
                                result=result,
                            )
                        except Exception as exc:
                            result.status = "failed"
                            result.message = str(exc)
                            if checkpoint:
                                checkpoint({"results": serialise_results(results)})
                            raise
                        if checkpoint:
                            checkpoint({"results": serialise_results(results)})
                    self._emit(progress, f"番茄音乐 BID {batch.bid} 已完成全部可检索素材打标")
                return results
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def _open_tomato_material_page(
            self,
            page,
            *,
            customer_id: str,
            material_url: str,
            progress: ProgressCallback | None,
    ):
        if material_url:
            await self._safe_goto(page, material_url)
            await page.wait_for_timeout(2500)
            await self._wait_tomato_material_page_ready(page)
            return page

        await self._safe_goto(page, HOME_URL)
        await page.wait_for_timeout(2500)
        if customer_id:
            await self._select_customer(page, customer_id, progress)

        await self._wait_for_page_text(page, ("墨攻AI",), timeout_ms=None, raise_on_timeout=True)
        await self._click_text(page, "墨攻AI")
        await self._wait_for_page_text(
            page,
            ("工单管理", "素材管理"),
            timeout_ms=None,
            raise_on_timeout=True,
        )
        await self._click_text(page, "素材管理")
        await page.wait_for_timeout(2500)
        await self._wait_tomato_material_page_ready(page)
        return page

    async def _select_customer(
            self,
            page,
            customer_id: str,
            progress: ProgressCallback | None,
    ) -> None:
        customer_id = str(customer_id or "").strip()
        if not customer_id:
            return
        body = await self._body_text(page, timeout_ms=3000)
        if customer_id not in body:
            await self._click_if_present(page, "客户列表")
            search = await self._first_existing(
                page,
                (
                    "input[placeholder*='客户']",
                    "input[placeholder*='ID']",
                    "input[placeholder*='搜索']",
                ),
            )
            if search:
                await search.fill(customer_id)
                await search.press("Enter")
                await page.wait_for_timeout(2000)

        customer = page.get_by_text(customer_id, exact=True).first
        try:
            if not await customer.count() or not await customer.is_visible():
                self._emit(progress, f"客户 {customer_id} 未显示在列表中，沿用当前客户上下文")
                return
        except Exception:
            return

        for xpath in (
            "xpath=ancestor::tr[1]",
            "xpath=ancestor::*[contains(@class,'card')][1]",
            "xpath=ancestor::*[contains(@class,'item')][1]",
            "xpath=ancestor::*[contains(@class,'row')][1]",
        ):
            container = customer.locator(xpath)
            try:
                if not await container.count() or not await container.is_visible():
                    continue
                for text in ("进入", "选择", "切换", "确认"):
                    button = container.get_by_text(text, exact=True).first
                    if await button.count() and await button.is_visible():
                        await button.click()
                        await page.wait_for_timeout(1800)
                        self._emit(progress, f"已选择客户 {customer_id}")
                        return
            except Exception:
                continue
        await customer.click()
        await page.wait_for_timeout(1800)
        await self._click_if_present(page, "确定")
        self._emit(progress, f"已选择客户 {customer_id}")

    async def _wait_tomato_material_page_ready(self, page) -> None:
        async def ready() -> bool:
            body = await self._body_text(page, timeout_ms=2500)
            return "素材管理" in body and "全部素材" in body and "全局搜索" in body

        if not await self._wait_for_result(ready, timeout_ms=60000, interval_ms=800):
            await self._snapshot_error(page, "tomato_music_material_page_not_ready")
            raise RuntimeError("未进入墨攻素材管理页")

    @staticmethod
    def _material_search_base_url(url: str) -> str:
        parts = urlsplit(str(url or ""))
        params = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "q"]
        query = urlencode(params)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

    @staticmethod
    def _material_search_url(base_url: str, cids: list[str]) -> str:
        parts = urlsplit(base_url)
        params = parse_qsl(parts.query, keep_blank_values=True)
        params.append(("q", "\n".join(cids)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))

    async def _tag_one_cid_chunk(
            self,
            page,
            *,
            base_url: str,
            cids: list[str],
            tag: str,
            progress: ProgressCallback | None,
            result: TomatoMusicChunkResult,
    ):
        search_url = self._material_search_url(base_url, cids)
        await self._safe_goto(page, search_url)
        matched_count = await self._wait_material_search_result(page, cids)
        result.matched_count = matched_count
        if matched_count <= 0:
            result.status = "skipped"
            result.message = "当前客户素材库未检索到这些 CID"
            self._emit(progress, f"番茄音乐 {result.bid} 第 {result.chunk_index} 组：未检索到素材，跳过")
            return page
        if matched_count > len(cids):
            raise RuntimeError(
                f"搜索结果 {matched_count} 条超过本组 CID 数 {len(cids)}，为避免误选已停止"
            )

        if await self._selected_count(page) > 0:
            await self._clear_redfruit_material_selection(page, progress)
        if not await self._click_first_material_card(page, []):
            await self._snapshot_error(page, "tomato_music_first_card_not_selected")
            raise RuntimeError("未选中番茄音乐搜索结果中的第一条素材")
        await self._wait_redfruit_material_selection_bar_visible(page)
        await self._select_redfruit_all_materials(page, matched_count)
        selected = await self._selected_count(page)
        if selected != matched_count:
            raise RuntimeError(f"实际选中 {selected} 条，预期 {matched_count} 条，已停止提交")

        await self._run_material_edit_action(page, "修改自定义标签")
        await self._fill_tomato_custom_tag_dialog(page, tag)
        before_pages = list(page.context.pages)
        before_url = page.url
        await self._submit_tomato_custom_tag_dialog(page)
        task_page = await self._wait_redfruit_arlp_task_page(page, before_pages, before_url, progress)
        task_id = await self._read_current_task_id(task_page, progress)
        result.task_id = task_id
        task_result = await self._wait_redfruit_arlp_task_result(task_page, progress, "修改自定义标签")
        result.total = int(task_result.get("total") or 0)
        result.success = int(task_result.get("success") or 0)
        result.failed = int(task_result.get("failed") or 0)
        if not self._redfruit_operation_all_expected_success(task_result, matched_count):
            raise RuntimeError(
                f"标签任务 {task_id} 未全部成功：成功 {result.success}/{result.total}，失败 {result.failed}，"
                f"目标 {matched_count} 条"
            )
        result.status = "success"
        result.message = f"任务 {task_id} 全部成功"
        self._emit(
            progress,
            f"番茄音乐 {result.bid} 第 {result.chunk_index} 组：任务 {task_id}，"
            f"成功 {result.success}/{result.total}，失败 {result.failed}",
        )
        await self._close_redfruit_result_dialog(page)
        if page.is_closed():
            page = task_page
        return page

    async def _wait_material_search_result(self, page, cids: list[str]) -> int:
        minimum_wait_seconds = 4.0
        started = asyncio.get_running_loop().time()
        while True:
            self._raise_if_cancelled()
            body = await self._body_text(page, timeout_ms=3000)
            matches = re.findall(r"共\s*(\d+)\s*条", body)
            if matches:
                return int(matches[-1])
            if "暂无数据" in body and asyncio.get_running_loop().time() - started >= minimum_wait_seconds:
                return 0
            if asyncio.get_running_loop().time() - started >= 60:
                await self._snapshot_error(
                    page,
                    "tomato_music_search_timeout",
                    extra=f"cids={','.join(cids)}",
                )
                raise RuntimeError("等待番茄音乐 CID 搜索结果超时")
            await page.wait_for_timeout(800)

    async def _fill_tomato_custom_tag_dialog(self, page, tag: str) -> None:
        dialog = await self._wait_tomato_custom_tag_dialog(page)
        body = await self._locator_text(dialog, timeout_ms=3000)
        if tag in body:
            return
        inputs = dialog.locator(".arco-input-tag input, input")
        input_box = None
        for index in range(await inputs.count() - 1, -1, -1):
            candidate = inputs.nth(index)
            if await candidate.is_visible():
                input_box = candidate
                break
        if input_box is None:
            raise RuntimeError("修改自定义标签弹窗中未找到标签输入框")
        await input_box.fill(tag)
        await input_box.press("Enter")
        await page.wait_for_timeout(400)
        if tag not in await self._locator_text(dialog, timeout_ms=3000):
            raise RuntimeError(f"自定义标签未成功加入弹窗：{tag}")

    async def _wait_tomato_custom_tag_dialog(self, page):
        async def find_dialog():
            candidates = page.locator(
                ".arco-modal:has-text('修改自定义标签'), "
                "[role='dialog']:has-text('修改自定义标签')"
            )
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if await candidate.is_visible():
                    return candidate
            return None

        dialog = await self._wait_for_result(find_dialog, timeout_ms=15000, interval_ms=300)
        if not dialog:
            raise RuntimeError("修改自定义标签弹窗未出现")
        return dialog

    async def _submit_tomato_custom_tag_dialog(self, page) -> None:
        dialog = await self._wait_tomato_custom_tag_dialog(page)
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(250)
        except Exception:
            pass
        for text in ("确定", "保存", "确认"):
            button = dialog.get_by_role("button", name=text, exact=True).first
            try:
                if await button.count() and await button.is_visible() and await button.is_enabled():
                    await button.click()
                    await page.wait_for_timeout(1800)
                    return
            except Exception:
                continue
        raise RuntimeError("修改自定义标签弹窗中未找到可点击的确定按钮")


def build_dry_run_payload(
        batches: list[TomatoMusicTagBatch],
        *,
        input_path: Path | str,
        customer_id: str,
        chunk_size: int,
) -> dict:
    return {
        "workflow": "tomato_music_bid_tagging",
        "dry_run": True,
        "input": str(input_path),
        "customer_id": customer_id,
        "chunk_size": max(1, min(chunk_size, MAX_SEARCH_CHUNK_SIZE)),
        "summary": {
            "batches": len(batches),
            "cids": sum(len(batch.cids) for batch in batches),
            "chunks": sum(len(split_cids(batch.cids, chunk_size)) for batch in batches),
        },
        "batches": [asdict(batch) for batch in batches],
    }
